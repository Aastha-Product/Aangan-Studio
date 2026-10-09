"""Deterministic qualification rules (build step 2).

Input: the fields extracted from a transcript (see extract.py SCHEMA) + the transcript text.
Output: decision, priority, reason code, per-gate results, interest score with verified quotes, flags.

Every rule here comes from CLAUDE.md / context/qualified.md / context/services.md.
Do not change a rule without asking Aastha (and updating CLAUDE.md).
"""
from dataclasses import dataclass, field, asdict

from .budget import check_budget_fit
from .quotes import quote_in_transcript

# --- services.md -------------------------------------------------------------------
PUNE_LOCALITIES = [
    "pune", "kothrud", "baner", "aundh", "wakad", "koregaon park", "kalyani nagar", "viman nagar",
    "hadapsar", "magarpatta", "nibm", "kondhwa", "undri", "shivane", "warje", "erandwane", "deccan",
]
PCMC_LOCALITIES = [
    "pcmc", "pimpri", "chinchwad", "pimple saudagar", "pimple nilakh", "ravet", "hinjewadi",
]
OUT_OF_AREA_PLACES = ["talegaon", "lonavala", "nashik", "mumbai"]

MIN_LEAD_WEEKS = 6              # "cannot begin execution on a project that needs to be ready in under 6 weeks"
MAX_WEEKS_TO_SITE = 10          # qualified.md: site available for execution within 8–10 weeks of consultation
TIGHT_TIMELINE_WEEKS = 12       # design 3–4 wks + execution 8+ wks: flag (not fail) anything sooner
OFFICE_MAX_SQFT = 3000          # "Office: up to ~3,000 sq ft"
OFFICE_MIN_SQFT = 500           # PROVISIONAL — front desk in T18; not in services.md. Pending Nikhil.

# Primary reason code when more than one gate fails (most fundamental first).
REASON_ORDER = [
    "OUT_OF_AREA", "OUT_OF_SCOPE_TYPE", "ADVICE_ONLY", "TOO_SMALL",
    "TIMELINE_IMPOSSIBLE", "BUDGET_MISALIGNED", "NOT_DECISION_MAKER",
]

FIXED_DATE_DRIVERS = {"move_in", "possession", "go_live"}


@dataclass
class Gate:
    number: int
    name: str
    status: str                 # pass | fail | unclear
    reason_code: str = ""
    note: str = ""


@dataclass
class ScoreLine:
    factor: str
    points: int
    max_points: int
    quote: str
    note: str = ""


@dataclass
class Result:
    call_id: str
    decision: str               # Qualified | Nurture | Not qualified | Escalate | No data
    priority: str = ""          # P1 | P2 (Qualified only)
    reason_code: str = ""
    all_fail_codes: list[str] = field(default_factory=list)
    priority_reasons: list[str] = field(default_factory=list)
    gates: list[Gate] = field(default_factory=list)
    score: int = 0
    score_lines: list[ScoreLine] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --- gates ----------------------------------------------------------------------------

def gate_scope(f: dict) -> Gate:
    g = Gate(1, "Real project, in scope", "pass")
    category, service, unit = f.get("property_category"), f.get("service_wanted"), f.get("scope_unit")
    size = f.get("size_sqft")

    if service in ("advice_or_ideas_only", "diy_execution"):
        return Gate(1, g.name, "fail", "ADVICE_ONLY", f"wants {service.replace('_', ' ')}")
    if category in ("restaurant_hotel_hospitality", "retail", "gym"):
        return Gate(1, g.name, "fail", "OUT_OF_SCOPE_TYPE", f"{category.replace('_', ' ')} is out of scope")
    if service in ("furniture_sourcing_only", "vastu_only", "structural_only"):
        return Gate(1, g.name, "fail", "OUT_OF_SCOPE_TYPE", f"{service.replace('_', ' ')} is not offered")
    if category == "office_clinic_studio" and size:
        if size > OFFICE_MAX_SQFT:
            return Gate(1, g.name, "fail", "OUT_OF_SCOPE_TYPE", f"{size:,.0f} sq ft is above the ~{OFFICE_MAX_SQFT:,} sq ft office limit")
        if size < OFFICE_MIN_SQFT:
            return Gate(1, g.name, "fail", "TOO_SMALL",
                        f"{size:,.0f} sq ft is below the provisional {OFFICE_MIN_SQFT} sq ft office minimum (pending Nikhil)")
    if unit == "single_item":
        return Gate(1, g.name, "fail", "TOO_SMALL", "a single item, not a room redesign")
    if category in ("unknown", "other_commercial", None) and service in ("unknown", None):
        return Gate(1, g.name, "unclear", note="project type / execution not established")
    if service in ("unknown", None):
        g.note = "design + execution assumed, not confirmed"
    return g


