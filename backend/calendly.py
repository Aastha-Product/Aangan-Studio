"""Calendly: open slots, booking on the call (Scheduling API), webhook verification + parsing.

Verified against developer.calendly.com (Oct 2026):
- GET  /event_type_available_times?event_type&start_time&end_time  (window <= 31 days, UTC)
- POST /invitees  {event_type, start_time, invitee{name,email,timezone}, tracking{utm_content}}  — paid plans only
- Webhook header "Calendly-Webhook-Signature: t=<unix>,v1=<hex hmac_sha256(key, f'{t}.{raw_body}')>", 3-min tolerance
Webhook *payload* field names follow Calendly's documented invitee resource; verify against the first live payload.
"""
import hashlib
import hmac
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import config
from .http import HttpError, request_json

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _headers():
    return {"Authorization": f"Bearer {config.CALENDLY_TOKEN}"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def spoken_label(dt: datetime) -> str:
    d = dt.astimezone(config.IST)
    hour = d.strftime("%I").lstrip("0")
    minute = "" if d.minute == 0 else d.strftime(":%M")
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}, {hour}{minute} {d.strftime('%p').lower()}"


def get_available_times(start: datetime, days: int | None = None) -> list[dict]:
    days = min(days or config.SLOT_LOOKAHEAD_DAYS, 31)
    end = start + timedelta(days=days)
    q = urllib.parse.urlencode({"event_type": config.CALENDLY_EVENT_TYPE_URI,
                                "start_time": _iso(start), "end_time": _iso(end)})
    data = request_json("GET", f"{config.CALENDLY_API}/event_type_available_times?{q}", _headers())
    return [s for s in data.get("collection", []) if s.get("status") == "available"]


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def filter_slots(slots: list[dict], preference: str | None) -> list[dict]:
    """Best-effort filter on a spoken preference like 'Saturday morning' or 'weekend'."""
    if not preference:
        return slots
    p = preference.lower()
    out = slots
    days = [i for i, w in enumerate(_WEEKDAYS) if w in p]
    if "weekend" in p:
        days += [5, 6]
    if "weekday" in p:
        days += [0, 1, 2, 3, 4]
    if days:
        out = [s for s in out if parse_time(s["start_time"]).astimezone(config.IST).weekday() in days]
    hours = (range(0, 12) if "morning" in p else range(12, 17) if "afternoon" in p
             else range(17, 24) if "evening" in p else None)
    if hours:
        out = [s for s in out if parse_time(s["start_time"]).astimezone(config.IST).hour in hours]
    return out or slots


def open_slots(preference: str | None = None, limit: int = 4, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    slots = filter_slots(get_available_times(now + timedelta(hours=2)), preference)[:limit]
    return [{"id": s["start_time"], "label": spoken_label(parse_time(s["start_time"]))} for s in slots]


def book(start_time_utc: str, full_name: str, email: str, call_id: str) -> dict:
    """POST /invitees. Returns the Invitee resource. Raises HttpError on failure."""
    body = {
        "event_type": config.CALENDLY_EVENT_TYPE_URI,
        "start_time": start_time_utc,
        "invitee": {"name": full_name, "email": email, "timezone": "Asia/Kolkata"},
        "tracking": {"utm_source": "aangan_phone_agent", "utm_medium": "phone", "utm_content": call_id},
    }
    data = request_json("POST", f"{config.CALENDLY_API}/invitees", _headers(), body)
    return data.get("resource", data)


def is_slot_problem(err: HttpError) -> bool:
    """4xx other than auth/plan errors usually means the time is no longer bookable."""
    return 400 <= err.status < 500 and err.status not in (401, 403)


# --- webhooks -------------------------------------------------------------------------

def verify_signature(raw_body: bytes, header: str | None, key: str | None = None,
                     tolerance_sec: int = 180, now: float | None = None) -> bool:
    key = key or config.CALENDLY_SIGNING_KEY
    if not key or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1 or not t.isdigit():
        return False
    if abs((now or time.time()) - int(t)) > tolerance_sec:
        return False
    expected = hmac.new(key.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def sign(raw_body: bytes, key: str, t: int | None = None) -> str:
    """For tests / local simulation."""
    t = t or int(time.time())
    return f"t={t},v1={hmac.new(key.encode(), f'{t}.'.encode() + raw_body, hashlib.sha256).hexdigest()}"


def booking_from_payload(payload: dict) -> dict:
    """Pull what we store from an invitee.created / invitee.canceled payload."""
    ev = payload.get("scheduled_event") or {}
    members = ev.get("event_memberships") or []
    tracking = payload.get("tracking") or {}
    return {
        "call_id": tracking.get("utm_content"),
        "invitee_uri": payload.get("uri"),
        "event_uri": ev.get("uri") or payload.get("event"),
        "invitee_email": payload.get("email"),
        "invitee_name": payload.get("name"),
        "slot_start": ev.get("start_time"),
        "designer_email": members[0].get("user_email") if members else None,
        "designer_name": members[0].get("user_name") if members else None,
        "cancel_url": payload.get("cancel_url"),
        "reschedule_url": payload.get("reschedule_url"),
        "rescheduled": bool(payload.get("rescheduled")),
        "old_invitee": payload.get("old_invitee"),
        "new_invitee": payload.get("new_invitee"),
    }
