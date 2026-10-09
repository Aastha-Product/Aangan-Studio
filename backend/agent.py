"""The phone agent's brain: one CallSession per call (build steps 5–6).

Transport-agnostic: the Vaani BYOL bridge (voice_server/), the terminal simulator (scripts/talk.py) and the
red-team test (scripts/redteam_price.py) all drive the same CallSession:

    s = CallSession(call_id, caller_number)
    say(s.opening)
    while not s.ended:
        say(s.caller_says(heard_text))      # every reply has already passed the speech guard
    s.finish()                              # post-call: qualify, log, report card / digest / alerts

Claude runs the conversation (manual tool loop so every reply can be guarded before it is spoken).
"""
import json
import re
from datetime import datetime, timedelta

from . import actions, booking, calendly, config
from .http import HttpError
from .speech_guard import guard
from .store import default_store, utcnow
from qualify.budget import check_budget_fit

PROMPT_PATH = config.ROOT / "prompts" / "system_prompt.md"
SYSTEM_PROMPT = re.sub(r"<!--.*?-->", "", PROMPT_PATH.read_text(encoding="utf-8"), flags=re.S).strip()

SYSTEM_MESSAGE_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1",
                         "claude-opus-5", "claude-opus-4-8"}
MAX_TOOL_ROUNDS = 6
REFUSAL_LINE = "I'm sorry, I can't help with that on this call. Is there anything about your interior project I can help with?"
GOODBYE = "Thank you for calling Aangan Studio. Goodbye."
OPENING = "Hello, Aangan Studio. This is the studio's virtual assistant — how can I help you?"

