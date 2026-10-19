"""Windows-only WorkBuddy Buddy Station daily check-in.

Credentials are read from the local WorkBuddy session and never written to disk.
The service endpoints are internal and undocumented and may change.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import auth_helper

API_HOST = "copilot.tencent.com"
API_BASE = f"https://{API_HOST}/v2/billing/meter/"
SHANGHAI = timezone(timedelta(hours=8))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def state_dir():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("WINDOWS_PROFILE_UNAVAILABLE")
    return Path(local_app_data) / "WorkBuddyDailyCredit"


def request_data(opener, headers, action):
    if action not in ("checkin-activity-status", "daily-checkin"):
        raise ValueError("INVALID_ACTION")
    request = urllib.request.Request(
        API_BASE + action,
        data=b"{}",
        headers=headers,
        method="POST",
    )
    with opener.open(request, timeout=25) as response:
        payload = json.load(response)
    if (not isinstance(payload, dict) or payload.get("code") != 0 or
            not isinstance(payload.get("data"), dict)):
        raise ValueError("API_REJECTED_OR_CHANGED")
    return payload["data"]


def run(status_only=False):
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("WINDOWS_PROFILE_UNAVAILABLE")
    auth_file = Path(local_app_data) / auth_helper.AUTH_BASENAME
    session = json.loads(auth_file.read_text(encoding="utf-8-sig"))
    session = auth_helper.resolve_session(session)

    account = session["account"]
    auth = session["auth"]
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + auth["accessToken"],
        "X-User-Id": str(account["uid"]),
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy",
    }
    if auth.get("domain"):
        headers["X-Domain"] = str(auth["domain"])

    opener = urllib.request.build_opener(NoRedirect)
    status = request_data(opener, headers, "checkin-activity-status")
    if status_only:
        result = "STATUS"
    elif status.get("today_checked_in") is True:
        result = "ALREADY_CLAIMED"
    elif status.get("active") is False:
        result = "INACTIVE"
    elif status.get("active") is True and status.get("today_checked_in") is False:
        try:
            request_data(opener, headers, "daily-checkin")
        except Exception:
            # Handle another client claiming between our status check and claim.
            latest = request_data(opener, headers, "checkin-activity-status")
            if latest.get("today_checked_in") is True:
                status = latest
                result = "ALREADY_CLAIMED"
            else:
                raise
        else:
            status = request_data(opener, headers, "checkin-activity-status")
            if status.get("today_checked_in") is not True:
                raise ValueError("VERIFY_FAILED")
            result = "CLAIMED"
    else:
        raise ValueError("STATUS_SCHEMA_CHANGED")

    allowed = (
        "active", "today_checked_in", "today_credit", "daily_credit",
        "streak_days", "total_credits", "end_time",
    )
    return {
        "result": result,
        **{key: status[key] for key in allowed if key in status},
    }


def main():
    exit_code = 0
    try:
        result = run("--status" in sys.argv[1:])
    except auth_helper.AuthError as exc:
        reason = getattr(exc, "reason", "AUTH_ERROR")
        result = {"result": "ERROR", "reason": str(reason)}
        exit_code = 1
    except Exception as exc:
        safe_reasons = {
            "API_REJECTED_OR_CHANGED", "STATUS_SCHEMA_CHANGED",
            "VERIFY_FAILED", "INVALID_ACTION", "WINDOWS_PROFILE_UNAVAILABLE",
        }
        reason = str(exc) if isinstance(exc, (ValueError, RuntimeError)) and str(exc) in safe_reasons else type(exc).__name__
        result = {"result": "ERROR", "reason": reason}
        exit_code = 1

    result["time"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    line = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    try:
        log = state_dir() / "results.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except Exception:
        # A log failure must not change the safe claim result.
        pass
    print(line)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

