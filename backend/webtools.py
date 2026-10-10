"""Booking tools the Vaani agent calls during a web call (Vaani "custom tools", hosted here).

POST /api/tools/slots     find open consultation times          (call_ref, preference?)
POST /api/tools/book      book one                              (call_ref, slot_id, full_name, email, visit_type, site_address?, phone?)
POST /api/tools/pending   the caller is right for us but no slot got booked (call_ref, reason)

No shared secret is needed. Each web call gets its own short, random `call_ref` (backend/webcall.py). Vaani fills it into
the agent's instructions, the agent passes it back, and these tools only act for a call that holds one: a web call that
is still open (not yet processed), started within the last two hours. A made-up or old reference does nothing.

The tools reuse AgentTools (backend/agent.py), the same booking code the Claude voice bridge uses.
"""
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import agent, calendly, config
from .store import utcnow

MAX_AGE = timedelta(hours=2)
MAX_TOOL_CALLS = 40                                   # per call: a runaway agent can't hammer the calendar
TOOLS = ("slots", "book", "pending")
_WRAPPERS = ("parameters", "arguments", "args", "params", "input", "data", "body")


def parse_params(raw: bytes, query: dict) -> dict:
    """Parameters from a JSON body (possibly wrapped), a form body, or the query string. Keys lower-cased."""
    body: dict = {}
    text = (raw or b"").decode("utf-8", "replace").strip()
    if text:
        try:
            body = json.loads(text)
        except ValueError:
            body = {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}
    if not isinstance(body, dict):
        body = {}
    for key in _WRAPPERS:
        inner = body.get(key)
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                inner = None
        if isinstance(inner, dict):
            body = {**body, **inner}
    for k, v in (query or {}).items():
        body.setdefault(k, v[0] if isinstance(v, list) else v)
    return {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in body.items()}


def _call_for(store, ref) -> dict | None:
    ref = re.sub(r"[^a-z0-9]", "", str(ref or "").lower())
    if len(ref) != 8:
        return None
    row = store.find_call("call_ref", ref)
    if not row or row.get("channel") != "web" or row.get("processed_at"):
        return None
    t = row.get("started_at") or row.get("created_at")
    if not t or datetime.now(timezone.utc) - calendly.parse_time(t) > MAX_AGE:
        return None
    return row


def _e164(phone) -> str | None:
    s = str(phone or "")
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    return "+" + digits if s.strip().startswith("+") and 8 <= len(digits) <= 15 else None


def _visit_type(v) -> str | None:
    s = str(v or "").lower()
    if any(w in s for w in ("site", "home", "house", "flat", "visit", "address", "property")):
        return "site_visit"
    if any(w in s for w in ("studio", "office", "your place", "come to")):
        return "studio"
    return None


def _resolve_slot(store, call_id: str, slot) -> str | None:
    """The agent passes back what it was given (an ISO time), but be forgiving: an option number or the spoken label."""
    s = str(slot or "").strip()
    if not s:
        return None
    try:
        dt = calendly.parse_time(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=config.IST)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    offered = next((e["payload"].get("slots") for e in reversed(store.list_call_events(call_id))
                    if e.get("kind") == "slots_offered" and isinstance(e.get("payload"), dict)), None) or []
    m = re.fullmatch(r"(?:option|slot|number)?\s*(\d)", s.lower())
    if m and 1 <= int(m.group(1)) <= len(offered):
        return offered[int(m.group(1)) - 1]["id"]
    norm = re.sub(r"[^a-z0-9]", "", s.lower())
    for o in offered:
        if norm and norm == re.sub(r"[^a-z0-9]", "", o["label"].lower()):
            return o["id"]
    return None


_UNKNOWN = {"ok": False, "reason": "unknown_call",
            "say": "Sorry, I can't reach the booking system for this call. I'll take your name, email and mobile number, "
                   "and our front desk will call you to confirm a time."}