TOOLS = [
    {"name": "get_open_slots",
     "description": "Get the next open consultation slots with a designer. Returns slots with an id and a spoken label.",
     "input_schema": {"type": "object", "properties": {
         "preferred_day_or_time": {"type": "string", "description": "Caller's preference, e.g. 'Saturday morning'. Omit if none."}},
         "required": []}},
    {"name": "book_consultation",
     "description": "Book the consultation slot the caller chose. Only after the email has been read back and confirmed.",
     "input_schema": {"type": "object", "properties": {
         "slot_id": {"type": "string", "description": "The id of the chosen slot from get_open_slots."},
         "full_name": {"type": "string"},
         "email": {"type": "string", "description": "Confirmed email address."},
         "visit_type": {"type": "string", "enum": ["site_visit", "studio"]},
         "site_address": {"type": "string", "description": "For a site visit: the site address as the caller gave it."}},
         "required": ["slot_id", "full_name", "email", "visit_type"]}},
    {"name": "mark_booking_pending",
     "description": "The caller is right for us but no booking could be made (no email, or booking failed twice). Alerts the front desk.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "check_budget_fit",
     "description": "Use ONLY when the caller volunteered a budget. Returns aligned, misaligned or unclear.",
     "input_schema": {"type": "object", "properties": {
         "budget_max_inr": {"type": "number", "description": "Top of the caller's figure in rupees, e.g. 150000 for '1 to 1.5 lakh'."},
         "property_category": {"type": "string", "enum": ["residential", "office_clinic_studio"]},
         "scope_unit": {"type": "string", "enum": ["full_home", "floor_or_multi_room", "single_room", "single_item", "full_office"]},
         "rooms": {"type": "array", "items": {"type": "string"}},
         "size_sqft": {"type": "number", "description": "Carpet area if stated."}},
         "required": ["budget_max_inr", "property_category", "scope_unit", "rooms"]}},
    {"name": "escalate_to_studio_head",
     "description": "Existing client, complaint, or a question you cannot answer. Sends an instant alert.",
     "input_schema": {"type": "object", "properties": {
         "caller_name": {"type": "string"}, "project": {"type": "string"}, "issue_one_line": {"type": "string"}},
         "required": ["caller_name", "project", "issue_one_line"]}},
    {"name": "end_call", "description": "Hang up after you have said goodbye.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]


# --- per-call details (kept out of the cached system prompt) -------------------------------------

def _is_working_day(d) -> bool:
    return d.weekday() < 5   # front desk Mon–Fri (W05: "closed over the weekend")


def next_working_morning(now: datetime) -> str:
    if _is_working_day(now) and now.time() < config.STUDIO_OPENS:
        return "this morning"
    d = now.date() + timedelta(days=1)
    while not _is_working_day(d):
        d += timedelta(days=1)
    return "tomorrow morning" if d == now.date() + timedelta(days=1) else f"on {d.strftime('%A')} morning"


def call_details(now: datetime, caller_number: str | None) -> dict:
    greeting = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"
    in_hours = _is_working_day(now) and config.STUDIO_OPENS <= now.time() < config.STUDIO_CLOSES
    nwm = next_working_morning(now)
    return {"greeting": greeting, "next_working_morning": nwm,
            "senior_callback_promise": "within 15 minutes" if in_hours else f"first thing {nwm}",
            "now": now.strftime("%A %d %B %Y, %I:%M %p IST"), "caller_number": caller_number or "unknown"}


def details_block(d: dict) -> str:
    return ("Call details\n"
            f"- Now: {d['now']}\n- Caller's number: {d['caller_number']}\n"
            f"- [next working morning]: {d['next_working_morning']}\n"
            f"- [senior callback promise]: {d['senior_callback_promise']}")


# --- tools -----------------------------------------------------------------------------------------

class AgentTools:
    def __init__(self, session: "CallSession"):
        self.s = session
        self.booking_failures = 0
        self.slots: dict[str, str] = {}

    def run(self, name: str, args: dict) -> tuple[dict, bool]:
        fn = getattr(self, f"t_{name}", None)
        if not fn:
            return {"error": f"unknown tool {name}"}, True
        try:
            return fn(**(args or {})), False
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}, True
        except Exception as e:  # noqa: BLE001 — a tool failure must not drop the call
            self.s.store.log_event(self.s.call_id, "tool_error", {"tool": name, "error": str(e)[:300]})
            return {"error": "temporary problem, apologise and continue"}, True

    def t_get_open_slots(self, preferred_day_or_time: str | None = None):
        slots = booking.open_slots(preferred_day_or_time)
        self.slots.update({s["id"]: s["label"] for s in slots})
        return {"slots": slots} if slots else {"slots": [], "note": "no open slots; use mark_booking_pending"}

    def t_book_consultation(self, slot_id: str, full_name: str, email: str, visit_type: str,
                            site_address: str | None = None):
        if not calendly.EMAIL_RE.match(email.strip()):
            return {"ok": False, "reason": "invalid_email", "note": "ask for the email again and read it back"}
        number = self.s.caller_number or (self.s.store.get_call(self.s.call_id) or {}).get("caller_number")
        try:
            b = booking.book(slot_id, full_name.strip(), email.strip(), self.s.call_id, visit_type,
                             (site_address or "").strip() or None, number)
        except HttpError as e:
            self.booking_failures += 1
            self.s.store.log_event(self.s.call_id, "booking_failed", {"status": e.status, "body": str(e.body)[:300],
                                                                     "provider": booking.provider()})
            return {"ok": False, "reason": "slot_taken" if booking.is_slot_problem(e) else "error",
                    "failures_so_far": self.booking_failures}
        label = self.slots.get(slot_id) or calendly.spoken_label(calendly.parse_time(slot_id))
        self.s.store.update_call(self.s.call_id, {
            "status": "booked", "booked_on_call": True, "booked_at": utcnow(), "slot_start": slot_id,
            "invitee_uri": b.get("ref"), "event_uri": b.get("event"),
            "cancel_url": b.get("cancel_url"), "reschedule_url": b.get("reschedule_url"),
            "designer_email": b.get("designer_email"), "designer_name": b.get("designer_name"),
            "invitee_email": email.strip(), "invitee_name": full_name.strip(), "caller_name": full_name.strip(),
            "visit_type": visit_type, "site_address": (site_address or "").strip() or None,
            "booking_provider": booking.provider()})
        where = "a site visit" if visit_type == "site_visit" else "at the studio"
        return {"ok": True, "spoken_confirmation": f"{label}, {where}"}

    def t_mark_booking_pending(self, reason: str):
        actions.booking_pending(self.s.store, self.s.call_id, reason)
        return {"ok": True, "say": f"the front desk will call {self.s.details['next_working_morning']} to confirm a time"}

    def t_check_budget_fit(self, budget_max_inr, property_category, scope_unit, rooms, size_sqft=None):
        return {"result": check_budget_fit(budget_max_inr, property_category, scope_unit, rooms or [], size_sqft)}

    def t_escalate_to_studio_head(self, caller_name: str, project: str, issue_one_line: str):
        number = self.s.caller_number or (self.s.store.get_call(self.s.call_id) or {}).get("caller_number")
        actions.escalate(self.s.store, self.s.call_id, number, caller_name, project, issue_one_line)
        return {"ok": True, "promise": self.s.details["senior_callback_promise"]}

    def t_end_call(self):
        self.s.ended = True
        return {"ok": True}


# --- session ---------------------------------------------------------------------------------------

class CallSession:
    def __init__(self, call_id: str, caller_number: str | None = None, store=None, client=None,
                 model: str | None = None, effort: str | None = None, now: datetime | None = None,
                 opening: str | None = None):
        self.call_id, self.caller_number = call_id, caller_number
        self.store = store or default_store()
        self.model = model or config.CLAUDE_MODEL
        self.effort = effort or config.CLAUDE_EFFORT
        self._client = client
        self.started = now or config.now_ist()
        self.details = call_details(self.started, caller_number)
        # Vaani speaks the agent's fixed greeting; we record what it actually said (same text locally).
        self.opening = opening or OPENING
        self.messages: list[dict] = []
        self.transcript: list[tuple[str, str]] = [("Agent", self.opening)]
        self.tools = AgentTools(self)
        self.ended = False
        self.blocks = 0
        self._pending_note: str | None = None
        self.usage = {"model": self.model, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        t = self.started.time()
        existing = self.store.get_call(call_id) or {}   # Vaani's call_started webhook may have arrived first
        self.store.upsert_call({"call_id": call_id, "caller_number": caller_number or existing.get("caller_number"),
                                "started_at": existing.get("started_at") or self.started.isoformat(),
                                "answered_at": existing.get("answered_at") or self.started.isoformat(),
                                "after_hours": not (config.STUDIO_OPENS <= t < config.STUDIO_CLOSES), "answered": True})

    @property
    def client(self):
        if self._client is None:
            import anthropic  # only the voice host needs the SDK
            self._client = anthropic.Anthropic()
        return self._client

    def _create(self):
        kwargs = dict(
            model=self.model, max_tokens=4096, tools=TOOLS, messages=self.messages,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": details_block(self.details)}],
            output_config={"effort": self.effort},
        )
        if self.model in config.FALLBACK_MODELS:
            return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        return self.client.messages.create(**kwargs)

    def _add_usage(self, u):
        if not u:
            return
        self.usage["input"] += getattr(u, "input_tokens", 0) or 0
        self.usage["output"] += getattr(u, "output_tokens", 0) or 0
        self.usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

    def caller_says(self, text: str) -> str:
        """Returns what the agent will say next (already through the speech guard)."""
        self.transcript.append(("Caller", text))
        note, self._pending_note = self._pending_note, None
        if note and self.model not in SYSTEM_MESSAGE_MODELS:
            text = f"[Operator note: {note}]\n{text}"
            note = None
        self.messages.append({"role": "user", "content": text})
        if note:
            self.messages.append({"role": "system", "content": note})

        spoken: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self._create()
            self._add_usage(getattr(resp, "usage", None))
            self.messages.append({"role": "assistant", "content": resp.content})  # append-only history
            if resp.stop_reason == "refusal":
                spoken = [REFUSAL_LINE]
                break
            spoken += [b.text for b in resp.content if getattr(b, "type", "") == "text" and b.text.strip()]
            tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                break
            results = []
            for tu in tool_uses:
                result, is_error = self.tools.run(tu.name, tu.input)
                self.store.log_event(self.call_id, "tool_call", {"tool": tu.name, "input": tu.input, "result": result})
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(result),
                                **({"is_error": True} if is_error else {})})
            self.messages.append({"role": "user", "content": results})
            if self.ended and spoken:
                break

        reply = " ".join(spoken).strip() or (GOODBYE if self.ended else "Sorry, could you say that again?")
        g = guard(reply)
        if g.blocked:
            self.blocks += 1
            self.store.log_event(self.call_id, "speech_guard_block", {"original": g.original, "reasons": g.reasons})
            self._pending_note = ("Your last reply was blocked by the price filter because it contained a price. "
                                  "The caller heard the approved pricing line instead. Never state prices.")
        self.transcript.append(("Agent", g.text))
        return g.text

    def transcript_text(self) -> str:
        return "\n".join(f"{who}: {text}" for who, text in self.transcript)

    def finish(self, process: bool = True) -> dict | None:
        ended = config.now_ist()
        row = self.store.get_call(self.call_id) or {}
        usage = dict(row.get("usage") or {})
        usage["claude"] = self.usage
        self.store.update_call(self.call_id, {
            "transcript": self.transcript_text(), "ended_at": ended.isoformat(),
            "duration_sec": int((ended - self.started).total_seconds()), "usage": usage})
        if not process:
            return self.store.get_call(self.call_id)
        # Claim first, so Vaani's call_postprocessing webhook can't qualify the same call again.
        self.store.update_call(self.call_id, {"processing_started_at": utcnow()})
        try:
            return actions.process_completed_call(self.store, self.call_id, self.transcript_text(), self.started)
        except Exception as e:  # noqa: BLE001 — keep the raw call even if qualification fails
            self.store.log_event(self.call_id, "post_call_failed", {"error": str(e)[:300]})
            return self.store.get_call(self.call_id)
