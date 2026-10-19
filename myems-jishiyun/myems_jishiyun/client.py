"""Bounded read-only HTTP client for the two approved Jishiyun endpoints."""

from __future__ import annotations

import json
import logging
import math
import random
import ssl
import threading
import time
from contextlib import nullcontext
from datetime import datetime, tzinfo
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Callable, ContextManager, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from myems_jishiyun.contracts import EnergyReading, MeterArchive, electricity_request, parse_archives, parse_readings


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
RETRYABLE_STATUS = {429, 502, 503, 504}
Encoding = Literal["json", "form"]


def http_test_setting(value: str) -> bool:
    if value not in ("true", "false"):
        raise ValueError("JISHIYUN_ALLOW_HTTP_TEST must be true or false")
    return value == "true"


class TransportError(RuntimeError):
    """Sanitized request failure; never contains request headers or raw bodies."""

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def retry_delay(header: str) -> float:
    """Parse provider delay once for both client retries and the shared gate."""
    if not header:
        return 0
    if header.isdecimal():
        try:
            delay = float(header)
        except (ValueError, OverflowError):
            raise TransportError("Provider returned an invalid retry delay; defer this task") from None
    else:
        try:
            retry_at = parsedate_to_datetime(header)
        except (ValueError, TypeError, OverflowError):
            raise TransportError("Provider returned an invalid retry delay; defer this task") from None
        if retry_at.tzinfo is None:
            raise TransportError("Provider returned an ambiguous retry delay; defer this task") from None
        delay = max(0.0, retry_at.timestamp() - time.time())
    if not math.isfinite(delay) or delay > 31536000:
        raise TransportError("Provider retry delay exceeds supported one-year range; manual review required")
    return delay


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str,
        headers: object, newurl: str,
    ) -> None:
        # API keys must not be forwarded to redirected hosts or downgraded URLs.
        return None


class JishiyunClient:
    """Local limiter with an optional shared request guard for worker processes."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        provider_timezone: tzinfo,
        encoding: Encoding = "json",
        timeout_seconds: float = 30,
        max_attempts: int = 3,
        request_guard: Callable[[], ContextManager[None]] | None = None,
        *,
        allow_http_test: bool = False,
    ) -> None:
        if type(allow_http_test) is not bool:
            raise ValueError("HTTP test permission must be a boolean")
        try:
            parsed = urlsplit(base_url)
            valid_port = parsed.port
        except ValueError:
            raise ValueError("Invalid provider base URL") from None
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") != "/WebApi"
            or (valid_port is not None and not 1 <= valid_port <= 65535)
        ):
            raise ValueError("Provider URL must be HTTPS with path /WebApi and no embedded credentials")
        if parsed.scheme == "http":
            if not allow_http_test or parsed.hostname != "nhapi.yunjichaobiao.com" or valid_port not in (None, 80):
                raise ValueError("HTTP requires explicit test permission and the approved provider host on port 80")
            logging.getLogger(__name__).warning(
                "Jishiyun HTTP test transport enabled: API key and provider data are transmitted without encryption",
            )
        if not api_key.strip() or any(ord(character) < 33 or ord(character) > 126 for character in api_key):
            raise ValueError("API key must contain visible ASCII characters only")
        if encoding not in ("json", "form"):
            raise ValueError("Request encoding must be json or form")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError("Timeout must be greater than zero and at most 120 seconds")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError("Attempts must be between 1 and 5")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.provider_timezone = provider_timezone
        self.encoding = encoding
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self._request_guard = request_guard
        self._opener = build_opener(NoRedirect())
        self._lock = threading.Lock()
        self._next_request = 0.0
        self.request_count = 0

    def archives(self) -> tuple[MeterArchive, ...]:
        return parse_archives(self._post("AmmeterBasicInfo", {"meterAddress": ""}), self.provider_timezone)

    def readings(self, start: datetime, end: datetime) -> tuple[EnergyReading, ...]:
        parameters = electricity_request(start, end, self.provider_timezone)
        return parse_readings(self._post("ElectricQuantity", parameters), self.provider_timezone)

    def _post(self, endpoint: str, parameters: dict[str, str]) -> str:
        if endpoint not in ("AmmeterBasicInfo", "ElectricQuantity"):
            raise ValueError("Unsupported provider endpoint")
        if self.encoding == "json":
            payload = json.dumps(parameters).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            payload = urlencode(parameters).encode("utf-8")
            content_type = "application/x-www-form-urlencoded; charset=utf-8"
        request = Request(
            self.base_url + "/NH2/" + endpoint,
            data=payload,
            headers={"API_KEY": self._api_key, "Content-Type": content_type, "Accept": "application/json"},
            method="POST",
        )
        for attempt in range(self.max_attempts):
            retry_after = 0.0
            failure = "Provider connection failed"
            try:
                with self._lock:
                    wait = self._next_request - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    guard = self._request_guard() if self._request_guard is not None else nullcontext()
                    self._next_request = time.monotonic() + 7
                    with guard:
                        self.request_count += 1
                        with self._opener.open(request, timeout=self.timeout_seconds) as response:
                            body = response.read(MAX_RESPONSE_BYTES + 1)
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise TransportError("Provider response exceeded the configured size limit")
                            try:
                                return body.decode("utf-8-sig")
                            except UnicodeDecodeError:
                                raise TransportError("Provider response is not UTF-8") from None
            except HTTPError as exc:
                status = exc.code
                if status in RETRYABLE_STATUS:
                    header = exc.headers.get("Retry-After", "")
                    try:
                        retry_after = retry_delay(header)
                    finally:
                        exc.close()
                exc.close()
                if status not in RETRYABLE_STATUS:
                    raise TransportError(f"Provider HTTP status {status}; request not retried") from None
                failure = f"Provider HTTP status {status}"
                if retry_after > 300:
                    raise TransportError(
                        "Provider requested a long retry delay; defer this task",
                        retryable=True, retry_after=retry_after,
                    ) from None
            except URLError as exc:
                if isinstance(exc.reason, ssl.SSLCertVerificationError):
                    raise TransportError("Provider TLS certificate validation failed; request not retried") from None
            except ssl.SSLCertVerificationError:
                raise TransportError("Provider TLS certificate validation failed; request not retried") from None
            except (TimeoutError, ConnectionError):
                failure = "Provider request timed out or connection was interrupted"
            except HTTPException:
                failure = "Provider returned an interrupted or malformed HTTP response"
            if attempt + 1 == self.max_attempts:
                raise TransportError(
                    f"{failure}; exhausted {self.max_attempts} attempts",
                    retryable=True, retry_after=retry_after,
                ) from None
            time.sleep(max(retry_after, 2 ** attempt + random.uniform(0, 1)))
        raise TransportError("Provider request did not complete")
