"""Cal.com (API v2): open slots, booking on the call, webhook verification + parsing.

Verified against Cal.com's v2 OpenAPI spec and webhook guide (Oct 2026):
- Auth: "Authorization: Bearer cal_live_…"; each endpoint needs its own "cal-api-version" header (below).
- GET  /v2/slots?eventTypeId&start&end&timeZone          -> {"data": {"YYYY-MM-DD": [{"start": iso}, …]}}
- POST /v2/bookings {start, eventTypeId, attendee{name,email,timeZone,phoneNumber?}, location?, metadata}
- Webhooks: header "x-cal-signature-256" = hex HMAC-SHA256(secret, raw body);
  body {"triggerEvent": BOOKING_CREATED | BOOKING_RESCHEDULED | BOOKING_CANCELLED | BOOKING_NO_SHOW_UPDATED, "payload": {…}}
api.cal.com sits behind Cloudflare, which rejects Python's default User-Agent; backend/http.py sends a real one.
"""
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import config
from .calendly import filter_slots, parse_time, spoken_label   # shared, provider-neutral helpers
from .http import HttpError, request_json

API = "https://api.cal.com/v2"
V_SLOTS, V_BOOKINGS, V_EVENT_TYPES = "2024-09-04", "2026-02-25", "2026-06-12"
APP = "https://app.cal.com"


def _h(version: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {config.CALCOM_API_KEY}"}
    if version:
        h["cal-api-version"] = version
    return h


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_available_times(start: datetime, days: int | None = None) -> list[dict]:
    end = start + timedelta(days=min(days or config.SLOT_LOOKAHEAD_DAYS, 31))
    q = urllib.parse.urlencode({"eventTypeId": config.CALCOM_EVENT_TYPE_ID, "start": _iso(start), "end": _iso(end),
                                "timeZone": "Asia/Kolkata"})
    data = request_json("GET", f"{API}/slots?{q}", _h(V_SLOTS)).get("data") or {}
    slots = [s for day in sorted(data) for s in data[day]]
    # normalise to the Calendly shape the shared filter expects (UTC "Z" times)
    return [{"start_time": _iso(parse_time(s["start"])), "status": "available"} for s in slots if s.get("start")]


def open_slots(preference: str | None = None, limit: int = 4, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    slots = filter_slots(get_available_times(now + timedelta(hours=2)), preference)[:limit]
    return [{"id": s["start_time"], "label": spoken_label(parse_time(s["start_time"]))} for s in slots]


def book(start_time_utc: str, full_name: str, email: str, call_id: str, visit_type: str,
         site_address: str | None = None, phone: str | None = None) -> dict:
    """POST /v2/bookings -> normalised booking dict. Raises HttpError on failure."""
    attendee = {"name": full_name, "email": email, "timeZone": "Asia/Kolkata", "language": "en"}
    if phone and phone.startswith("+"):
        attendee["phoneNumber"] = phone
    body = {"start": start_time_utc, "eventTypeId": int(config.CALCOM_EVENT_TYPE_ID), "attendee": attendee,
            "metadata": {"call_id": call_id, "visit_type": visit_type, "source": "aangan_phone_agent"}}
    if visit_type == "site_visit" and site_address:
        body["location"] = {"type": "attendeeAddress", "address": site_address}
    elif visit_type == "studio":
        body["location"] = {"type": "address"}                       # the studio address set on the event type
    data = request_json("POST", f"{API}/bookings", _h(V_BOOKINGS), body).get("data") or {}
    hosts = data.get("hosts") or []
    uid = data.get("uid")
    return {"ref": f"calcom:{uid}", "event": uid, "start": data.get("start") or start_time_utc,
            "designer_email": hosts[0].get("email") if hosts else None,
            "designer_name": hosts[0].get("name") if hosts else None,
            "cancel_url": f"{APP}/booking/{uid}?cancel=true" if uid else None,
            "reschedule_url": f"{APP}/reschedule/{uid}" if uid else None}


def is_slot_problem(err: HttpError) -> bool:
    return 400 <= err.status < 500 and err.status not in (401, 403)


# --- webhooks -------------------------------------------------------------------------------

def verify_signature(raw_body: bytes, header: str | None, secret: str | None = None) -> bool:
    secret = secret or config.CALCOM_WEBHOOK_SECRET
    if not secret or not header:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def sign(raw_body: bytes, secret: str) -> str:
    """For tests / local simulation."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def _response_value(r):
    """Cal.com booking-form answers come as {"value": …} or a bare value."""
    return r.get("value") if isinstance(r, dict) else r


def booking_from_payload(trigger: str, p: dict) -> dict:
    """Normalise a Cal.com webhook payload into the shared booking shape (see actions.apply_booking_event)."""
    if trigger == "BOOKING_NO_SHOW_UPDATED":
        uid = p.get("bookingUid")
        attendees = p.get("attendees") or []
        return {"ref": f"calcom:{uid}" if uid else None, "no_show": any(a.get("noShow") for a in attendees)}
    uid = p.get("uid")
    org = p.get("organizer") or {}
    att = (p.get("attendees") or [{}])[0]
    meta = p.get("metadata") or {}
    old = p.get("rescheduleUid")
    responses = p.get("responses") or {}
    phone = att.get("phoneNumber") or _response_value(responses.get("attendeePhoneNumber"))
    # Where the consultation is: the caller's address = site visit; the event's own address = the studio.
    loc = responses.get("location") or {}
    loc_type = (loc.get("value") if isinstance(loc.get("value"), str) else "") if isinstance(loc, dict) else ""
    visit_type, site_address = None, None
    if loc_type == "attendeeAddress":
        visit_type, site_address = "site_visit", (loc.get("optionValue") if isinstance(loc, dict) else None) or p.get("location")
    elif loc_type in ("inPerson", "address"):
        visit_type = "studio"
    return {
        "invitee_phone": phone,
        "visit_type": visit_type,
        "site_address": site_address,
        "call_id": meta.get("call_id"),
        "ref": f"calcom:{uid}" if uid else None,
        "old_ref": f"calcom:{old}" if old else None,
        "event": uid,
        "invitee_email": att.get("email"),
        "invitee_name": att.get("name"),
        "slot_start": p.get("startTime"),
        "designer_email": org.get("email"),
        "designer_name": org.get("name"),
        "cancel_url": f"{APP}/booking/{uid}?cancel=true" if uid else None,
        "reschedule_url": f"{APP}/reschedule/{uid}" if uid else None,
    }
