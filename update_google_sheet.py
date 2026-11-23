"""GitHub Actions completion callback to update the originating Google Sheet row."""
import json
import os
import re
import sys
from urllib.request import Request, urlopen

from fk_common import BASE_DIR


REQUEST_ID_PATTERN = r"REQ-[0-9]{8}-[A-Za-z0-9_-]{1,40}"


def main():
    request_id = os.environ.get("REQUEST_ID", "")

    if not re.fullmatch(REQUEST_ID_PATTERN, request_id):
        raise RuntimeError("REQUEST_ID has an invalid format")

    status_file = BASE_DIR / "reports" / request_id / "request_status.json"

    if not status_file.is_file():
        raise RuntimeError("Request status file was not found")

    status = json.loads(status_file.read_text(encoding="utf-8"))
    result = status.get("result", {})

    details = (
        status.get("error")
        or result.get("failure_reason")
        or "; ".join(result.get("warnings", []))
    )

    payload = {
        "secret": os.environ.get("SHEETS_CALLBACK_SECRET", ""),
        "request_id": request_id,
        "status": status.get("status", "Failed"),
        "details": details,
        "job_url": os.environ.get("BUILD_URL", ""),
    }

    url = os.environ.get("SHEETS_CALLBACK_URL", "")

    if not url.startswith("https://"):
        raise RuntimeError(
            "SHEETS_CALLBACK_URL must be an HTTPS Apps Script web-app URL"
        )

    if not payload["secret"]:
        raise RuntimeError(
            "SHEETS_CALLBACK_SECRET is not configured in GitHub Actions secrets"
        )

    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urlopen(req, timeout=30) as response:
        response_body = response.read().decode("utf-8")

        try:
            callback_result = json.loads(response_body)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"Apps Script returned a non-JSON response (HTTP {response.status})"
            ) from None

        if response.status >= 300 or not callback_result.get("ok"):
            raise RuntimeError(
                f"Google Sheets callback was rejected (HTTP {response.status})"
            )

    print(f"Google Sheets callback succeeded for {request_id}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(
            f"Sheets status callback failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