def _place_matches(text: str, places: list[str]) -> str | None:
    t = (text or "").lower()
    return next((p for p in places if p in t), None)


def gate_area(f: dict) -> Gate:
    name = "Service area"
    loc = f.get("location_text") or ""
    out = _place_matches(loc, OUT_OF_AREA_PLACES)
    if out:
        return Gate(2, name, "fail", "OUT_OF_AREA", f"{loc} is outside Pune/PCMC")
    if _place_matches(loc, PUNE_LOCALITIES + PCMC_LOCALITIES):
        return Gate(2, name, "pass", note=loc)
    city = f.get("location_city")
    if city == "other_city":
        return Gate(2, name, "fail", "OUT_OF_AREA", f"{loc or 'location'} is outside Pune/PCMC")
    if city in ("pune", "pcmc"):
        return Gate(2, name, "pass", note=f"{loc}: not on the services.md list. Confirm area")
    return Gate(2, name, "unclear", note="location not established")


def gate_timeline(f: dict) -> Gate:
    name = "Realistic timeline"
    needed = f.get("weeks_until_needed")
    site = f.get("weeks_until_site_available")
    if needed is not None and needed < MIN_LEAD_WEEKS:
        return Gate(3, name, "fail", "TIMELINE_IMPOSSIBLE", f"needed in ~{needed:g} weeks (minimum {MIN_LEAD_WEEKS})")
    if site is not None and site > MAX_WEEKS_TO_SITE:
        return Gate(3, name, "fail", "TIMELINE_IMPOSSIBLE", f"site not available for ~{site:g} weeks (max {MAX_WEEKS_TO_SITE})")
    if not f.get("timeline_discussed"):
        return Gate(3, name, "unclear", note="timeline not asked")
    return Gate(3, name, "pass", note=f.get("timeline_text") or "")


def gate_budget(f: dict) -> Gate:
    name = "Budget band (never asked)"
    if not f.get("budget_volunteered"):
        return Gate(4, name, "pass", note="not mentioned")
    fit = check_budget_fit(f.get("budget_max_inr"), f.get("property_category"), f.get("scope_unit"),
                           f.get("rooms") or [], f.get("size_sqft"))
    if fit == "misaligned":
        return Gate(4, name, "fail", "BUDGET_MISALIGNED", "volunteered budget clearly below scope")
    return Gate(4, name, "pass", note=f"volunteered, {fit}")


def gate_decision_maker(f: dict) -> Gate:
    name = "Decision-maker"
    dm = f.get("decision_maker")
    if dm == "owner_or_authorised":
        return Gate(5, name, "pass")
    if dm == "deciders_will_attend":
        return Gate(5, name, "pass", note="decision with others. They must attend the consultation")
    if dm == "researching_no_authority":
        return Gate(5, name, "fail", "NOT_DECISION_MAKER", "researching for others with no authority")
    return Gate(5, name, "unclear", note="decision-maker not confirmed (treated as qualified)")


# --- score ----------------------------------------------------------------------------

def _line(factor, max_points, points, quote, transcript, note="") -> ScoreLine:
    if points and not quote_in_transcript(quote, transcript):
        return ScoreLine(factor, 0, max_points, quote or "", "quote not found in transcript, 0 points")
    return ScoreLine(factor, points, max_points, (quote or "") if points else "", note)


def score(f: dict, transcript: str) -> list[ScoreLine]:
    commitment = {"agreed": 30, "thinking": 10}.get(f.get("booking_response"), 0)
    readiness = {"clear_target_date": 25, "vague_date": 10}.get(f.get("readiness"), 0)

    rooms_ok = bool(f.get("rooms")) and quote_in_transcript(f.get("rooms_quote"), transcript)
    size_ok = bool(f.get("size_sqft")) and quote_in_transcript(f.get("size_quote"), transcript)
    scope_pts = 20 if rooms_ok and size_ok else 10 if rooms_ok or size_ok else 0
    scope_quote = " … ".join(q for q, ok in ((f.get("rooms_quote"), rooms_ok), (f.get("size_quote"), size_ok)) if ok)

    dm = {"owner_or_authorised": 15, "deciders_will_attend": 8}.get(f.get("decision_maker"), 0)
    src = {"named_referral": 10, "online": 5}.get(f.get("source"), 0)

    return [
        _line("Commitment", 30, commitment, f.get("booking_quote"), transcript),
        _line("Readiness", 25, readiness, f.get("readiness_quote"), transcript),
        ScoreLine("Scope clarity", scope_pts, 20, scope_quote),
        _line("Decision-maker", 15, dm, f.get("decision_maker_quote"), transcript),
        _line("Warm source", 10, src, f.get("source_quote"), transcript),
    ]


