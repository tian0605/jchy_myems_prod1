"""Synthetic HTTP transport checks; never contact the provider."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from myems_jishiyun.client import JishiyunClient, http_test_setting
from myems_jishiyun.verify import main as verify
from myems_jishiyun.worker import main as worker
from myems_jishiyun.execution import execute_one
from test_client import LOCAL_TIME, START, END, SYNTHETIC_KEY, opened_response


HTTP_BASE = "http://nhapi.yunjichaobiao.com/WebApi"


class HTTPTestTransportTests(unittest.TestCase):
    def test_two_exact_post_endpoints_and_header_with_explicit_permission(self):
        with patch("myems_jishiyun.client.build_opener") as opener, patch(
            "myems_jishiyun.client.time.sleep",
        ), self.assertLogs("myems_jishiyun.client", level="WARNING") as logs:
            opener.return_value.open.return_value = opened_response()
            client = JishiyunClient(HTTP_BASE, SYNTHETIC_KEY, LOCAL_TIME, allow_http_test=True)
            client.archives()
            client.readings(START, END)
        calls = opener.return_value.open.call_args_list
        for call, endpoint in zip(calls, ("AmmeterBasicInfo", "ElectricQuantity")):
            request = call.args[0]
            self.assertEqual(request.full_url, HTTP_BASE + "/NH2/" + endpoint)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.get_header("Api_key"), SYNTHETIC_KEY)
            self.assertNotIn(SYNTHETIC_KEY.encode(), request.data)
        self.assertEqual(json.loads(calls[1].args[0].data)["valueType"], "SJZ")
        self.assertNotIn(SYNTHETIC_KEY, " ".join(logs.output))

    def test_http_default_denied_and_opt_in_does_not_allow_other_hosts_or_ports(self):
        with patch("myems_jishiyun.client.build_opener") as opener:
            with self.assertRaises(ValueError):
                JishiyunClient(HTTP_BASE, SYNTHETIC_KEY, LOCAL_TIME)
            for url in ("http://other.invalid/WebApi", HTTP_BASE.replace(".com", ".com.evil.invalid"),
                        "http://nhapi.yunjichaobiao.com:8080/WebApi",
                        "http://user@nhapi.yunjichaobiao.com/WebApi",
                        HTTP_BASE + "?key=x", HTTP_BASE + "/NH2/ElectricQuantity"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    JishiyunClient(url, SYNTHETIC_KEY, LOCAL_TIME, allow_http_test=True)
            opener.assert_not_called()

    def test_strict_configuration_and_permission_types(self):
        self.assertFalse(http_test_setting("false"))
        self.assertTrue(http_test_setting("true"))
        for value in ("", "1", "TRUE", "yes", " false "):
            with self.assertRaises(ValueError):
                http_test_setting(value)
        for value in ("true", 1, None):
            with self.assertRaises(ValueError):
                JishiyunClient(HTTP_BASE, SYNTHETIC_KEY, LOCAL_TIME, allow_http_test=value)

    def test_verifier_reads_key_and_http_permission_from_environment(self):
        environment = {"JISHIYUN_API_KEY": SYNTHETIC_KEY, "JISHIYUN_ALLOW_HTTP_TEST": "true"}
        with patch.dict("os.environ", environment, clear=True), patch(
            "myems_jishiyun.client.build_opener",
        ) as opener, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            opener.return_value.open.return_value = opened_response()
            self.assertEqual(verify(["--base-url", HTTP_BASE, "--utc-offset=+08:00"]), 0)
            self.assertEqual(opener.return_value.open.call_args.args[0].get_header("Api_key"), SYNTHETIC_KEY)

    def test_worker_validates_permission_before_claim_and_propagates_it(self):
        environment = {"JISHIYUN_BASE_URL": HTTP_BASE, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
                       "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_ALLOW_HTTP_TEST": "true"}
        with patch.dict("os.environ", environment, clear=True), patch(
            "myems_jishiyun.worker.database_factory",
        ), patch("myems_jishiyun.worker.execute_one", return_value=None) as execute, patch(
            "myems_jishiyun.worker.signal.signal",
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(worker(["run", "--once"]), 0)
            self.assertTrue(execute.call_args.kwargs["allow_http_test"])
            environment["JISHIYUN_ALLOW_HTTP_TEST"] = "false"
            with patch.dict("os.environ", environment, clear=True):
                execute.reset_mock()
                self.assertEqual(worker(["run", "--once"]), 1)
                execute.assert_not_called()

    def test_execution_passes_opt_in_to_archive_client(self):
        from unittest.mock import MagicMock
        queue = MagicMock()
        queue.claim.return_value.phase = "archive_fetch"
        queue.claim.return_value.offset_minutes = 480
        queue.claim.return_value.encoding = "json"
        with patch("myems_jishiyun.execution.JishiyunClient") as client, patch(
            "myems_jishiyun.initialization.ArchiveInitializer",
        ):
            execute_one(queue, HTTP_BASE, SYNTHETIC_KEY, allow_http_test=True)
            self.assertTrue(client.call_args.kwargs["allow_http_test"])


if __name__ == "__main__":
    unittest.main()