def handle(store, tool: str, params: dict) -> tuple[str, dict]:
    if tool not in TOOLS:
        return "404 Not Found", {"ok": False, "error": "unknown tool"}
    row = _call_for(store, params.get("call_ref"))
    if not row:
        return "200 OK", _UNKNOWN
    call_id = row["call_id"]
    if len([e for e in store.list_call_events(call_id) if e.get("kind") == "tool_call"]) >= MAX_TOOL_CALLS:
        return "429 Too Many Requests", {"ok": False, "reason": "too_many_requests"}

    s = agent.CallSession(call_id, row.get("caller_number"), store=store)
    try:
        out = _run(store, s, row, tool, params)
    except Exception as e:  # noqa: BLE001 — a tool problem must never break the call
        store.log_event(call_id, "tool_error", {"tool": tool, "error": str(e)[:300]})
        out = {"ok": False, "reason": "error", "say": "Sorry, something went wrong on our side. I'll note your details "
                                                      "and our front desk will call you to confirm a time."}
    store.log_event(call_id, "tool_call", {"tool": tool, "ok": bool(out.get("ok", True)), "keys": sorted(params)[:12]})
    return "200 OK", out


def _run(store, s, row: dict, tool: str, p: dict) -> dict:
    call_id = row["call_id"]
    if tool == "slots":
        res, err = s.tools.run("get_open_slots", {"preferred_day_or_time": p.get("preference") or p.get("preferred_day_or_time")
                                                  or p.get("when") or None})
        slots = (res or {}).get("slots") or []
        store.log_event(call_id, "slots_offered", {"slots": slots})
        if not slots:
            return {"ok": False, "slots": [], "say": "I don't see an open time right now. Take their details and use "
                                                     "mark_booking_pending."}
        shown = slots[:4]
        return {"ok": True, "slots": [{"option": i + 1, **o} for i, o in enumerate(shown)],
                "say": "Offer the caller two of these, in words: " + "; ".join(o["label"] for o in shown[:2])}

    if tool == "pending":
        res, err = s.tools.run("mark_booking_pending", {"reason": str(p.get("reason") or "caller needs a callback to confirm a time")[:300]})
        return {**res, "ok": not err}

    # tool == "book"
    if row.get("status") == "booked":
        return {"ok": False, "reason": "already_booked",
                "say": f"This caller is already booked ({calendly.spoken_label(calendly.parse_time(row['slot_start']))})."
                if row.get("slot_start") else "This caller is already booked."}
    name, email = str(p.get("full_name") or p.get("name") or "").strip(), str(p.get("email") or "").strip()
    visit = _visit_type(p.get("visit_type") or p.get("where") or p.get("location_type"))
    address = str(p.get("site_address") or p.get("address") or "").strip()
    slot = _resolve_slot(store, call_id, p.get("slot_id") or p.get("slot") or p.get("time"))
    if not name:
        return {"ok": False, "reason": "need_name", "say": "Ask for the caller's full name."}
    if not calendly.EMAIL_RE.match(email):
        return {"ok": False, "reason": "invalid_email", "say": "That email doesn't look right. Ask for it again and read it back letter by letter."}
    if not visit:
        return {"ok": False, "reason": "need_visit_type", "say": "Ask: a site visit, or at the studio?"}
    if visit == "site_visit" and not address:
        return {"ok": False, "reason": "need_address", "say": "Ask for the full site address."}
    if not slot:
        return {"ok": False, "reason": "unknown_slot", "say": "Call get_open_slots again and offer one of the times it returns."}
    phone = _e164(p.get("phone") or p.get("mobile") or p.get("phone_number"))
    if phone:
        store.update_call(call_id, {"caller_number": phone})
        s.caller_number = phone
    res, err = s.tools.run("book_consultation", {"slot_id": slot, "full_name": name, "email": email, "visit_type": visit,
                                                 "site_address": address or None})
    if err or not res.get("ok"):
        return {**res, "ok": False, "say": "Apologise and offer another time once; if that fails too, use mark_booking_pending."}
    return {**res, "ok": True, "say": f"Booked: {res.get('spoken_confirmation')}. Read this back, and say a confirmation email "
                                      "and calendar invite are on the way."}
