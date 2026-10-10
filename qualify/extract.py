"""Field extraction: transcript -> structured facts with verbatim quotes (Gemini Flash).

Results are cached in out/extractions/<call_id>.json so the rules can be re-run for free.
"""
import json
from datetime import datetime, timezone

from .env import ROOT
from .gemini import generate_json
from .transcripts import Call

PROMPT_PATH = ROOT / "prompts" / "extract_fields.md"
CACHE_DIR = ROOT / "out" / "extractions"


def _s(enum=None, nullable=False):
    d = {"type": "STRING", "nullable": nullable}
    if enum:
        d["enum"] = enum
    return d


B = {"type": "BOOLEAN"}
N = {"type": "NUMBER", "nullable": True}
Q = _s(nullable=True)  # a verbatim quote

FIELDS = {
    "has_conversation": B,
    "caller_name": _s(nullable=True),
    "existing_client_issue": B, "existing_client_quote": Q,
    "returning_caller_let_down": B, "returning_caller_quote": Q,
    "property_category": _s(["residential", "office_clinic_studio", "restaurant_hotel_hospitality",
                             "retail", "gym", "other_commercial", "unknown"]),
    "property_description": _s(nullable=True),
    "scope_unit": _s(["full_home", "floor_or_multi_room", "single_room", "single_item", "full_office", "unknown"]),
    "rooms": {"type": "ARRAY", "items": {"type": "STRING"}}, "rooms_quote": Q,
    "size_sqft": N, "size_quote": Q,
    "service_wanted": _s(["design_and_execution", "advice_or_ideas_only", "diy_execution",
                          "furniture_sourcing_only", "vastu_only", "structural_only", "unknown"]),
    "service_quote": Q,
    "just_exploring": B, "exploring_quote": Q,
    "location_text": _s(nullable=True),
    "location_city": _s(["pune", "pcmc", "other_city", "unknown"]), "location_quote": Q,
    "rented": B, "structural_change_requested": B,
    "current_state": _s(["bare_shell", "builder_finished", "lived_in", "unknown"]),
    "timeline_discussed": B, "timeline_text": _s(nullable=True), "timeline_quote": Q,
    "weeks_until_needed": N,
    "deadline_driver": _s(["move_in", "possession", "go_live", "festival_or_event", "preference_only", "none"]),
    "weeks_until_site_available": N,
    "open_to_later_start": B,
    "wants_meeting_within_2_weeks": B, "meeting_quote": Q,
    "budget_volunteered": B, "budget_max_inr": N, "budget_quote": Q,
    "decision_maker": _s(["owner_or_authorised", "deciders_will_attend", "researching_no_authority", "unknown"]),
    "decision_maker_quote": Q,
    "source": _s(["named_referral", "online", "unknown"]),
    "referrer_name": _s(nullable=True), "source_quote": Q,
    "booking_response": _s(["agreed", "thinking", "declined", "not_offered"]), "booking_quote": Q,
    "readiness": _s(["clear_target_date", "vague_date", "just_exploring", "not_discussed"]), "readiness_quote": Q,
    "asked_for_price": B, "price_ask_count": {"type": "INTEGER"}, "price_quote": Q,
    "wants_site_visit": B,
    "call_dropped_and_returned": B,
    # web calls carry no caller ID, so the callback details come from what the caller said
    "caller_phone": _s(nullable=True), "caller_email": _s(nullable=True),
}

SCHEMA = {"type": "OBJECT", "properties": FIELDS, "required": list(FIELDS), "propertyOrdering": list(FIELDS)}


def build_prompt(call: Call) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return (template
            .replace("{{call_date}}", call.call_date.strftime("%A %d %B %Y"))
            .replace("{{transcript}}", call.text))


def extract_fields(call: Call, refresh: bool = False, use_cache: bool = True) -> dict:
    """Returns {"fields": {...}, "usage": {...}, "extracted_at": iso}.

    use_cache=False in production (serverless file systems are read-only)."""
    cache = CACHE_DIR / f"{call.call_id}.json"
    if use_cache and cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    fields, usage = generate_json(build_prompt(call), SCHEMA)
    record = {
        "call_id": call.call_id,
        "fields": fields,
        "usage": usage,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record
