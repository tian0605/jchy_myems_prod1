import io
import json
import ssl
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from myems_jishiyun.client import MAX_RESPONSE_BYTES, JishiyunClient, NoRedirect, TransportError
from myems_jishiyun.contracts import ContractError, EnergyReading
from myems_jishiyun.verify import main, provider_timezone


BASE_URL = "https://synthetic.invalid/WebApi"
SYNTHETIC_KEY = "synthetic-test-only"
LOCAL_TIME = timezone(timedelta(hours=8))
START = datetime(2026, 10, 1, 12, tzinfo=LOCAL_TIME)
END = START + timedelta(hours=1)
EMPTY_BODY = b'{"IsSuccess":true,"Code":"0000","Data":[],"Count":0}'


def http_error(status: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(BASE_URL, status, "Synthetic error", headers, io.BytesIO(b"not logged"))


def opened_response(body: bytes = EMPTY_BODY) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        opener_patch = patch("myems_jishiyun.client.build_opener")
        self.mock_opener = opener_patch.start().return_value
        self.addCleanup(opener_patch.stop)
        sleep_patch = patch("myems_jishiyun.client.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        clock_patch = patch("myems_jishiyun.client.time.monotonic", return_value=100.0)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.mock_opener.open.return_value = opened_response()
        self.client = JishiyunClient(BASE_URL, SYNTHETIC_KEY, LOCAL_TIME)

    def test_json_post_uses_auth_header_not_url_or_body(self) -> None:
        self.assertEqual(self.client.readings(START, END), ())
        request = self.mock_opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, BASE_URL + "/NH2/ElectricQuantity")
        self.assertEqual(request.get_header("Api_key"), SYNTHETIC_KEY)
        self.assertEqual(json.loads(request.data)["valueType"], "SJZ")
        self.assertNotIn(SYNTHETIC_KEY.encode(), request.data)
        self.assertEqual(self.mock_opener.open.call_args.kwargs["timeout"], 30)
        self.assertEqual(self.client.request_count, 1)

    def test_explicit_form_encoding_without_fallback(self) -> None:
        client = JishiyunClient(BASE_URL, SYNTHETIC_KEY, LOCAL_TIME, encoding="form")
        client.readings(START, END)
        request = self.mock_opener.open.call_args.args[0]
        self.assertIn("application/x-www-form-urlencoded", request.get_header("Content-type"))
        parameters = parse_qs(request.data.decode(), keep_blank_values=True)
        self.assertEqual(parameters["valueType"], ["SJZ"])
        self.assertEqual(parameters["meterAddress"], [""])
        self.assertEqual(parameters["dateType"], ["mi15"])

    def test_archive_endpoint_and_throttle_across_both_endpoints(self) -> None:
        self.client.archives()
        self.assertTrue(self.mock_opener.open.call_args.args[0].full_url.endswith("/AmmeterBasicInfo"))
        self.client.readings(START, END)
        self.sleep.assert_called_once_with(7)
        self.assertEqual(self.client.request_count, 2)

    def test_retry_429_honors_delay_and_quota(self) -> None:
        self.mock_opener.open.side_effect = [http_error(429, "20"), opened_response()]
        self.client.archives()
        self.assertIn(call(20), self.sleep.call_args_list)
        self.assertEqual(self.client.request_count, 2)

    def test_http_date_retry_delay_is_honored(self) -> None:
        now = datetime(2026, 6, 1, 7, 59, 40, tzinfo=timezone.utc).timestamp()
        with patch("myems_jishiyun.client.time.time", return_value=now):
            self.mock_opener.open.side_effect = [http_error(503, "Mon, 01 Jun 2026 08:00:00 GMT"), opened_response()]
            self.client.archives()
        self.assertIn(call(20), self.sleep.call_args_list)
        self.assertEqual(self.client.request_count, 2)

    def test_invalid_retry_delay_is_reported(self) -> None:
        self.mock_opener.open.side_effect = http_error(429, "invalid delay")
        with self.assertRaisesRegex(TransportError, "invalid retry delay"):
            self.client.archives()
        self.assertEqual(self.client.request_count, 1)

    def test_long_provider_delay_is_deferred_not_ignored(self) -> None:
        self.mock_opener.open.side_effect = http_error(429, "301")
        with self.assertRaisesRegex(TransportError, "defer this task"):
            self.client.archives()
        self.assertEqual(self.client.request_count, 1)

    def test_authentication_and_redirects_are_not_retried(self) -> None:
        for status in (301, 302, 303, 307, 308, 400, 401, 403, 404, 500):
            with self.subTest(status=status):
                self.mock_opener.open.reset_mock()
                self.mock_opener.open.side_effect = http_error(status)
                with self.assertRaisesRegex(TransportError, f"status {status}"):
                    self.client.archives()
                self.assertEqual(self.mock_opener.open.call_count, 1)

    def test_redirect_handler_never_forwards_credentials(self) -> None:
        self.assertIsNone(NoRedirect().redirect_request(MagicMock(), None, 302, "", None, "http://other.invalid"))

    def test_certificate_errors_do_not_retry(self) -> None:
        for failure in (
            ssl.SSLCertVerificationError("synthetic cert error"),
            URLError(ssl.SSLCertVerificationError("synthetic cert error")),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.mock_opener.open.reset_mock()
                self.mock_opener.open.side_effect = failure
                with self.assertRaisesRegex(TransportError, "certificate validation failed"):
                    self.client.archives()
                self.assertEqual(self.mock_opener.open.call_count, 1)

    def test_retryable_network_errors_are_bounded_and_sanitized(self) -> None:
        for failure in (URLError(SYNTHETIC_KEY), TimeoutError(SYNTHETIC_KEY), ConnectionError(SYNTHETIC_KEY)):
            with self.subTest(failure=type(failure).__name__):
                self.mock_opener.open.reset_mock()
                self.mock_opener.open.side_effect = failure
                with self.assertRaises(TransportError) as caught:
                    self.client.archives()
                self.assertEqual(self.mock_opener.open.call_count, 3)
                self.assertNotIn(SYNTHETIC_KEY, str(caught.exception))

    def test_business_error_does_not_retry_or_echo_body(self) -> None:
        body = json.dumps({"IsSuccess": False, "Code": "error", "Msg": SYNTHETIC_KEY}).encode()
        self.mock_opener.open.return_value = opened_response(body)
        with self.assertRaises(ContractError) as caught:
            self.client.archives()
        self.assertNotIn(SYNTHETIC_KEY, str(caught.exception))
        self.assertEqual(self.mock_opener.open.call_count, 1)

    def test_interrupted_response_retries_without_logging_partial_body(self) -> None:
        response = opened_response()
        response.__enter__.return_value.read.side_effect = IncompleteRead(SYNTHETIC_KEY.encode())
        self.mock_opener.open.return_value = response
        with self.assertRaises(TransportError) as caught:
            self.client.archives()
        self.assertNotIn(SYNTHETIC_KEY, str(caught.exception))
        self.assertEqual(self.client.request_count, 3)

    def test_utf8_bom_supported_and_other_encoding_rejected(self) -> None:
        self.mock_opener.open.return_value = opened_response(b"\xef\xbb\xbf" + EMPTY_BODY)
        self.assertEqual(self.client.archives(), ())
        self.mock_opener.open.return_value = opened_response(b"\xff")
        with self.assertRaisesRegex(TransportError, "not UTF-8"):
            self.client.archives()

    def test_response_size_is_bounded(self) -> None:
        self.mock_opener.open.return_value = opened_response(b"x" * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(TransportError, "size limit"):
            self.client.archives()

    def test_insecure_or_embedded_credential_urls_are_rejected(self) -> None:
        for url in (
            "http://synthetic.invalid/WebApi", "https://user:pass@synthetic.invalid/WebApi",
            BASE_URL + "?key=value", BASE_URL + "#fragment", "https://synthetic.invalid/other",
            "https://synthetic.invalid:broken/WebApi", "https://synthetic.invalid:99999/WebApi",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                JishiyunClient(url, SYNTHETIC_KEY, LOCAL_TIME)

    def test_key_and_numeric_configuration_validation(self) -> None:
        for key in ("", "has space", "line\r\nbreak", "non-ascii-\u00e9"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                JishiyunClient(BASE_URL, key, LOCAL_TIME)
        for timeout in (0, -1, float("nan"), float("inf"), 121):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                JishiyunClient(BASE_URL, SYNTHETIC_KEY, LOCAL_TIME, timeout_seconds=timeout)
        for attempts in (0, 6, True):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                JishiyunClient(BASE_URL, SYNTHETIC_KEY, LOCAL_TIME, max_attempts=attempts)


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        environment_patch = patch.dict("os.environ", {"JISHIYUN_API_KEY": SYNTHETIC_KEY}, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        client_patch = patch("myems_jishiyun.verify.JishiyunClient")
        self.client = client_patch.start().return_value
        self.addCleanup(client_patch.stop)
        self.client.archives.return_value = ()
        self.client.readings.return_value = ()
        self.client.request_count = 2
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.args = ["--base-url", BASE_URL, "--utc-offset", "+08:00"]

    def invoke(self, extra: list[str]) -> int:
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return main(self.args + extra)

    def test_archives_only_does_not_request_readings(self) -> None:
        self.assertEqual(self.invoke([]), 0)
        self.client.readings.assert_not_called()
        self.assertNotIn(SYNTHETIC_KEY, self.stdout.getvalue())

    def test_no_scope_does_not_claim_complete(self) -> None:
        self.assertEqual(self.invoke(["--start", "2026-10-01 12:00:00", "--end", "2026-10-01 13:00:00"]), 0)
        self.assertIsNone(json.loads(self.stdout.getvalue())["interval_coverage_complete"])

    def test_configured_scope_detects_missing_slots_and_returns_nonzero(self) -> None:
        self.assertEqual(self.invoke([
            "--start", "2026-10-01 12:00:00", "--end", "2026-10-01 13:00:00",
            "--expected-addresses", "synthetic-meter",
        ]), 2)
        summary = json.loads(self.stdout.getvalue())
        self.assertEqual(summary["missing_count"], 4)
        self.assertFalse(summary["hourly_boundary_verified"])

    def test_invalid_window_fails_before_requests(self) -> None:
        self.assertEqual(self.invoke(["--start", "2026-10-01 12:00:00", "--end", "2026-10-01 12:07:00"]), 1)
        self.client.archives.assert_not_called()

    def test_invalid_reading_without_scope_is_not_success(self) -> None:
        self.client.readings.return_value = (
            EnergyReading("synthetic-meter", 1, START, None, "Invalid register", {}),
        )
        self.assertEqual(self.invoke(["--start", "2026-10-01 12:00:00", "--end", "2026-10-01 13:00:00"]), 2)
        self.assertEqual(json.loads(self.stdout.getvalue())["invalid_reading_count"], 1)

    def test_missing_key_fails_without_echoing_input(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.invoke([]), 1)
        self.client.archives.assert_not_called()

    def test_offset_validation(self) -> None:
        self.assertEqual(provider_timezone("+08:00").utcoffset(None), timedelta(hours=8))
        self.assertEqual(provider_timezone("-05:30").utcoffset(None), -timedelta(hours=5, minutes=30))
        for value in ("UTC", "+24:00", "+14:01", "+08:60"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                provider_timezone(value)


if __name__ == "__main__":
    unittest.main()
