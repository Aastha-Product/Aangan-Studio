"""Cal.com setup helper (uses CALCOM_API_KEY from .env).

    python scripts/setup_calcom.py me                                   # check the key
    python scripts/setup_calcom.py event-types                          # list event types (find CALCOM_EVENT_TYPE_ID)
    python scripts/setup_calcom.py create-event-type --studio-address "…" [--minutes 60]
                                                                        # "Aangan Design Consultation": site visit (caller's
                                                                        # address) or at the studio; saves CALCOM_EVENT_TYPE_ID
    python scripts/setup_calcom.py slots [EVENT_TYPE_ID]                # next open slots, as the agent would speak them
    python scripts/setup_calcom.py create-webhook                       # subscribe /api/calcom/webhook (needs PUBLIC_BASE_URL)
"""
import argparse
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import calcom, config  # noqa: E402
from backend.http import request_json  # noqa: E402

ENV_PATH = config.ROOT / ".env"
TRIGGERS = ["BOOKING_CREATED", "BOOKING_RESCHEDULED", "BOOKING_CANCELLED", "BOOKING_NO_SHOW_UPDATED"]


def set_env(key: str, value: str):
    s = ENV_PATH.read_text(encoding="utf-8")
    if re.search(rf"^{key}=", s, re.M):
        s = re.sub(rf"^{key}=.*$", f"{key}={value}", s, flags=re.M)
    else:
        s = s.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(s, encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["me", "event-types", "create-event-type", "slots", "create-webhook"])
    ap.add_argument("event_type_id", nargs="?")
    ap.add_argument("--studio-address")
    ap.add_argument("--minutes", type=int, default=60)
    a = ap.parse_args()
    if not config.CALCOM_API_KEY:
        sys.exit("CALCOM_API_KEY is empty in .env")

    if a.cmd == "me":
        d = request_json("GET", f"{calcom.API}/me", calcom._h())["data"]
        print(f"OK: {d.get('username')} · {d.get('email')} · timezone {d.get('timeZone')}")

    elif a.cmd == "event-types":
        for et in request_json("GET", f"{calcom.API}/event-types", calcom._h(calcom.V_EVENT_TYPES))["data"]:
            locs = [l.get("type") for l in et.get("locations") or []]
            print(f"{et['id']:>9}  {et.get('title')!r:<34} {et.get('lengthInMinutes')} min  locations={locs}"
                  f"{'  <- in use' if str(et['id']) == config.CALCOM_EVENT_TYPE_ID else ''}")

    elif a.cmd == "create-event-type":
        if not a.studio_address:
            sys.exit('Give the real studio address: --studio-address "…" (never invented)')
        body = {"title": "Aangan Design Consultation", "slug": "aangan-design-consultation",
                "lengthInMinutes": a.minutes,
                "description": "First consultation with an Aangan Studio designer — at your site or at our studio.",
                "locations": [{"type": "attendeeAddress"},
                              {"type": "address", "address": a.studio_address, "public": True}]}
        et = request_json("POST", f"{calcom.API}/event-types", calcom._h(calcom.V_EVENT_TYPES), body)["data"]
        set_env("CALCOM_EVENT_TYPE_ID", str(et["id"]))
        print(f"Created event type {et['id']} '{et['title']}' ({a.minutes} min) and saved CALCOM_EVENT_TYPE_ID to .env")

    elif a.cmd == "slots":
        if a.event_type_id:
            config.CALCOM_EVENT_TYPE_ID = a.event_type_id
        if not config.CALCOM_EVENT_TYPE_ID:
            sys.exit("No event type: pass an id or set CALCOM_EVENT_TYPE_ID")
        for s in calcom.open_slots(limit=6):
            print(f"{s['label']:<32} id={s['id']}")

    elif a.cmd == "create-webhook":
        if not config.PUBLIC_BASE_URL:
            sys.exit("Set PUBLIC_BASE_URL in .env first (your Vercel URL).")
        secret = config.CALCOM_WEBHOOK_SECRET or secrets.token_urlsafe(32)
        body = {"subscriberUrl": f"{config.PUBLIC_BASE_URL}/api/calcom/webhook", "triggers": TRIGGERS,
                "active": True, "secret": secret}
        wh = request_json("POST", f"{calcom.API}/webhooks", calcom._h(), body)["data"]
        set_env("CALCOM_WEBHOOK_SECRET", secret)
        print(f"Webhook {wh.get('id')} -> {body['subscriberUrl']}  (secret saved to .env; add it to Vercel too)")


if __name__ == "__main__":
    main()
