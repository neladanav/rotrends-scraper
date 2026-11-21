import unittest
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from scripts.preflight_supabase import (
    EXPECTED_HOST,
    PreflightError,
    check_supabase_ready,
    validate_base_url,
)

BASE_URL = f"https://{EXPECTED_HOST}"


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        if self.error:
            raise self.error
        return self.response


class SupabasePreflightTests(unittest.TestCase):
    def test_accepts_only_the_configured_project_origin(self):
        self.assertEqual(
            validate_base_url(BASE_URL),
            BASE_URL + "/functions/v1/dt-scout-pipeline",
        )
        for url in (
            "http://"+EXPECTED_HOST,
            "https://example.supabase.co",
            BASE_URL + "/other-project",
            BASE_URL + "?redirect=1",
            "https://user@"+EXPECTED_HOST,
        ):
            with self.subTest(url=url), self.assertRaises(PreflightError):
                validate_base_url(url)

    def test_expected_401_means_read_only_authorization_rpc_is_reachable(self):
        opener = FakeOpener(response=FakeResponse(401))
        with patch("scripts.preflight_supabase.secrets.token_hex", return_value="a"*64):
            status = check_supabase_ready(BASE_URL, opener=opener)

        self.assertEqual(status, 401)
        self.assertEqual(opener.timeout, 15)
        self.assertEqual(opener.request.full_url, BASE_URL + "/functions/v1/dt-scout-pipeline")
        self.assertEqual(opener.request.get_method(), "POST")
        self.assertEqual(opener.request.data, b"{}")
        headers = {key.lower(): value for key, value in opener.request.header_items()}
        self.assertEqual(headers["x-scout-token"], "a"*64)

    def test_http_402_blocks_collection(self):
        opener = FakeOpener(error=HTTPError(BASE_URL, 402, "Service restricted", {}, None))
        with self.assertRaisesRegex(PreflightError, "HTTP 402"):
            check_supabase_ready(BASE_URL, opener=opener)

    def test_other_statuses_do_not_allow_collection(self):
        for status in (200, 400, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                with self.assertRaisesRegex(PreflightError, f"HTTP {status}"):
                    check_supabase_ready(BASE_URL, opener=FakeOpener(response=FakeResponse(status)))

    def test_network_failure_blocks_collection_without_leaking_details(self):
        opener = FakeOpener(error=OSError("private network detail"))
        with self.assertRaisesRegex(PreflightError, "could not reach"):
            check_supabase_ready(BASE_URL, opener=opener)


if __name__ == "__main__":
    unittest.main()
