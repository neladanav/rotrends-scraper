"""Read-only Supabase preflight for the RoTrends collector.

The request calls dt_pipeline_call('authorize') with a fresh, random, invalid
collector token. That SQL branch only compares a hash in dt_state and returns
false; it does not create a lease, start the pipeline, or write game data.
The Edge Function should therefore return HTTP 401 when the database path is
healthy. A Supabase service restriction (HTTP 402), database/API outage, or
misconfigured function returns another status and blocks scraping.
"""
from __future__ import annotations

import os
import secrets
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

EXPECTED_HOST = "xxuwqkscmxhjgibuhpwc.supabase.co"
TIMEOUT_SECONDS = 15


class PreflightError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_base_url(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url.strip())
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        raise PreflightError("SUPABASE_URL is invalid; no collection was started.") from None

    if (
        parsed.scheme != "https"
        or parsed.hostname != EXPECTED_HOST
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise PreflightError("SUPABASE_URL does not match the configured Scoutbase project; no collection was started.")

    return f"https://{EXPECTED_HOST}/functions/v1/dt-scout-pipeline"


def check_supabase_ready(base_url: str, opener=None) -> int:
    endpoint = validate_base_url(base_url)

    # A fresh random token is deliberately not a registered collector token.
    # The pipeline's 'authorize' operation only performs a read-only hash
    # comparison; successful DB access is represented by its expected 401.
    token = secrets.token_hex(32)
    request = Request(
        endpoint,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-Scout-Token": token,
        },
        method="POST",
    )

    client = opener or build_opener(_NoRedirect())
    try:
        with client.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.getcode()
    except HTTPError as error:
        status = error.code
        error.close()
    except (URLError, TimeoutError, OSError):
        raise PreflightError(
            "Supabase collector preflight could not reach the database authorization path; no scrape was started."
        ) from None

    if status != 401:
        raise PreflightError(
            "Supabase collector preflight expected HTTP 401 from the deliberately invalid token, "
            f"but received HTTP {status}; no scrape was started."
        )
    return status


def main() -> int:
    base_url = os.environ.get("SUPABASE_URL", "")
    try:
        status = check_supabase_ready(base_url)
    except PreflightError as error:
        print(f"Supabase preflight blocked collection: {error}", file=sys.stderr)
        return 1

    print(
        f"Supabase preflight passed (HTTP {status}): the collector authorization RPC is reachable. "
        "The test token was rejected; no game data or cursor was changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