# --- flags for the designer / digest ---------------------------------------------------

def build_flags(f: dict, gates: list[Gate], after_hours: bool) -> list[str]:
    flags = []
    if f.get("source") == "named_referral" and f.get("referrer_name"):
        flags.append(f"Referral ({f['referrer_name']})")
    if f.get("asked_for_price"):
        n = f.get("price_ask_count") or 1
        flags.append(f"Asked for price{' x' + str(n) if n > 1 else ''}. Deflected, NOT quoted")
    if f.get("wants_site_visit"):
        flags.append("Wants site visit")
    if f.get("rented"):
        flags.append("Rented. Reversible fittings only" + ("; structural change mentioned!" if f.get("structural_change_requested") else ""))
    elif f.get("structural_change_requested"):
        flags.append("Mentioned structural change. We don't do structural work")
    if f.get("returning_caller_let_down"):
        flags.append("Returning caller we let down. Handle with care")
    if f.get("call_dropped_and_returned"):
        flags.append("Line dropped once and called back")
    if f.get("property_category") == "office_clinic_studio":
        flags.append("Commercial")
    if after_hours:
        flags.append("Called outside 10am–7pm")
    needed = f.get("weeks_until_needed")
    if needed is not None and MIN_LEAD_WEEKS <= needed < TIGHT_TIMELINE_WEEKS:
        flags.append(f"Tight timeline (~{needed:g} weeks)")
    for g in gates:
        worth_flagging = (g.status == "unclear"
                          or (g.number == 2 and "Confirm area" in g.note)
                          or (g.number == 5 and g.status == "pass" and g.note))
        if worth_flagging:
            flags.append(f"Gate {g.number} {g.name}: {g.note}")
    return flags


# --- routing ---------------------------------------------------------------------------

def evaluate(call_id: str, f: dict, transcript: str, after_hours: bool = False) -> Result:
    if not f.get("has_conversation"):
        return Result(call_id, "No data", reason_code="NO_INFO",
                      flags=["No conversation. The agent would have answered this call"])

    if f.get("existing_client_issue"):
        return Result(call_id, "Escalate", reason_code="EXISTING_CLIENT_COMPLAINT",
                      flags=["Existing client. Instant alert to studio head; senior callback promised"])

    gates = [gate_scope(f), gate_area(f), gate_timeline(f), gate_budget(f), gate_decision_maker(f)]
    fails = sorted({g.reason_code for g in gates if g.status == "fail"}, key=REASON_ORDER.index)
    lines = score(f, transcript)
    res = Result(call_id, "", gates=gates, all_fail_codes=fails,
                 score_lines=lines, score=sum(l.points for l in lines),
                 flags=build_flags(f, gates, after_hours))

    if fails:
        # One clear fail = Not qualified (CLAUDE.md; OPEN QUESTION for Nikhil: "two or more").
        # Exception: a timeline-only fail where the caller is open to a later start -> Nurture (T07).
        if fails == ["TIMELINE_IMPOSSIBLE"] and f.get("open_to_later_start"):
            res.decision, res.reason_code = "Nurture", "TIMELINE_IMPOSSIBLE"
        else:
            res.decision, res.reason_code = "Not qualified", fails[0]
        return res

    if f.get("just_exploring") and f.get("booking_response") != "agreed":
        res.decision = "Nurture"
        return res

    res.decision = "Qualified"
    if f.get("deadline_driver") in FIXED_DATE_DRIVERS:
        res.priority_reasons.append(f"fixed date: {f['deadline_driver'].replace('_', ' ')}")
    if f.get("wants_meeting_within_2_weeks"):
        res.priority_reasons.append("wants to meet within 2 weeks")
    if f.get("returning_caller_let_down"):
        res.priority_reasons.append("returning caller we let down")
    res.priority = "P1" if res.priority_reasons else "P2"
    return res
