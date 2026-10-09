"""One-off Calendly setup helper.

    python scripts/setup_calendly.py list-event-types        # find CALENDLY_EVENT_TYPE_URI
    python scripts/setup_calendly.py create-webhook           # subscribe our endpoint (needs PUBLIC_BASE_URL)
    python scripts/setup_calendly.py list-webhooks

Webhook subscriptions need Calendly Standard or higher; the Scheduling API (booking) needs a paid plan.
"""
import secrets
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from backend.http import request_json  # noqa: E402

H = {"Authorization": f"Bearer {config.CALENDLY_TOKEN}"}
EVENTS = ["invitee.created", "invitee.canceled", "invitee_no_show.created", "invitee_no_show.deleted"]


def me():
    return request_json("GET", f"{config.CALENDLY_API}/users/me", H)["resource"]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list-event-types"
    user = me()
    if cmd == "list-event-types":
        q = urllib.parse.urlencode({"organization": user["current_organization"], "active": "true"})
        for et in request_json("GET", f"{config.CALENDLY_API}/event_types?{q}", H)["collection"]:
            print(f"{et['name']:<40} {et.get('pooling_type') or 'solo':<12} {et['uri']}")
    elif cmd == "create-webhook":
        if not config.PUBLIC_BASE_URL:
            sys.exit("Set PUBLIC_BASE_URL in .env first (your Vercel URL).")
        key = config.CALENDLY_SIGNING_KEY or secrets.token_urlsafe(32)
        body = {"url": f"{config.PUBLIC_BASE_URL}/api/calendly/webhook", "events": EVENTS,
                "organization": user["current_organization"], "scope": "organization", "signing_key": key}
        res = request_json("POST", f"{config.CALENDLY_API}/webhook_subscriptions", H, body)
        print("Created:", res["resource"]["uri"])
        if not config.CALENDLY_SIGNING_KEY:
            print("Put this in .env and in Vercel as CALENDLY_WEBHOOK_SIGNING_KEY:", key)
    elif cmd == "list-webhooks":
        q = urllib.parse.urlencode({"organization": user["current_organization"], "scope": "organization"})
        for w in request_json("GET", f"{config.CALENDLY_API}/webhook_subscriptions?{q}", H)["collection"]:
            print(w["callback_url"], w["events"], w["state"])


if __name__ == "__main__":
    main()
