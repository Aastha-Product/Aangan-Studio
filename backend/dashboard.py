"""Nikhil's dashboard (build step 9): what the agent handled, what it booked, and what it cost.

compute_metrics() is pure (testable); render_*() return HTML strings (server-rendered, inline SVG, light/dark).
Costs are computed here from raw usage stored per call, so changing a rate never needs a data migration.

Every call falls in one of three groups, used by the chart, the filters and the table:
  booked     a consultation is on the calendar
  follow_up  someone at the studio has to act (slot to confirm, escalation, cancellation, no-show, missed call, nurture)
  closed     not taken forward (not a fit, or no conversation)
"""
import contextvars
import html
import json
import statistics
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config
from .calendly import parse_time, spoken_label
from .emails import plain_reason

# Who is signed in for the page being rendered (set by web.py): {"kind": "account"|"studio", "name", "email"}.
VIEWER: contextvars.ContextVar[dict | None] = contextvars.ContextVar("viewer", default=None)

REASON_ORDER = ["OUT_OF_AREA", "OUT_OF_SCOPE_TYPE", "ADVICE_ONLY", "TOO_SMALL", "TIMELINE_IMPOSSIBLE",
                "BUDGET_MISALIGNED", "NOT_DECISION_MAKER", "EXISTING_CLIENT_COMPLAINT", "NO_INFO"]
REASON_SHORT = {
    "OUT_OF_AREA": "Outside Pune / PCMC",
    "OUT_OF_SCOPE_TYPE": "Project type we don't take",
    "ADVICE_ONLY": "Wanted advice only",
    "TOO_SMALL": "Below minimum scope",
    "TIMELINE_IMPOSSIBLE": "Timeline too tight",
    "BUDGET_MISALIGNED": "Budget below scope",
    "NOT_DECISION_MAKER": "Not the decision-maker",
    "EXISTING_CLIENT_COMPLAINT": "Existing client complaint",
    "NO_INFO": "No conversation",
}
GROUPS = [("booked", "Booked"), ("follow_up", "Needs follow-up"), ("closed", "Closed")]
GROUP_HINT = {"booked": "consultation on the calendar",
              "follow_up": "someone at the studio has to call or confirm",
              "closed": "not a fit, no conversation, or follow-up done"}
GROUP_COLOR = {"booked": "var(--series-1)", "follow_up": "var(--series-2)", "closed": "var(--series-3)"}


# --- cost ----------------------------------------------------------------------------------------

def call_cost(c: dict) -> dict:
    """{'usd': float, 'inr': float, 'missing': [rate names]} — usd = APIs billed in dollars, inr = rupee items."""
    u = c.get("usage") or {}
    usd, inr, missing = 0.0, 0.0, []
    cl = u.get("claude") or {}
    if cl:
        p = config.CLAUDE_PRICES_USD.get(cl.get("model"))
        if p:
            usd += (cl.get("input", 0) * p[0] + cl.get("output", 0) * p[1]
                    + cl.get("cache_read", 0) * p[2] + cl.get("cache_write", 0) * p[3]) / 1e6
        else:
            missing.append(f"Claude price for {cl.get('model')}")
    gm = u.get("gemini") or {}
    if gm:
        if config.GEMINI_USD_PER_MTOK_IN is not None and config.GEMINI_USD_PER_MTOK_OUT is not None:
            usd += (gm.get("input_tokens", 0) * config.GEMINI_USD_PER_MTOK_IN
                    + (gm.get("output_tokens", 0) + gm.get("thinking_tokens", 0)) * config.GEMINI_USD_PER_MTOK_OUT) / 1e6
        else:
            missing.append("COST_GEMINI_USD_PER_MTOK_IN/OUT")
    if c.get("duration_sec"):
        if config.VAANI_INR_PER_MIN is not None:
            inr += c["duration_sec"] / 60 * config.VAANI_INR_PER_MIN
        else:
            missing.append("COST_VAANI_INR_PER_MIN")
    inr += (u.get("emails") or 0) * (config.EMAIL_INR_EACH or 0)
    return {"usd": usd, "inr": inr, "missing": missing}


def to_inr(cost: dict) -> float | None:
    if config.USD_INR_RATE is None:
        return None if cost["usd"] else cost["inr"]
    return cost["inr"] + cost["usd"] * config.USD_INR_RATE


# --- metrics ---------------------------------------------------------------------------------------

def _ts(iso):
    return parse_time(iso) if iso else None


def _median(xs):
    return statistics.median(xs) if xs else None


def group_of(c: dict) -> str:
    status, decision = c.get("status"), c.get("decision")
    if status == "booked":
        return "booked"
    if c.get("handled_at"):
        return "closed"
    if c.get("overturned_at"):
        return "follow_up"
    if status in ("not_qualified", "no_data", "nurture_followed_up") or decision in ("Not qualified", "No data"):
        return "closed"
    return "follow_up"   # booking_pending, escalated, cancelled, no_show, missed, nurture, still processing


def next_step(c: dict, today=None) -> tuple[str, str]:
    """(short text, urgency) for the table and the attention list. urgency: high | medium | low | none."""
    status = c.get("status")
    if status == "booked":
        return (f"Consultation {_slot(c.get('slot_start'))}" if c.get("slot_start") else "Consultation booked"), "none"
    if c.get("handled_at"):
        return ("Done" + (f": {c['handled_note']}" if c.get("handled_note") else "")), "none"
    if c.get("overturned_at"):
        return "Call back: rejection overturned", "high"
    if status == "escalated":
        return "Senior callback: existing client", "high"
    if status == "booking_pending":
        return "Confirm a consultation slot", "high"
    if status == "cancelled":
        return "Call back: consultation cancelled", "medium"
    if status == "no_show":
        return "Follow up: missed the consultation", "medium"
    if status == "missed":
        return "Call back: missed call", "high"
    if status == "nurture":
        due = c.get("follow_up_on")
        is_due = bool(due and today and due <= today.isoformat())
        return (f"Follow up {'now' if is_due else 'on ' + _date(due)}" if due else "Follow up later"), \
            ("medium" if is_due else "low")
    if status == "nurture_followed_up":
        return "Followed up", "none"
    if not c.get("decision"):
        return "Being processed", "none"
    return REASON_SHORT.get(c.get("reason_code") or "", "Not taken forward"), "none"


def compute_metrics(calls: list[dict], events: list[dict], now: datetime, days: int | None) -> dict:
    if days:
        start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # month to date
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    in_range = [c for c in calls if start <= (_ts(c.get("started_at") or c.get("created_at")) or now) <= now]

    answered = [c for c in in_range if c.get("answered", True)]
    talked = [c for c in answered if c.get("decision") and c.get("decision") != "No data"]
    qualified = [c for c in talked if c.get("decision") == "Qualified"]
    booked_on_call = [c for c in in_range if c.get("booked_on_call")]
    booked_any = [c for c in in_range if c.get("booked_at")]
    cancelled = [c for c in in_range if c.get("status") == "cancelled"]
    no_show = [c for c in in_range if c.get("status") == "no_show"]
    with_card = [c for c in in_range if c.get("report_card_sent_at")]
    reask = [c for c in with_card if c.get("had_to_reask")]
    won = [c for c in in_range if c.get("deal_outcome") == "won"]
    lost = [c for c in in_range if c.get("deal_outcome") == "lost"]

    answer_secs = [(_ts(c["answered_at"]) - _ts(c["started_at"])).total_seconds()
                   for c in answered if c.get("answered_at") and c.get("started_at")]
    to_booking_min = [(_ts(c["booked_at"]) - _ts(c["started_at"])).total_seconds() / 60
                      for c in booked_any if c.get("started_at")]

    costs = [call_cost(c) for c in in_range]
    inr_values = [to_inr(x) for x in costs]
    total_inr = sum(v for v in inr_values if v is not None) if all(v is not None for v in inr_values) else None
    missing = sorted({m for x in costs for m in x["missing"]} | ({"USD_INR_RATE"} if total_inr is None and costs else set()))

    reasons = Counter(c.get("reason_code") for c in in_range
                      if c.get("decision") in ("Not qualified", "Nurture", "Escalate") and c.get("reason_code"))
    groups = Counter(group_of(c) for c in in_range)

    day_keys = []
    d = start.date()
    while d <= now.date():
        day_keys.append(d)
        d += timedelta(days=1)
    per_day = {k: {"calls": 0, "bookings": 0, **{g: 0 for g, _ in GROUPS}} for k in day_keys}
    for c in in_range:
        t = _ts(c.get("started_at") or c.get("created_at"))
        if t and t.astimezone(config.IST).date() in per_day:
            day = per_day[t.astimezone(config.IST).date()]
            day["calls"] += 1
            day[group_of(c)] += 1
            if c.get("booked_on_call"):
                day["bookings"] += 1

    # Open items from the last 7 days, whatever the period filter says — this is the to-do list.
    week_ago = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    rank = {"high": 0, "medium": 1, "low": 2}
    attention = []
    for c in calls:
        t = _ts(c.get("started_at") or c.get("created_at"))
        if not t or t < week_ago:
            continue
        text, urgency = next_step(c, now.date())
        if urgency in ("high", "medium"):
            attention.append((rank[urgency], -t.timestamp(), c, text, urgency))
    attention.sort(key=lambda x: (x[0], x[1]))

    blocks = [e for e in events if start <= (_ts(e.get("created_at")) or now) <= now
              and e.get("kind") == "speech_guard_block"]
    pct = lambda a, b: (100 * len(a) / len(b)) if b else None  # noqa: E731
    return {
        "start": start, "now": now, "calls": len(in_range), "answered": len(answered),
        "answered_pct": pct(answered, in_range), "talked": len(talked),
        "median_answer_sec": _median(answer_secs), "max_answer_sec": max(answer_secs) if answer_secs else None,
        "after_hours": sum(1 for c in answered if c.get("after_hours")),
        "qualified": len(qualified), "qualified_pct": pct(qualified, talked),
        "booked_on_call": len(booked_on_call), "booked_any": len(booked_any),
        "median_call_to_booking_min": _median(to_booking_min),
        "cancelled": len(cancelled), "no_show": len(no_show),
        "reask_pct": pct(reask, with_card), "report_cards": len(with_card),
        "won": len(won), "lost": len(lost),
        "escalations": sum(1 for c in in_range if c.get("decision") == "Escalate"),
        "guard_blocks": len(blocks),
        "total_cost_inr": total_inr,
        "cost_per_call_inr": (total_inr / len(in_range)) if total_inr is not None and in_range else None,
        "cost_per_booking_inr": (total_inr / len(booked_any)) if total_inr is not None and booked_any else None,
        "cost_missing": missing,
        "reasons": [(r, reasons[r]) for r in REASON_ORDER if reasons.get(r)],
        "groups": {g: groups.get(g, 0) for g, _ in GROUPS},
        "per_day": [(k, v["calls"], v["bookings"]) for k, v in per_day.items()],
        "per_day_groups": [(k, v["booked"], v["follow_up"], v["closed"]) for k, v in per_day.items()],
        "attention": [(c, text, urgency) for *_, c, text, urgency in attention],
        "ever": len(calls),
        "rows": sorted(in_range, key=lambda c: c.get("started_at") or c.get("created_at") or "", reverse=True),
    }



# --- formatting ------------------------------------------------------------------------------------

e = html.escape


def _fmt_inr(v):
    if v is None:
        return "—"
    return f"₹{v:,.0f}" if v >= 100 else f"₹{v:,.2f}"


def _fmt_pct(v):
    return "—" if v is None else f"{v:.0f}%"


def _fmt_secs(v):
    if v is None:
        return "—"
    return f"{v:.0f} s" if v < 120 else f"{v / 60:.1f} min"


def _fmt_dur(sec):
    if not sec:
        return "—"
    m, s = divmod(int(sec), 60)
    return f"{m} min {s:02d} s" if m else f"{s} s"


def _when(c):
    t = _ts(c.get("started_at") or c.get("created_at"))
    return t.astimezone(config.IST).strftime("%d %b, %I:%M %p").lstrip("0").replace(" 0", " ") if t else "—"


def _slot(iso):
    try:
        return spoken_label(parse_time(iso)) if iso else "—"
    except ValueError:
        return iso


def _date(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d %b").lstrip("0") if iso else "—"
    except ValueError:
        return iso


def _caller(c):
    f = c.get("fields") or {}
    return (c.get("caller_name") or c.get("invitee_name") or f.get("caller_name") or c.get("caller_number")
            or ("Website caller" if c.get("channel") == "web" else "Unknown caller"))


def _project(c):
    f = c.get("fields") or {}
    kind = f.get("property_description") or {
        "residential": "Residential", "office_clinic_studio": "Office", "restaurant_hotel_hospitality": "Restaurant / hotel",
        "retail": "Retail", "gym": "Gym", "other_commercial": "Commercial"}.get(f.get("property_category") or "", "")
    area = f.get("location_text") or ""
    if area and area.split(" —")[0].lower() in kind.lower():
        area = ""                                   # "3BHK in Kothrud" already names the area
    kind = kind[:1].upper() + kind[1:]
    return " · ".join(x for x in (kind, area) if x) or "—"


def _q(path, token, **params):
    params = {k: v for k, v in params.items() if v is not None}
    if token:
        params["token"] = token
    return path + ("?" + urllib.parse.urlencode(params) if params else "")


BADGES = {  # (css class, icon, label) — status colour always ships with an icon and a word
    "booked": ("good", "✓", "Booked"),
    "booking_pending": ("warning", "●", "Slot to confirm"),
    "escalated": ("critical", "!", "Existing client"),
    "cancelled": ("serious", "×", "Cancelled"),
    "no_show": ("serious", "×", "Didn't show up"),
    "missed": ("critical", "!", "Missed call"),
    "nurture": ("neutral", "↻", "Follow up later"),
    "nurture_followed_up": ("neutral", "✓", "Followed up"),
    "not_qualified": ("neutral", "–", "Not a fit"),
    "no_data": ("neutral", "–", "No conversation"),
}
FRIENDLY_DECISION = {"Qualified": "Good fit", "Not qualified": "Not a fit", "Nurture": "Maybe later",
                     "Escalate": "Existing client", "No data": "No conversation"}
FLASH = {"done": "Marked as done. It's off the to-do list.",
         "reopen": "Reopened. It's back on the to-do list.",
         "overturn": "Overturned. The caller is on the to-do list for a call back.",
         "welcome": "Account created. You're signed in."}


def _badge(c):
    if c.get("status") != "booked" and c.get("handled_at"):
        cls, icon, label = "good", "✓", "Done"
    elif c.get("status") != "booked" and c.get("overturned_at"):
        cls, icon, label = "warning", "↺", "Overturned"
    else:
        cls, icon, label = BADGES.get(c.get("status") or "", ("neutral", "…", "Being processed"))
    return f"<span class='badge {cls}'><span class='bi' aria-hidden='true'>{icon}</span>{e(label)}</span>"


def _fit(c):
    """Plain-words verdict: 'Good fit · high priority' / 'Outside Pune / PCMC'."""
    d = c.get("decision")
    if not d:
        return ""
    if d == "Qualified":
        return "Good fit · high priority" if c.get("priority") == "P1" else "Good fit"
    return REASON_SHORT.get(c.get("reason_code") or "", FRIENDLY_DECISION.get(d, d))


def _outcome(c):   # kept for the CSV and older callers
    return e(_fit(c)) or "—"


def _interest_word(score):
    if score is None:
        return None
    return "High" if score >= 70 else "Medium" if score >= 40 else "Low"


def _interest(score, compact=False):
    w = _interest_word(score)
    if w is None:
        return ""
    s = max(0, min(100, int(score)))
    return (f"<span class='meter' title='How interested they sounded: {s} out of 100'>"
            f"<span class='bar'><span style='width:{s}%'></span></span><span>{w}{'' if compact else ' interest'}</span></span>")


def _meter(score):   # used by tests / older markup
    return _interest(score, compact=True) or "—"


def _ago(iso, now):
    t = _ts(iso)
    if not t:
        return "—"
    secs = (now - t).total_seconds()
    local = t.astimezone(config.IST)
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if local.date() == now.date():
        return f"{int(secs // 3600)} h ago"
    if local.date() == now.date() - timedelta(days=1):
        return "yesterday, " + local.strftime("%I:%M %p").lstrip("0")
    if secs < 6 * 86400:
        return local.strftime("%a, %I:%M %p").replace(" 0", " ")
    return local.strftime("%d %b").lstrip("0")


def _initials(c):
    name = c.get("caller_name") or c.get("invitee_name") or (c.get("fields") or {}).get("caller_name") or ""
    parts = [p for p in name.replace("(", " ").split() if p[:1].isalpha()]
    return "".join(p[0].upper() for p in parts[:2]) or ""


# --- icons (inline, currentColor) -------------------------------------------------------------------

_ICON_PATHS = {
    "phone": "M5 4h3l2 5-2.5 1.5a11 11 0 0 0 5 5L14 13l5 2v3a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2",
    "mail": "M4 6h16v12H4zM4 7l8 6 8-6",
    "ext": "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
    "play": "M8 5v14l11-7z",
    "check": "M5 12.5l4.5 4.5L19 7.5",
    "undo": "M9 14L4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3",
    "download": "M12 4v11M7 10l5 5 5-5M5 20h14",
    "back": "M15 18l-6-6 6-6",
    "arrow": "M5 12h14M13 6l6 6-6 6",
    "home": "M4 11l8-7 8 7v9h-5v-6H9v6H4z",
    "list": "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
    "chart": "M4 20V10M10 20V4M16 20v-7M22 20H2",
    "gear": "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12l2-1-1-3-2 .5-1.5-1.5.5-2-3-1-1 2h-2L9 3 6 4l.5 2L5 7.5 3 7 2 10l2 1v2l-2 1 1 3 2-.5L6.5 18 6 20l3 1 1-2h2l1 2 3-1-.5-2 1.5-1.5 2 .5 1-3-2-1z",
    "user": "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
    "x": "M6 6l12 12M18 6L6 18",
    "info": "M12 8h.01M11 12h1v5h1M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
    "moon": "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
}


def _icon(name, size=16):
    return (f"<svg class='ic' width='{size}' height='{size}' viewBox='0 0 24 24' fill='none' stroke='currentColor' "
            f"stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
            f"<path d='{_ICON_PATHS[name]}'/></svg>")


# --- styles ----------------------------------------------------------------------------------------

CSS = """
.viz-root{color-scheme:light;--page:#f6f5f1;--surface-1:#fcfcfb;--surface-2:#f0efea;--text-primary:#0b0b0b;
--text-secondary:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--seq-track:#cde2fb;
--good:#0ca30c;--good-text:#006300;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;--critical-text:#b02a2a;
--good-bg:#e6f4e6;--warning-bg:#fff5db;--serious-bg:#fdece4;--critical-bg:#fbe6e6;--neutral-bg:#efeee9;
--brand:#9a4a2b;--brand-ink:#fff;--brand-soft:#f6e9e2;--focus:#2a78d6;--shadow:0 1px 2px rgba(11,11,11,.04)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--page:#0d0d0d;
--surface-1:#1a1a19;--surface-2:#232321;--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;
--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;
--seq-track:#184f95;--good-text:#0ca30c;--critical-text:#e66767;--good-bg:#132a13;--warning-bg:#3a2f12;--serious-bg:#3a2016;
--critical-bg:#3a1616;--neutral-bg:#2a2a28;--brand:#e0936f;--brand-ink:#1a1a19;--brand-soft:#3a251c;--focus:#3987e5;--shadow:none}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--surface-2:#232321;
--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--seq-track:#184f95;
--good-text:#0ca30c;--critical-text:#e66767;--good-bg:#132a13;--warning-bg:#3a2f12;--serious-bg:#3a2016;--critical-bg:#3a1616;
--neutral-bg:#2a2a28;--brand:#e0936f;--brand-ink:#1a1a19;--brand-soft:#3a251c;--focus:#3987e5;--shadow:none}
*{box-sizing:border-box}html,body{margin:0;background:var(--page)}[hidden]{display:none!important}
.viz-root{background:var(--page);color:var(--text-primary);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:inherit}a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.ic{flex:none;vertical-align:-3px}
.wrap{max-width:1120px;margin:0 auto;padding:0 16px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
/* top bar + tabs */
.topbar{background:var(--surface-1);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:5}
.topbar .wrap{display:flex;align-items:center;gap:18px;min-height:58px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;margin-right:6px}
.mark{width:30px;height:30px;border-radius:8px;background:var(--brand);color:var(--brand-ink);display:grid;place-items:center;font-weight:700;font-size:15px}
.brand b{display:block;font-size:15px;line-height:1.2}.brand small{display:block;color:var(--text-secondary);font-size:12px}
.tabs{display:flex;gap:2px;align-self:stretch;flex:1;min-width:0;overflow-x:auto;scrollbar-width:none}
.tabs a{display:flex;align-items:center;gap:7px;padding:0 12px;text-decoration:none;color:var(--text-secondary);font-size:14px;
border-bottom:2px solid transparent;white-space:nowrap;position:relative}
.tabs a:hover{color:var(--text-primary)}
.tabs a.on{color:var(--text-primary);font-weight:600;border-bottom-color:var(--brand)}
.tabs .pip{width:7px;height:7px;border-radius:50%;background:var(--warning);display:inline-block}
.usermenu{position:relative}
.usermenu summary{list-style:none;cursor:pointer;border-radius:50%}.usermenu summary::-webkit-details-marker{display:none}
.uav{width:34px;height:34px;border-radius:50%;background:var(--brand-soft);color:var(--brand);display:grid;place-items:center;
font-weight:700;font-size:13px}
.umenu{position:absolute;right:0;top:42px;z-index:10;background:var(--surface-1);border:1px solid var(--border);border-radius:12px;
padding:14px;min-width:230px;box-shadow:0 8px 28px rgba(0,0,0,.14);display:grid;gap:4px}
.umenu span{color:var(--muted);font-size:12.5px;margin-bottom:8px;overflow-wrap:anywhere}
.tabs .count{background:var(--critical);color:#fff;border-radius:999px;font-size:11px;font-weight:700;padding:0 6px;line-height:17px}
.seg{display:flex;gap:2px;background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:3px}
.seg a{padding:4px 11px;border-radius:7px;color:var(--text-secondary);text-decoration:none;font-size:13px;white-space:nowrap}
.seg a:hover{color:var(--text-primary)}
.seg a.on{background:var(--surface-1);color:var(--text-primary);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.08)}
main{padding:26px 0 56px}
/* type */
h1{font-size:26px;line-height:1.2;margin:0;letter-spacing:-.015em}h2{font-size:16px;margin:0}
h3{font-size:14px;margin:0}
.sub{color:var(--text-secondary);margin:2px 0 0;font-size:13.5px}
.muted{color:var(--muted)}
.pagehead{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;flex-wrap:wrap;margin-bottom:20px}
.headline{font-size:16px;margin:8px 0 0;max-width:760px;color:var(--text-secondary);line-height:1.55}
.headline b{color:var(--text-primary);font-weight:600}.headline .urgent{color:var(--critical-text);font-weight:600}
/* buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid var(--border);background:var(--surface-1);
color:var(--text-primary);border-radius:10px;padding:8px 14px;font:inherit;font-size:14px;font-weight:600;text-decoration:none;
cursor:pointer;white-space:nowrap;min-height:38px}
.btn:hover{background:var(--surface-2)}
.btn.primary{background:var(--text-primary);color:var(--surface-1);border-color:var(--text-primary)}
.btn.primary:hover{opacity:.88}
.btn.small{padding:5px 10px;font-size:13px;min-height:32px;border-radius:9px}
.btn.icon{padding:0;width:36px;min-height:36px}
.link{display:inline-flex;align-items:center;gap:6px;font-size:14px;font-weight:600;text-decoration:none;color:var(--text-primary)}
.link:hover{text-decoration:underline}
/* cards */
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:16px;padding:20px;box-shadow:var(--shadow);min-width:0}
.cardhead{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.cardhead .sub{margin:0}
.grid2{display:grid;grid-template-columns:3fr 2fr;gap:16px;margin-top:16px}
.stack{display:grid;gap:16px;align-content:start}
/* banners + toast */
.banner{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;border-radius:14px;padding:12px 16px;
margin:0 0 18px;font-size:14px;background:var(--warning-bg);border:1px solid var(--border)}
.banner .txt{display:flex;gap:10px;align-items:center}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--text-primary);color:var(--surface-1);
padding:11px 16px;border-radius:12px;font-size:14px;font-weight:600;display:flex;gap:10px;align-items:center;z-index:20;
box-shadow:0 8px 28px rgba(0,0,0,.2);animation:toast 5s ease forwards;max-width:calc(100% - 32px)}
@keyframes toast{0%{opacity:0;transform:translate(-50%,10px)}6%{opacity:1;transform:translate(-50%,0)}85%{opacity:1}100%{opacity:0;visibility:hidden}}
/* stat cards */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
a.stat{text-decoration:none;display:block;transition:border-color .15s}
a.stat:hover{border-color:var(--axis)}
.stat .label{color:var(--text-secondary);font-size:13.5px;display:flex;align-items:center;gap:7px}
.stat .num{font-size:34px;font-weight:650;line-height:1.1;margin:6px 0 4px;letter-spacing:-.02em}
.stat .note{font-size:12.5px;color:var(--muted)}
.key{display:inline-block;width:10px;height:10px;border-radius:3px;vertical-align:-1px}
.delta{display:inline-flex;align-items:center;gap:3px;font-size:12.5px;font-weight:600;color:var(--text-secondary);white-space:nowrap}
.delta.good{color:var(--good-text)}.delta.bad{color:var(--critical-text)}
.delta .vs{font-weight:400;color:var(--muted)}
/* call list */
.list{list-style:none;margin:0;padding:0}
.list>li{border-top:1px solid var(--grid);display:flex;align-items:center;gap:8px}
.list>li:first-child{border-top:0}
.list>li.dayrow{border-top:0;padding:16px 4px 6px;font-size:12.5px;font-weight:600;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.05em}
.list>li.dayrow span{text-transform:none;letter-spacing:0;font-weight:400;color:var(--muted)}
.list>li.dayrow+li{border-top:0}
a.item{flex:1;min-width:0;display:grid;grid-template-columns:40px minmax(0,1fr) auto;gap:12px;align-items:center;padding:12px 6px;text-decoration:none;border-radius:12px}
a.item>.body{min-width:0}
a.item:hover{background:var(--surface-2)}
.avatar{width:40px;height:40px;border-radius:50%;background:var(--brand-soft);color:var(--brand);display:grid;place-items:center;font-weight:700;font-size:14px}
.item .l1{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .l1 .muted{font-weight:400}
.item .l2{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:3px;font-size:13.5px;color:var(--text-secondary)}
.item .l2 .urgent{color:var(--critical-text);font-weight:600}
.item .meta{display:flex;flex-direction:column;align-items:flex-end;gap:5px;font-size:12.5px;color:var(--muted);white-space:nowrap}
.item .meta .ic{vertical-align:-2px;margin-left:4px}
.item .meta .meter .bar{width:28px}
.quick{display:flex;gap:6px;padding-right:4px}
.quick form{margin:0}
.empty{color:var(--text-secondary);font-size:14px;padding:10px 0}
.emptybig{text-align:center;padding:34px 12px}.emptybig .ok{width:46px;height:46px;border-radius:50%;background:var(--good-bg);
color:var(--good-text);display:grid;place-items:center;margin:0 auto 10px}
/* filters */
.toolbar{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:6px}
.toolbar .right{display:flex;gap:8px;align-items:center}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--border);background:var(--surface-1);color:var(--text-secondary);border-radius:999px;padding:6px 13px;
font:inherit;font-size:13.5px;cursor:pointer;display:inline-flex;align-items:center;gap:7px}
.chip:hover{color:var(--text-primary)}.chip[aria-pressed="true"]{background:var(--text-primary);color:var(--surface-1);border-color:var(--text-primary)}
.chip .c{font-variant-numeric:tabular-nums;opacity:.75}
.search{border:1px solid var(--border);background:var(--surface-1);color:var(--text-primary);border-radius:10px;padding:8px 12px;
font:inherit;font-size:14px;width:260px;max-width:100%}
/* badges + meters */
.badge{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:2px 9px 2px 6px;font-size:12.5px;font-weight:600;white-space:nowrap;
background:var(--neutral-bg);color:var(--text-primary)}
.badge .bi{font-size:11px;width:15px;height:15px;border-radius:50%;display:inline-grid;place-items:center;color:#fff;background:var(--muted)}
.badge.good{background:var(--good-bg)}.badge.good .bi{background:var(--good)}
.badge.warning{background:var(--warning-bg)}.badge.warning .bi{background:var(--warning);color:#0b0b0b}
.badge.serious{background:var(--serious-bg)}.badge.serious .bi{background:var(--serious);color:#0b0b0b}
.badge.critical{background:var(--critical-bg)}.badge.critical .bi{background:var(--critical)}
.meter{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--text-secondary)}
.meter .bar{width:44px;height:6px;border-radius:3px;background:var(--seq-track);overflow:hidden}
.meter .bar span{display:block;height:100%;background:var(--series-1);border-radius:3px}
/* reports */
.funnel{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.fstep{background:var(--surface-2);border-radius:12px;padding:14px;min-width:0}
.fstep .v{font-size:28px;font-weight:650;line-height:1.1;margin-top:4px}.fstep .n{color:var(--muted);font-size:12.5px;margin-top:2px}
.fstep .rail{height:6px;border-radius:3px;background:var(--seq-track);margin-top:10px;overflow:hidden}
.fstep .rail span{display:block;height:100%;background:var(--series-1);border-radius:3px}
.fstep .label,.tile .label{color:var(--text-secondary);font-size:13px}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--text-secondary);font-size:13px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.chart{position:relative}.chart svg{display:block;width:100%;height:auto}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
padding:8px 10px;font-size:12.5px;box-shadow:0 4px 16px rgba(0,0,0,.12);white-space:nowrap;opacity:0;transition:opacity .1s;z-index:2}
.tip b{display:block;margin-bottom:2px}.tip .r{display:flex;align-items:center;gap:6px;color:var(--text-secondary)}
.tip .r span:last-child{margin-left:auto;padding-left:12px;color:var(--text-primary);font-variant-numeric:tabular-nums}
.hbars .row{display:grid;grid-template-columns:minmax(120px,180px) 1fr;gap:10px;align-items:center;margin:10px 0}
.hbars .name{color:var(--text-secondary);font-size:13.5px;line-height:1.3}
.hbars .track{display:flex;align-items:center;gap:8px;min-width:0}
.hbars .fill{height:14px;background:var(--series-1);border-radius:0 4px 4px 0;min-width:3px}
.hbars .val{font-size:13px;font-variant-numeric:tabular-nums}
.section-title{font-size:12.5px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:28px 0 10px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{padding:16px}.tile .value{font-size:24px;font-weight:650;margin-top:4px;line-height:1.2}
.tile .note{color:var(--muted);font-size:12.5px;margin-top:3px}
details.tv summary{cursor:pointer;color:var(--text-secondary);font-size:13px;margin-top:10px}
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--grid)}th{color:var(--muted);font-weight:600;font-size:12px}
td.num,th.num{font-variant-numeric:tabular-nums;text-align:right}
/* setup */
.checklist{list-style:none;margin:0;padding:0}
.checklist>li{display:grid;grid-template-columns:34px 1fr;gap:14px;padding:16px 0;border-top:1px solid var(--grid)}
.checklist>li:first-child{border-top:0;padding-top:4px}
.st{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:14px;color:#fff}
.st.ok{background:var(--good)}.st.no{background:var(--critical)}.st.warn{background:var(--warning);color:#0b0b0b}
.checklist .what{color:var(--text-secondary);font-size:14px;margin-top:2px}
.checklist .fix{margin-top:8px;font-size:13.5px;background:var(--surface-2);border-radius:10px;padding:10px 12px}
.checklist .fix ol{margin:4px 0 0;padding-left:18px}
.checklist code{font:12.5px ui-monospace,Consolas,monospace;background:var(--neutral-bg);padding:1px 5px;border-radius:5px}
.progress{height:8px;border-radius:4px;background:var(--seq-track);overflow:hidden;margin:10px 0 2px}
.progress span{display:block;height:100%;background:var(--good)}
/* call page */
.back{display:inline-flex;align-items:center;gap:4px;color:var(--text-secondary);text-decoration:none;font-size:14px;margin-bottom:14px}
.back:hover{color:var(--text-primary)}
.callhead{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:18px}
.callhead .avatar{width:56px;height:56px;font-size:19px}
.callhead .meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;color:var(--text-secondary);font-size:14px}
.why{font-size:16px;margin:0;line-height:1.55}
.facts-inline{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.fact{background:var(--surface-2);border-radius:999px;padding:4px 11px;font-size:13px;color:var(--text-secondary)}
.fact b{color:var(--text-primary);font-weight:600}
.nextcard{border:1px solid var(--border);border-left:4px solid var(--brand);border-radius:16px;background:var(--surface-1);padding:18px 20px;margin-bottom:16px}
.nextcard.high{border-left-color:var(--critical)}.nextcard.ok{border-left-color:var(--good)}
.nextcard .label{font-size:12.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.nextcard .todo{font-size:18px;font-weight:650;margin:4px 0 0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.handle{margin-top:14px;padding-top:14px;border-top:1px solid var(--grid)}
.handle form{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.handle input{border:1px solid var(--border);background:var(--surface-1);color:var(--text-primary);border-radius:10px;padding:8px 11px;font:inherit;font-size:14px;min-height:38px}
.handle input[name=note]{flex:1 1 260px;min-width:0}.handle input[name=by]{width:160px}
.handle .hint{font-size:13px;color:var(--text-secondary);margin-bottom:8px}
dl.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px 20px;margin:0}
dl.facts dt{color:var(--muted);font-size:12.5px}dl.facts dd{margin:2px 0 0;overflow-wrap:anywhere}
details.more{border-top:1px solid var(--grid);margin-top:16px;padding-top:12px}
details.more summary{cursor:pointer;font-size:14px;font-weight:600;color:var(--text-secondary)}
.checks{list-style:none;margin:0;padding:0}
.checks li{display:grid;grid-template-columns:26px 1fr;gap:10px;padding:10px 0;border-top:1px solid var(--grid)}
.checks li:first-child{border-top:0}
.ci{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;color:#fff}
.ci.pass{background:var(--good)}.ci.fail{background:var(--critical)}.ci.unclear{background:var(--warning);color:#0b0b0b}
.checks .n{font-weight:600;font-size:14px}.checks .d{color:var(--text-secondary);font-size:13px}
.factor{padding:10px 0;border-top:1px solid var(--grid)}.factor:first-child{border-top:0}
.factor .top{display:flex;justify-content:space-between;gap:8px;font-size:14px}
.factor .top span{font-variant-numeric:tabular-nums;color:var(--text-secondary)}
.factor .bar{height:6px;border-radius:3px;background:var(--seq-track);margin:6px 0;overflow:hidden}
.factor .bar span{display:block;height:100%;background:var(--series-1);border-radius:3px}
.factor q{display:block;color:var(--text-secondary);font-size:13px;font-style:italic}
.flags{margin:10px 0 0;padding-left:18px;color:var(--text-secondary);font-size:14px}.flags li{margin:3px 0}
.timeline{list-style:none;margin:0;padding:0}
.timeline li{display:grid;grid-template-columns:120px 1fr;gap:10px;padding:8px 0;font-size:13.5px;border-top:1px solid var(--grid)}
.timeline li:first-child{border-top:0}.timeline .t{color:var(--muted);font-size:12.5px}
.timeline .bad{color:var(--critical-text);font-weight:600}
.chat{display:flex;flex-direction:column;gap:8px;max-height:620px;overflow-y:auto;padding-right:4px}
.msg{max-width:80%;padding:9px 13px;border-radius:16px;font-size:14px;line-height:1.45}
.msg .who{display:block;font-size:11px;font-weight:600;color:var(--muted);margin-bottom:2px;text-transform:uppercase;letter-spacing:.04em}
.msg.agent{align-self:flex-start;background:var(--surface-2);border-bottom-left-radius:4px}
.msg.caller{align-self:flex-end;background:var(--seq-track);border-bottom-right-radius:4px}
.footer{color:var(--muted);font-size:12.5px;margin-top:32px;text-align:center}
@media (max-width:900px){.grid2{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.funnel{grid-template-columns:repeat(2,1fr)}}
@media (max-width:960px){.topbar .wrap{gap:4px 12px;padding-top:8px}.tabs{order:3;flex-basis:100%;min-height:44px}
 .seg{margin-left:auto}}
@media (max-width:640px){
 .topbar{position:static}
 .brand small{display:none}h1{font-size:22px}.headline{font-size:15px}
 .stat .num{font-size:28px}.card{padding:16px;border-radius:14px}
 a.item{grid-template-columns:36px 1fr;gap:10px}.avatar{width:36px;height:36px;font-size:13px}
 .item .meta{grid-column:2;flex-direction:row;align-items:center;justify-content:flex-start;gap:10px}
 .search{width:100%}.toolbar .right{width:100%}.toolbar .right .search{flex:1}
 .msg{max-width:92%}.timeline li{grid-template-columns:1fr;gap:0}.hide-sm{display:none}
 .seg{order:2;margin-left:auto}
}
"""

JS = """
(function(){
  // Chart hover: one tooltip per day column
  document.querySelectorAll('.chart').forEach(function(box){
    var tip = box.querySelector('.tip');
    box.querySelectorAll('[data-tip]').forEach(function(hit){
      hit.addEventListener('mouseenter', function(){
        var d = JSON.parse(hit.getAttribute('data-tip'));
        tip.innerHTML = '<b>'+d.day+'</b>' + d.rows.map(function(r){
          return '<div class="r"><span class="key" style="background:'+r[2]+'"></span><span>'+r[0]+'</span><span>'+r[1]+'</span></div>';
        }).join('');
        tip.style.opacity = 1;
      });
      hit.addEventListener('mousemove', function(ev){
        var b = box.getBoundingClientRect(), x = ev.clientX - b.left + 14, w = tip.offsetWidth;
        if (x + w > b.width) x = ev.clientX - b.left - w - 14;
        tip.style.left = x + 'px'; tip.style.top = Math.max(0, ev.clientY - b.top - 20) + 'px';
      });
      hit.addEventListener('mouseleave', function(){ tip.style.opacity = 0; });
    });
  });
  // "Your name" remembers the last name typed on this device
  document.querySelectorAll('input[name=by]').forEach(function(i){
    try { i.value = i.value || localStorage.getItem('aangan_by') || ''; } catch (e) {}
    if (i.form) i.form.addEventListener('submit', function(){ try { localStorage.setItem('aangan_by', i.value); } catch (e) {} });
  });
  // Quick "Done" buttons carry the remembered name too
  document.querySelectorAll('form.quickdone').forEach(function(f){
    f.addEventListener('submit', function(){ try { f.by.value = localStorage.getItem('aangan_by') || ''; } catch (e) {} });
  });
  // Back link returns to wherever you came from on this site
  var back = document.querySelector('a.back');
  if (back && document.referrer && document.referrer.indexOf(location.origin) === 0) {
    back.addEventListener('click', function(ev){ ev.preventDefault(); history.back(); });
  }
  // Calls list: group chips + search; day headings follow their rows
  var list = document.getElementById('calls');
  var group = list ? (list.getAttribute('data-initial') || 'all') : 'all', q = '';
  if (list) {
    var rows = Array.prototype.slice.call(list.querySelectorAll('li[data-group]'));
    var days = Array.prototype.slice.call(list.querySelectorAll('li.dayrow'));
    var none = document.getElementById('nomatch');
    var apply = function(){
      var shown = 0, perDay = {};
      rows.forEach(function(r){
        var ok = (group === 'all' || r.getAttribute('data-group') === group) &&
                 (!q || r.textContent.toLowerCase().indexOf(q) !== -1);
        r.hidden = !ok;
        if (ok) { shown++; perDay[r.getAttribute('data-day')] = 1; }
      });
      days.forEach(function(d){ d.hidden = !perDay[d.getAttribute('data-day')]; });
      if (none) none.hidden = shown !== 0;
    };
    document.querySelectorAll('.chip[data-g]').forEach(function(ch){
      ch.addEventListener('click', function(){
        group = ch.getAttribute('data-g');
        document.querySelectorAll('.chip[data-g]').forEach(function(o){ o.setAttribute('aria-pressed', o === ch ? 'true' : 'false'); });
        apply();
      });
    });
    var s = document.getElementById('search');
    if (s) s.addEventListener('input', function(){ q = s.value.trim().toLowerCase(); apply(); });
    apply();
  }
  // Pages with live numbers refresh every 2 minutes while on screen, unless you're in the middle of something
  if (document.body.getAttribute('data-refresh')) {
    setInterval(function(){
      var a = document.activeElement;
      var busy = q || group !== 'all' || document.querySelector('details[open]') || (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA'));
      if (document.visibilityState === 'visible' && !busy) {
        var u = new URL(location.href); u.searchParams.delete('msg'); location.replace(u.toString());
      }
    }, 120000);
  }
  // Drop ?msg= from the address so a reload doesn't repeat the confirmation
  if (location.search.indexOf('msg=') !== -1 && history.replaceState) {
    var u = new URL(location.href); u.searchParams.delete('msg'); history.replaceState(null, '', u.toString());
  }
})();
"""

FAVICON_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='#9a4a2b'/>"
               "<text x='16' y='22' font-family='system-ui,sans-serif' font-size='18' font-weight='700' fill='#fff' "
               "text-anchor='middle'>A</text></svg>")
FAVICON = "data:image/svg+xml," + urllib.parse.quote(FAVICON_SVG)

NAV = [("home", "/dashboard", "Home", "home"), ("calls", "/dashboard/calls", "Calls", "list"),
       ("reports", "/dashboard/reports", "Reports", "chart"), ("setup", "/dashboard/setup", "Setup", "gear")]
PERIODS = (("1", "Today"), ("7", "7 days"), ("30", "30 days"), ("mtd", "This month"))
PERIOD_LABEL = {"1": "today", "7": "in the last 7 days", "30": "in the last 30 days", "mtd": "this month"}
PERIOD_VS = {"1": "yesterday", "7": "previous 7 days", "30": "previous 30 days", "mtd": "same days last month"}


def _viewer_name() -> str:
    v = VIEWER.get() or {}
    return (v.get("name") or v.get("email") or "") if v.get("kind") == "account" else ""


def _user_menu() -> str:
    """Who is signed in, and Sign out. Nothing for the older ?token= links (there's no session to end)."""
    v = VIEWER.get()
    if not v:
        return ""
    if v.get("kind") == "account":
        who, sub = v.get("name") or v.get("email"), v.get("email")
        ini = "".join(p[0].upper() for p in (v.get("name") or v.get("email") or "?").split()[:2] if p[:1].isalnum()) or "?"
    else:
        who, sub, ini = "Studio login", "signed in with the studio password", "A"
    return (f"<details class='usermenu'><summary aria-label='Account: {e(who)}'><span class='uav'>{e(ini[:2])}</span></summary>"
            f"<div class='umenu'><b>{e(who)}</b><span>{e(sub or '')}</span>"
            f"<form method='post' action='/dashboard/logout'><button class='btn small' type='submit'>Sign out</button></form>"
            f"</div></details>")


def _page(title: str, body: str, token: str, active: str | None = None, period: str | None = None,
          flash: str | None = None, refresh: bool = False, todo: int = 0, setup_open: bool = False) -> str:
    tabs = []
    for key, path, label, icon in NAV:
        extra = ""
        if key == "home" and todo:
            extra = f"<span class='count' title='{todo} to do'>{todo}</span>"
        if key == "setup" and setup_open:
            extra = "<span class='pip' title='Something still needs setting up'></span>"
        tabs.append(f"<a class='{'on' if key == active else ''}' href='{e(_q(path, token))}'"
                    f"{' aria-current=page' if key == active else ''}>{_icon(icon)}{label}{extra}</a>")
    seg = ""
    if period is not None and active in ("home", "calls", "reports"):
        path = dict((k, p) for k, p, *_ in NAV)[active]
        seg = "<nav class='seg' aria-label='Time period'>" + "".join(
            f"<a class='{'on' if period == p else ''}' href='{e(_q(path, token, period=p))}'>{label}</a>"
            for p, label in PERIODS) + "</nav>"
    toast = (f"<div class='toast' role='status'>{_icon('check')}{e(FLASH[flash])}</div>" if flash in FLASH else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<link rel="icon" href="{FAVICON}"><title>{e(title)}</title><style>{CSS}</style></head>
<body class="viz-root"{' data-refresh="1"' if refresh else ''}>
<header class="topbar"><div class="wrap"><a class="brand" href="{e(_q('/dashboard', token))}"><span class="mark">A</span>
<span><b>Aangan Studio</b><small>Phone enquiries</small></span></a>
<nav class="tabs" aria-label="Sections">{''.join(tabs)}</nav>{seg}{_user_menu()}</div></header>
<main><div class="wrap">{body}
<p class="footer">Every call to the studio is answered by the agent, day or night. It never quotes a price.</p>
</div></main>{toast}<script>{JS}</script></body></html>"""


LOGIN_CSS = """
.login{min-height:100vh;display:grid;place-items:center;padding:24px 16px}
.login .card{width:100%;max-width:400px;padding:28px}
.login .brand{margin-bottom:22px}
.login label{display:block;font-size:14px;font-weight:600;margin:16px 0 6px}
.login input[type=password],.login input[type=email],.login input[type=text]{width:100%;border:1px solid var(--border);
background:var(--surface-1);color:var(--text-primary);border-radius:10px;padding:11px 12px;font:inherit;font-size:15px}
.login a{color:var(--text-primary);font-weight:600}
.login .ok{background:var(--good-bg);border-radius:10px;padding:9px 12px;font-size:14px;margin-top:12px}
.authtabs{display:flex;gap:2px;background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:3px;margin:16px 0 4px}
.authtabs a{flex:1;text-align:center;padding:7px 10px;border-radius:7px;text-decoration:none;font-weight:500!important;
color:var(--text-secondary)!important;font-size:14px}
.authtabs a.on{background:var(--surface-1);color:var(--text-primary)!important;font-weight:600!important;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.login .btn{width:100%;margin-top:14px;min-height:44px}
.login .err{background:var(--critical-bg);color:var(--critical-text);border-radius:10px;padding:9px 12px;font-size:14px;margin-top:14px}
.login .hint{color:var(--muted);font-size:13px;margin-top:14px;line-height:1.5}
"""


def _auth_page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<link rel="icon" href="{FAVICON}"><title>{e(title)} · Aangan</title><style>{CSS}{LOGIN_CSS}</style></head>
<body class="viz-root"><main class="login"><div class="card">
<div class="brand"><span class="mark">A</span><span><b>Aangan Studio</b><small>Phone enquiries dashboard</small></span></div>
{body}</div></main></body></html>"""


def render_login(next_path: str = "/dashboard", mode: str = "account", error: str | bool | None = None,
                 email: str | None = None, flash: str | None = None) -> str:
    """Two ways in: your own account, or the shared studio password."""
    mode = "studio" if mode == "studio" else "account"
    if error is True:
        error = "That didn't work. Check it and try again."
    err = f"<div class='err' role='alert'>{e(error)}</div>" if error else ""
    note = ("<div class='ok' role='status'>You're signed out.</div>" if flash == "out" else "")
    nxt = e(next_path)
    tab = lambda m, label: (f"<a class='{'on' if mode == m else ''}' href='/dashboard/login?{urllib.parse.urlencode({'mode': m, 'next': next_path})}'"  # noqa: E731
                            f"{' aria-current=page' if mode == m else ''}>{label}</a>")
    if mode == "account":
        form = (f"<form method='post' action='/dashboard/login'><input type='hidden' name='mode' value='account'>"
                f"<input type='hidden' name='next' value='{nxt}'>"
                f"<label for='email'>Email</label><input id='email' name='email' type='email' autocomplete='email' required "
                f"value='{e(email or '')}' {'' if email else 'autofocus'}>"
                f"<label for='password'>Password</label><input id='password' name='password' type='password' "
                f"autocomplete='current-password' required {'autofocus' if email else ''}>"
                f"{err}<button class='btn primary' type='submit'>Sign in</button></form>"
                f"<p class='hint'>New here? <a href='/dashboard/signup?{urllib.parse.urlencode({'next': next_path})}'>Create an account</a></p>")
    else:
        form = (f"<form method='post' action='/dashboard/login'><input type='hidden' name='mode' value='studio'>"
                f"<input type='hidden' name='next' value='{nxt}'>"
                f"<label for='password'>Studio password</label><input id='password' name='password' type='password' "
                f"autocomplete='current-password' required autofocus>"
                f"{err}<button class='btn primary' type='submit'>Open dashboard</button></form>"
                f"<p class='hint'>The shared password for the studio team. Ask Aastha if you don't have it.</p>")
    body = (f"<h1 style='font-size:22px'>Sign in</h1>{note}"
            f"<nav class='authtabs' aria-label='How to sign in'>{tab('account', 'My account')}{tab('studio', 'Studio password')}</nav>"
            f"{form}<p class='hint'>You stay signed in on this browser for 30 days.</p>")
    return _auth_page("Sign in", body)


def render_signup(next_path: str = "/dashboard", form: dict | None = None, error: str | None = None) -> str:
    f = form or {}
    err = f"<div class='err' role='alert'>{e(error)}</div>" if error else ""
    body = (f"<h1 style='font-size:22px'>Create your account</h1>"
            f"<p class='sub' style='margin-top:6px'>For the Aangan team. You'll sign in with your own email and password.</p>"
            f"<form method='post' action='/dashboard/signup'><input type='hidden' name='next' value='{e(next_path)}'>"
            f"<label for='name'>Your name</label><input id='name' name='name' type='text' autocomplete='name' "
            f"value='{e(f.get('name', ''))}' autofocus>"
            f"<label for='email'>Email</label><input id='email' name='email' type='email' autocomplete='email' required "
            f"value='{e(f.get('email', ''))}'>"
            f"<label for='password'>Choose a password</label><input id='password' name='password' type='password' "
            f"autocomplete='new-password' minlength='8' required><div class='hint' style='margin-top:4px'>At least 8 characters.</div>"
            f"<label for='studio_password'>Studio password</label><input id='studio_password' name='studio_password' "
            f"type='password' autocomplete='off' required>"
            f"<div class='hint' style='margin-top:4px'>Only the team knows it, so only the team can join. Ask Aastha.</div>"
            f"{err}<button class='btn primary' type='submit'>Create account</button></form>"
            f"<p class='hint'>Already have an account? <a href='/dashboard/login?{urllib.parse.urlencode({'next': next_path})}'>Sign in</a></p>")
    return _auth_page("Create account", body)


def _delta(cur, prev, good_when_up=True, unit="", vs="previous period"):
    """'▲ 4 vs previous 30 days'. Colour = direction × whether up is good; always with an arrow and a word."""
    if cur is None or prev is None:
        return ""
    diff = cur - prev
    if abs(diff) < 0.5:
        return f"<span class='delta'>Same as {e(vs)}</span>"
    up = diff > 0
    cls = "good" if up == good_when_up else "bad"
    return (f"<span class='delta {cls}'>{'▲' if up else '▼'} {abs(diff):.0f}{unit} "
            f"<span class='vs'>vs {e(vs)}</span></span>")


def _tile(label, value, note="", delta=""):
    return (f"<div class='card tile'><div class='label'>{e(label)}</div><div class='value'>{e(str(value))}</div>"
            f"<div class='note'>{delta or e(note)}</div></div>")


def _round_top(x, y, w, h, r=4):
    """Column segment with a rounded data-end (top) and a square base."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + w - r:.1f}"
            f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z")


def _stacked_svg(per_day) -> str:
    """Calls per day, stacked by group. One axis (counts), 2px surface gaps, rounded top, hover per column."""
    W, H, L, R, T, B = 720, 220, 30, 8, 12, 26
    n = len(per_day)
    if not n:
        return ""
    top = max([1] + [b + f + c for _, b, f, c in per_day])
    step = max(1, -(-top // 4))
    ymax = step * 4
    band = (W - L - R) / n
    bw = max(3.0, min(24.0, band * 0.62))
    y = lambda v: T + (H - T - B) * (1 - v / ymax)  # noqa: E731
    parts = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='Calls per day by outcome'>"]
    for k in range(5):
        v = step * k
        parts.append(f"<line x1='{L}' x2='{W - R}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='var({'--axis' if k == 0 else '--grid'})' stroke-width='1'/>"
                     f"<text x='{L - 8}' y='{y(v) + 4:.1f}' text-anchor='end' font-size='11' fill='var(--muted)'>{v}</text>")
    ticks = sorted({0, n // 2, n - 1}) if n > 2 else list(range(n))
    for i in ticks:
        cx = L + band * (i + 0.5)
        anchor = "start" if i == 0 and n > 2 else "end" if i == n - 1 and n > 2 else "middle"
        tx = cx - bw / 2 if anchor == "start" else cx + bw / 2 if anchor == "end" else cx
        parts.append(f"<text x='{tx:.1f}' y='{H - 7}' text-anchor='{anchor}' font-size='11' fill='var(--muted)'>"
                     f"{per_day[i][0].strftime('%d %b').lstrip('0')}</text>")
    for i, (d, b, f, c) in enumerate(per_day):
        x = L + band * i + (band - bw) / 2
        base = y(0)
        segs = [(v, GROUP_COLOR[g]) for v, (g, _) in zip((b, f, c), GROUPS) if v]
        for j, (v, color) in enumerate(segs):
            h = base - y(v)
            gap = 2 if j < len(segs) - 1 else 0
            top_y = base - h
            if j == len(segs) - 1:
                parts.append(f"<path d='{_round_top(x, top_y, bw, max(0.5, h))}' fill='{color}'/>")
            else:
                parts.append(f"<rect x='{x:.1f}' y='{top_y + gap:.1f}' width='{bw:.1f}' height='{max(0.5, h - gap):.1f}' fill='{color}'/>")
            base = top_y
        tip = {"day": d.strftime("%a %d %b"), "rows": [[label, v, GROUP_COLOR[g]] for (g, label), v in zip(GROUPS, (b, f, c))]
               + [["Total calls", b + f + c, "transparent"]]}
        parts.append(f"<rect x='{L + band * i:.1f}' y='{T}' width='{band:.1f}' height='{H - T - B}' fill='transparent' "
                     f"data-tip='{e(json.dumps(tip))}'><title>{d:%a %d %b}: {b + f + c} calls</title></rect>")
    parts.append("</svg>")
    return "".join(parts)


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else plural or word + 's'}"


def _day_label(d, today):
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return d.strftime("%A %d %B").replace(" 0", " ")


def _greeting(now):
    return "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"


# --- shared pieces ---------------------------------------------------------------------------------

def _quick_actions(c, token, back):
    """Call + Done buttons on a to-do row (Done skips the note; the call page has the full form)."""
    out = []
    if c.get("caller_number"):
        out.append(f"<a class='btn small icon' href='tel:{e(c['caller_number'].replace(' ', ''))}' "
                   f"title='Call {e(c['caller_number'])}' aria-label='Call {e(_caller(c))}'>{_icon('phone')}</a>")
    out.append(f"<form class='quickdone' method='post' action='/dashboard/call/action'>"
               f"<input type='hidden' name='call_id' value='{e(c['call_id'])}'><input type='hidden' name='token' value='{e(token)}'>"
               f"<input type='hidden' name='action' value='done'><input type='hidden' name='by' value=''>"
               f"<input type='hidden' name='back' value='{e(back)}'>"
               f"<button class='btn small' type='submit' title='Mark as done'>{_icon('check')}<span class='hide-sm'>Done</span></button></form>")
    return f"<div class='quick'>{''.join(out)}</div>"


def _call_item(c, token, now, *, todo=None, show_day=False, back="/dashboard", attrs=""):
    """One call as a friendly list row: avatar, name + project, status + next step, when + interest."""
    href = e(_q("/dashboard/call", token, call_id=c["call_id"]))
    ini = _initials(c)
    avatar = f"<span class='avatar' aria-hidden='true'>{e(ini) if ini else _icon('user', 18)}</span>"
    if todo:
        text, urgency = todo
        l2 = (f"{_badge(c)}<span class='{'urgent' if urgency == 'high' else ''}'>"
              f"{'Urgent: ' if urgency == 'high' else ''}{e(text)}</span>")
    else:
        step = next_step(c, now.date())[0]
        fit = _fit(c)
        l2 = f"{_badge(c)}<span>{e(step)}</span>" + (f"<span class='muted hide-sm'>· {e(fit)}</span>"
                                                     if fit and fit not in step and c.get("status") not in ("booked", "escalated")
                                                     else "")
    when = _ago(c.get("started_at") or c.get("created_at"), now) if (todo or show_day) else _clock(c)
    after = (f"<span title='Called after hours (outside 10am–7pm)'>{_icon('moon', 13)}<span class='sr'>after hours</span></span>"
             if c.get("after_hours") else "")
    meta = f"<span class='meta'><span>{e(when)}{after}</span>{_interest(c.get('score'), compact=True)}</span>"
    project = _project(c)
    item = (f"<a class='item' href='{href}'>{avatar}<span class='body'><span class='l1'>{e(_caller(c))}"
            f"{' <span class=muted>· ' + e(project) + '</span>' if project != '—' else ''}</span>"
            f"<span class='l2'>{l2}</span></span>{meta}</a>")
    actions = _quick_actions(c, token, back) if todo else ""
    return f"<li{attrs}>{item}{actions}</li>"


def _clock(c):
    t = _ts(c.get("started_at") or c.get("created_at"))
    return t.astimezone(config.IST).strftime("%I:%M %p").lstrip("0") if t else "—"


# --- Home ------------------------------------------------------------------------------------------

def render_home(m: dict, period: str, token: str, setup: list[dict] | None = None, flash: str | None = None) -> str:
    setup = setup or []
    open_setup = [s for s in setup if s["state"] != "ok"]
    todo = m["attention"]
    urgent = sum(1 for _, _, u in todo if u == "high")
    now = m["now"]
    prev = m.get("prev")
    plabel, vs = PERIOD_LABEL.get(period, ""), PERIOD_VS.get(period, "previous period")

    banner = ""
    if open_setup:
        banner = (f"<div class='banner' role='status'><span class='txt'>{_icon('info', 18)}<span>"
                  f"<b>{_plural(len(open_setup), 'thing')} left to set up</b> before real calls go all the way through.</span></span>"
                  f"<a class='link' href='{e(_q('/dashboard/setup', token))}'>Finish setup {_icon('arrow')}</a></div>")

    if not m["ever"]:
        body = (f"<div class='pagehead'><div><h1>{_greeting(now)}</h1><p class='headline'>Welcome. This is where every call "
                f"to the studio shows up.</p></div></div>{banner}"
                f"<div class='card emptybig'><div class='ok'>{_icon('phone', 22)}</div><h2>No calls yet</h2>"
                f"<p class='sub' style='max-width:480px;margin:8px auto 16px'>Callers talk to the agent from the web call page. "
                f"Each call appears here about a minute after it ends: who called, what they want, and whether a consultation "
                f"was booked.</p><a class='btn primary' href='/call' target='_blank' rel='noopener'>{_icon('phone')}Try a web call</a></div>")
        return _page("Aangan · Home", body, token, "home", period, flash, refresh=True, setup_open=bool(open_setup))

    after = f" ({m['after_hours']} after hours)" if m["after_hours"] else ""
    headline = (f"{plabel[:1].upper() + plabel[1:]}, the agent answered <b>{_plural(m['answered'], 'call')}</b>{after} "
                f"and <b>{_plural(m['booked_any'], 'consultation')}</b> {'was' if m['booked_any'] == 1 else 'were'} booked.")
    if urgent:
        headline += f" <span class='urgent'>{_plural(urgent, 'caller')} need{'s' if urgent == 1 else ''} a call back.</span>"
    elif todo:
        headline += f" {_plural(len(todo), 'thing')} on your to-do list."
    else:
        headline += " Nothing is waiting on you."

    g = m["groups"]
    stats = "".join(
        f"<a class='card stat' href='{e(_q('/dashboard/calls', token, period=period, group=grp))}'>"
        f"<div class='label'>{key}{e(label)}</div><div class='num'>{num}</div><div class='note'>{note}</div></a>"
        for grp, key, label, num, note in (
            ("all", "", "Calls answered", m["answered"], _delta(m["answered"], prev and prev["answered"], vs=vs) or e(f"{m['after_hours']} after hours")),
            ("booked", f"<span class='key' style='background:{GROUP_COLOR['booked']}'></span>", "Consultations booked",
             m["booked_any"], _delta(m["booked_any"], prev and prev["booked_any"], vs=vs)
             or e(f"{m['booked_on_call']} booked during the call itself")),
            ("follow_up", f"<span class='key' style='background:{GROUP_COLOR['follow_up']}'></span>", "Need a follow-up",
             g["follow_up"], e(GROUP_HINT["follow_up"])),
            ("closed", f"<span class='key' style='background:{GROUP_COLOR['closed']}'></span>", "Closed", g["closed"],
             e(GROUP_HINT["closed"])),
        ))

    if todo:
        items = "".join(_call_item(c, token, now, todo=(text, u), back=_q("/dashboard", token, period=period))
                        for c, text, u in todo[:10])
        more = (f"<p class='sub' style='margin-top:10px'><a class='link' href='{e(_q('/dashboard/calls', token, period='7', group='follow_up'))}'>"
                f"See all {len(todo)} {_icon('arrow')}</a></p>") if len(todo) > 10 else ""
        todo_html = f"<ul class='list'>{items}</ul>{more}"
    else:
        todo_html = (f"<div class='emptybig'><div class='ok'>{_icon('check', 22)}</div><h3>All caught up</h3>"
                     f"<p class='sub'>Every recent caller is booked or closed.</p></div>")

    recent = "".join(_call_item(c, token, now, show_day=True) for c in m["rows"][:6]) or "<li class='empty'>No calls in this period.</li>"
    top_reasons = "".join(f"<li style='display:flex;justify-content:space-between;padding:6px 0'><span>{e(REASON_SHORT.get(r, r))}</span>"
                          f"<b>{n}</b></li>" for r, n in sorted(m["reasons"], key=lambda x: -x[1])[:3])
    body = (f"<div class='pagehead'><div><h1>{_greeting(now)}</h1><p class='headline'>{headline}</p></div>"
            f"<a class='btn' href='/call' target='_blank' rel='noopener' title='Opens the page callers use'>{_icon('phone')}"
            f"Start a web call</a></div>{banner}"
            f"<div class='stats'>{stats}</div>"
            f"<div class='grid2'><div class='card'><div class='cardhead'><div><h2>Your to-do list</h2>"
            f"<p class='sub'>Callers someone at the studio should get back to (last 7 days)</p></div></div>{todo_html}</div>"
            f"<div class='stack'><div class='card'><div class='cardhead'><h2>Latest calls</h2>"
            f"<a class='link' href='{e(_q('/dashboard/calls', token, period=period))}'>All calls {_icon('arrow')}</a></div>"
            f"<ul class='list'>{recent}</ul></div>"
            + (f"<div class='card'><div class='cardhead'><h2>Most common reasons for not booking</h2></div>"
               f"<ul class='list' style='font-size:14px'>{top_reasons}</ul>"
               f"<p class='sub' style='margin-top:8px'><a class='link' href='{e(_q('/dashboard/reports', token, period=period))}'>"
               f"Full report {_icon('arrow')}</a></p></div>" if top_reasons else "")
            + "</div></div>")
    return _page("Aangan · Home", body, token, "home", period, flash, refresh=True, todo=len(todo),
                 setup_open=bool(open_setup))


render_dashboard = render_home   # older name


# --- Calls -----------------------------------------------------------------------------------------

def render_calls(m: dict, period: str, token: str, group: str = "all", setup_open: bool = False) -> str:
    now, g = m["now"], m["groups"]
    group = group if group in ("all", "booked", "follow_up", "closed") else "all"
    chips = "".join(f"<button type='button' class='chip' data-g='{k}' aria-pressed='{'true' if k == group else 'false'}'"
                    f" title='{e(GROUP_HINT.get(k, 'every call'))}'>"
                    f"{'' if k == 'all' else '<span class=key style=background:' + GROUP_COLOR[k] + '></span>'}"
                    f"{label} <span class='c'>{n}</span></button>"
                    for k, label, n in [("all", "All", m["calls"])] + [(k, label, g[k]) for k, label in GROUPS])
    by_day: dict = {}
    for c in m["rows"][:400]:
        t = _ts(c.get("started_at") or c.get("created_at"))
        by_day.setdefault(t.astimezone(config.IST).date() if t else now.date(), []).append(c)
    rows = []
    for d, cs in by_day.items():
        booked = sum(1 for c in cs if group_of(c) == "booked")
        rows.append(f"<li class='dayrow' data-day='{d}'>{e(_day_label(d, now.date()))} "
                    f"<span>· {_plural(len(cs), 'call')}{f' · {booked} booked' if booked else ''}</span></li>")
        rows.extend(_call_item(c, token, now, attrs=f" data-group='{group_of(c)}' data-day='{d}'") for c in cs)
    export = e(_q("/dashboard/export.csv", token, period=period))
    body = (f"<div class='pagehead'><div><h1>Calls</h1><p class='headline'>{_plural(m['calls'], 'call')} "
            f"{PERIOD_LABEL.get(period, '')}. Tap any call to see what was said and what happens next.</p></div>"
            f"<a class='btn' href='{export}'>{_icon('download')}Download spreadsheet</a></div>"
            f"<div class='card'><div class='toolbar'><div class='chips' role='group' aria-label='Show'>{chips}</div>"
            f"<div class='right'><label class='sr' for='search'>Search calls</label>"
            f"<input id='search' class='search' type='search' placeholder='Search by name, area or number'></div></div>"
            f"<ul class='list' id='calls' data-initial='{group}'>{''.join(rows)}"
            f"<li id='nomatch' class='empty' hidden>No calls match. Try another filter or a shorter search.</li></ul>"
            + ("" if rows else "<p class='empty'>No calls in this period. Pick a longer period at the top.</p>") + "</div>")
    return _page("Aangan · Calls", body, token, "calls", period, refresh=True, todo=len(m["attention"]),
                 setup_open=setup_open)


# --- Reports ---------------------------------------------------------------------------------------

def render_reports(m: dict, period: str, token: str, setup_open: bool = False) -> str:
    g, prev = m["groups"], m.get("prev")
    plabel, vs = PERIOD_LABEL.get(period, ""), PERIOD_VS.get(period, "previous period")
    base = max(1, m["answered"])
    funnel = "".join(
        f"<div class='fstep'><div class='label'>{label}</div><div class='v'>{v}</div><div class='n'>{note}</div>"
        f"<div class='rail' title='{v} of {m['answered']} answered calls'><span style='width:{100 * v / base:.0f}%'></span></div></div>"
        for label, v, note in (
            ("Calls answered", m["answered"], f"{m['after_hours']} after hours"),
            ("Real conversations", m["talked"], "the caller said what they need"),
            ("Good fit", m["qualified"], f"{_fmt_pct(m['qualified_pct'])} of conversations"),
            ("Booked", m["booked_any"], f"{m['cancelled'] + m['no_show']} later cancelled or didn't show"),
        ))
    trend_table = "".join(f"<tr><td>{d:%a %d %b}</td><td class='num'>{b}</td><td class='num'>{f}</td><td class='num'>{c}</td></tr>"
                          for d, b, f, c in m["per_day_groups"])
    chart = (f"<div class='card'><div class='cardhead'><h2>Calls each day</h2><div class='legend'>"
             + "".join(f"<span title='{e(GROUP_HINT[k])}'><span class='key' style='background:{GROUP_COLOR[k]}'></span>{label}</span>"
                       for k, label in GROUPS)
             + f"</div></div><div class='chart'>{_stacked_svg(m['per_day_groups'])}<div class='tip' role='tooltip'></div></div>"
             f"<details class='tv'><summary>Show as a table</summary><div class='tablewrap'><table><thead><tr><th>Day</th>"
             f"<th class='num'>Booked</th><th class='num'>Follow-up</th><th class='num'>Closed</th></tr></thead>"
             f"<tbody>{trend_table}</tbody></table></div></details></div>")
    top = max([1] + [c for _, c in m["reasons"]])
    bars = "".join(f"<div class='row'><div class='name'>{e(REASON_SHORT.get(r, r))}</div><div class='track'>"
                   f"<div class='fill' style='width:{max(2, 85 * c / top):.1f}%' title='{e(r)}: {c}'></div>"
                   f"<span class='val'>{c}</span></div></div>" for r, c in m["reasons"]) \
        or "<p class='empty'>No callers turned away in this period.</p>"
    reasons = (f"<div class='card hbars'><div class='cardhead'><h2>Why calls weren't booked</h2></div>{bars}"
               f"<p class='sub' style='margin-top:12px'>Designers see each of these in the 7pm email and can overturn any of "
               f"them from the call's page.</p></div>")

    speed = "".join([
        _tile("Time to answer", _fmt_secs(m["median_answer_sec"]), "typical call; the goal is under 5 minutes"),
        _tile("After-hours calls", m["after_hours"], "outside 10am–7pm", _delta(m["after_hours"], prev and prev["after_hours"], vs=vs)),
        _tile("Good-fit rate", _fmt_pct(m["qualified_pct"]), "of real conversations",
              _delta(m["qualified_pct"], prev and prev["qualified_pct"], unit=" pts", vs=vs)),
        _tile("Call to booking", "—" if m["median_call_to_booking_min"] is None else f"{m['median_call_to_booking_min']:.0f} min",
              "typical time from hello to a booked slot"),
    ])
    after = "".join([
        _tile("Cancelled / didn't show", f"{m['cancelled']} / {m['no_show']}", "the front desk calls them back"),
        _tile("Projects won / lost", f"{m['won']} / {m['lost']}", "from the designers' HubSpot deals"),
        _tile("Designer had to re-ask", _fmt_pct(m["reask_pct"]), f"of {_plural(m['report_cards'], 'report card')}"),
        _tile("Existing clients escalated", m["escalations"], "studio head alerted at once"),
        _tile("Price questions deflected", m["guard_blocks"], "a price never reaches the caller"),
    ])
    cost_note = "add the price rates on the Setup page" if m["cost_missing"] else plabel
    cost = "".join([
        _tile("Running cost", _fmt_inr(m["total_cost_inr"]), cost_note),
        _tile("Cost per call", _fmt_inr(m["cost_per_call_inr"]), "phone line + AI + email"),
        _tile("Cost per booked consultation", _fmt_inr(m["cost_per_booking_inr"]), "running cost ÷ bookings"),
    ])
    body = (f"<div class='pagehead'><div><h1>Reports</h1><p class='headline'>How the agent is doing {plabel}: "
            f"<b>{_plural(m['booked_any'], 'consultation')}</b> booked, {m['booked_on_call']} of them during the call "
            f"{_delta(m['booked_any'], prev and prev['booked_any'], vs=vs)}</p></div>"
            f"<a class='btn' href='{e(_q('/dashboard/export.csv', token, period=period))}'>{_icon('download')}Download spreadsheet</a></div>"
            f"<div class='card'><div class='cardhead'><h2>Where the calls went</h2></div><div class='funnel'>{funnel}</div></div>"
            f"<div class='grid2'>{chart}{reasons}</div>"
            f"<p class='section-title'>Speed and coverage</p><div class='tiles'>{speed}</div>"
            f"<p class='section-title'>After the booking</p><div class='tiles'>{after}</div>"
            f"<p class='section-title'>Cost</p><div class='tiles'>{cost}</div>")
    return _page("Aangan · Reports", body, token, "reports", period, refresh=True, todo=len(m["attention"]),
                 setup_open=setup_open)


# --- Setup -----------------------------------------------------------------------------------------

def setup_status(store, now: datetime | None = None) -> list[dict]:
    """Plain-language checklist of what's connected. state: ok | warn | no. Reads config + recent events only."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=30)).isoformat(timespec="seconds")

    def stats(kind, prefix=None):
        try:
            n, t = store.event_stats(kind, since, prefix)
            return n, (t.isoformat() if hasattr(t, "isoformat") else t)
        except Exception:  # noqa: BLE001 — the setup page must render even if the log can't be read
            return 0, None

    vaani, vaani_t = stats("vaani_webhook")
    cal_a, cal_at = stats("calcom_webhook")
    cal_b, cal_bt = stats("calendly_webhook")
    cal, cal_t = cal_a + cal_b, max(cal_at or "", cal_bt or "") or None
    failed, _ = stats("email_failed")
    digests, digest_t = stats("email_sent", "Aangan daily digest")
    when = lambda iso: _when({"started_at": iso}) if iso else "never"  # noqa: E731
    out = []

    web, web_t = stats("webcall_started")
    web_done, _ = stats("webcall_ended")
    agent_ready = bool(config.VAANI_API_KEY and config.env("VAANI_AGENT_ID"))
    page = (config.PUBLIC_BASE_URL or "") + "/call"
    out.append({
        "name": "Web calls", "state": "ok" if web_done or vaani else "warn" if agent_ready else "no",
        "what": (f"Callers talk to the agent from {page} (no phone number needed). Last web call: {when(web_t)}."
                 if web_done or vaani else
                 f"The call page is ready at {page}, but no web call has been completed yet." if agent_ready else
                 "The Vaani agent isn't connected, so web calls can't start."),
        "fix": None if web_done or vaani else [
            f"Open {page}, click Start web call, allow the microphone, and talk to the agent as a caller would.",
            "Hang up; within about a minute the call appears on the Home page with its outcome.",
            "Then share the page link (website, Instagram bio, WhatsApp) so callers can use it."] if agent_ready else [
            "Set the Vaani API key and agent id."],
        "setting": "VAANI_API_KEY, VAANI_AGENT_ID"})

    provider = config.BOOKING_PROVIDER
    out.append({
        "name": "Booking calendar", "state": "ok" if cal else "no",
        "what": ("The agent books consultations on the call, and bookings are flowing back here. "
                 f"Last booking update: {when(cal_t)}." if cal else
                 "No booking has come back from the calendar yet. Until the agent can book, good-fit callers get "
                 "a call back from the front desk instead."),
        "fix": None if cal else [
            "Create the \"Aangan Design Consultation\" event in Cal.com (site visit or studio, 60 minutes).",
            "In Vaani → Settings → Integrations → Cal.com, connect Cal.com and pick that event.",
            "Register this app's booking webhook in Cal.com.",
            "Make one test booking on a call and check it appears on that call's page."],
        "setting": "CALCOM_EVENT_TYPE_ID, CALCOM_WEBHOOK_SECRET" if provider == "calcom" else
                   "CALENDLY_EVENT_TYPE_URI, CALENDLY_WEBHOOK_SIGNING_KEY"})

    sender = config.EMAIL_FROM or ""
    test_sender = sender.endswith("@resend.dev>") or sender.endswith("@resend.dev")
    email_state = "no" if not config.RESEND_API_KEY else "warn" if (test_sender or failed) else "ok"
    recipients = ", ".join(config.DESIGNER_EMAILS) or "nobody"
    what = (f"Report cards, alerts and the 7pm email are sent from \"{sender}\" to {recipients}.")
    if test_sender:
        what += (" That is Resend's test address: it only delivers to the Resend account owner's own inbox, "
                 "and the sender name may not be the studio's.")
    if failed:
        what += f" {_plural(failed, 'email')} failed in the last 30 days (see the call pages)."
    out.append({
        "name": "Email", "state": email_state, "what": what if config.RESEND_API_KEY else "Email isn't connected, so nobody is notified.",
        "fix": None if email_state == "ok" else [
            "Verify the studio's own domain in Resend.",
            "Change the sender to something like \"Aangan Studio <agent@yourdomain>\".",
            "List every designer's email for report cards and the 7pm email."],
        "setting": "RESEND_API_KEY, EMAIL_FROM, DESIGNER_EMAILS, STUDIO_HEAD_ALERT_EMAIL, FRONT_DESK_EMAIL"})

    out.append({
        "name": "7pm email to designers", "state": "ok" if digests else "warn",
        "what": (f"Sent every evening with the calls that weren't booked. Last sent: {when(digest_t)}." if digests else
                 "Hasn't been sent in the last 30 days."),
        "fix": None if digests else ["It goes out automatically between 7 and 8pm once email works."],
        "setting": "CRON_SECRET"})

    out.append({
        "name": "HubSpot", "state": "ok" if config.HUBSPOT_TOKEN else "no",
        "what": ("Every booked consultation becomes a HubSpot deal (with no amount, since prices are never quoted)."
                 + ("" if config.HUBSPOT_PORTAL_ID else " Add the HubSpot account id to make deals clickable here.")
                 if config.HUBSPOT_TOKEN else "Not connected, so booked calls don't reach HubSpot."),
        "fix": None if config.HUBSPOT_TOKEN else ["Create a HubSpot private app with contacts and deals access."],
        "setting": "HUBSPOT_ACCESS_TOKEN, HUBSPOT_DEAL_STAGE_CONSULTATION_BOOKED, HUBSPOT_PORTAL_ID (optional)"})

    missing = []
    if config.VAANI_INR_PER_MIN is None:
        missing.append("Vaani price per minute")
    if config.GEMINI_USD_PER_MTOK_IN is None or config.GEMINI_USD_PER_MTOK_OUT is None:
        missing.append("Gemini token prices")
    if config.USD_INR_RATE is None:
        missing.append("dollar-to-rupee rate")
    out.append({
        "name": "Cost tracking", "state": "ok" if not missing else "warn",
        "what": ("Each call's cost is worked out from the phone minutes, AI usage and emails." if not missing else
                 "Cost per call can't be shown until these rates are added: " + ", ".join(missing) + "."),
        "fix": None if not missing else ["Copy the rates from your Vaani plan and Google's Gemini price page."],
        "setting": "COST_VAANI_INR_PER_MIN, COST_GEMINI_USD_PER_MTOK_IN, COST_GEMINI_USD_PER_MTOK_OUT, USD_INR_RATE"})
    return out


def render_setup(items: list[dict], token: str, todo: int = 0) -> str:
    done = sum(1 for s in items if s["state"] == "ok")
    mark = {"ok": "✓", "warn": "!", "no": "×"}
    word = {"ok": "Working", "warn": "Needs a look", "no": "Not set up"}
    rows = "".join(
        f"<li><span class='st {s['state']}' aria-hidden='true'>{mark[s['state']]}</span><div>"
        f"<h3>{e(s['name'])} <span class='sub' style='font-weight:400'>· {word[s['state']]}</span></h3>"
        f"<div class='what'>{e(s['what'])}</div>"
        + (f"<div class='fix'><b>To fix</b><ol>{''.join('<li>' + e(x) + '</li>' for x in s['fix'])}</ol>"
           f"<div class='muted' style='margin-top:6px'>Settings involved: <code>{e(s['setting'])}</code></div></div>"
           if s.get("fix") else "")
        + "</div></li>" for s in items)
    body = (f"<div class='pagehead'><div><h1>Setup</h1><p class='headline'>{done} of {len(items)} parts are working. "
            f"This page checks itself every time you open it.</p>"
            f"<div class='progress' style='max-width:420px'><span style='width:{100 * done / max(1, len(items)):.0f}%'></span></div>"
            f"</div><a class='btn primary' href='/call' target='_blank' rel='noopener'>{_icon('phone')}Try a web call</a></div>"
            f"<div class='card'><ul class='checklist'>{rows}</ul></div>"
            f"<p class='sub' style='margin-top:14px'>Settings live in Vercel → Project → Settings → Environment Variables. "
            f"After changing one, redeploy for it to take effect.</p>")
    return _page("Aangan · Setup", body, token, "setup", todo=todo, setup_open=done < len(items))


def setup_gaps() -> list[str]:   # older helper, kept for scripts
    return [s for s in ([] if config.CALCOM_EVENT_TYPE_ID or config.BOOKING_PROVIDER != "calcom" else
                        ["Booking calendar is not set up"])]


# --- CSV export ------------------------------------------------------------------------------------

def render_csv(rows: list[dict], now: datetime) -> str:
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Time (IST)", "Caller", "Phone", "Email", "Project", "Area", "Verdict", "Priority",
                "Reason", "Status", "Next step", "Interest score", "Consultation", "Designer", "HubSpot deal",
                "After hours", "Duration (sec)", "Call id"])
    for c in rows:
        f = c.get("fields") or {}
        t = _ts(c.get("started_at") or c.get("created_at"))
        t = t.astimezone(config.IST) if t else None
        w.writerow([t.strftime("%Y-%m-%d") if t else "", t.strftime("%H:%M") if t else "", _caller(c),
                    c.get("caller_number") or "", c.get("invitee_email") or "", _project(c), f.get("location_text") or "",
                    FRIENDLY_DECISION.get(c.get("decision") or "", ""), "High" if c.get("priority") == "P1" else "",
                    REASON_SHORT.get(c.get("reason_code") or "", ""), BADGES.get(c.get("status") or "", ("", "", ""))[2],
                    next_step(c, now.date())[0], c.get("score") if c.get("score") is not None else "",
                    _slot(c.get("slot_start")) if c.get("slot_start") else "", c.get("designer_name") or c.get("designer_email") or "",
                    c.get("hubspot_deal_id") or "", "yes" if c.get("after_hours") else "no", c.get("duration_sec") or "",
                    c["call_id"]])
    return "﻿" + out.getvalue()      # BOM so Excel opens the ₹ and names correctly


# --- one call --------------------------------------------------------------------------------------

EVENT_LABELS = {
    "email_sent": "Email sent", "email_failed": "Email FAILED", "calcom_webhook": "Calendar update",
    "calendly_webhook": "Calendar update", "recording": "Call recording", "speech_guard_block": "Price question deflected",
    "price_spoken": "Agent spoke a price", "hubspot_failed": "HubSpot update FAILED", "error": "Error",
    "vaani_webhook": "Phone system", "dashboard_done": "Marked as done", "dashboard_reopen": "Reopened",
    "dashboard_overturn": "Rejection overturned", "booking_matched": "Booking linked to this call",
    "hubspot_updated": "HubSpot deal updated",
}


def _event_line(ev: dict) -> str:
    kind = ev.get("kind") or ""
    p = ev.get("payload") or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    label = EVENT_LABELS.get(kind, kind.replace("_", " ").capitalize())
    bad = kind in ("email_failed", "hubspot_failed", "error", "price_spoken")
    if kind in ("email_sent", "email_failed"):
        detail = f"{p.get('subject') or ''} → {', '.join(p.get('to') or [])}"
        if kind == "email_failed":
            detail += f" ({p.get('error') or ''})"
    elif kind.endswith("_webhook") and kind != "vaani_webhook":
        detail = f"booking {p.get('kind') or ''}"
    elif kind == "vaani_webhook":
        detail = (p.get("event") or "").replace("_", " ")
    elif kind == "recording" and p.get("url"):
        return (f"<span>{e(label)}: <a href='{e(p['url'])}' target='_blank' rel='noopener'>listen</a>"
                f"{' · ' + e(p['summary']) if p.get('summary') else ''}</span>")
    elif kind == "speech_guard_block":
        detail = "the agent's line was replaced before the caller heard it"
    elif kind.startswith("dashboard_"):
        detail = " · ".join(x for x in ((f"by {p['by']}" if p.get("by") else ""), p.get("note") or "") if x)
    elif kind == "booking_matched":
        detail = f"matched by {p.get('by') or 'the call details'}"
    elif kind == "hubspot_updated":
        detail = p.get("note") or ""
    else:
        detail = p.get("error") or ""
    return f"<span><span class='{'bad' if bad else ''}'>{e(label)}</span>{': ' + e(detail) if detail else ''}</span>"


def _transcript_html(t: str) -> str:
    if not t:
        return "<p class='empty'>No transcript. The caller hung up before speaking, or the call is still being processed.</p>"
    out = []
    for line in t.splitlines():
        if not line.strip():
            continue
        who, _, text = line.partition(":")
        role = who.strip().lower()
        if role in ("agent", "caller") and text:
            out.append(f"<div class='msg {role}'><span class='who'>{'Agent' if role == 'agent' else 'Caller'}</span>{e(text.strip())}</div>")
        elif out:
            out.append(f"<div class='msg agent'>{e(line)}</div>")
    return f"<div class='chat'>{''.join(out)}</div>"


def _handle_box(c: dict, token: str) -> str:
    """Mark as done / reopen / overturn. Plain HTML forms, so they work without JavaScript."""
    back = _q("/dashboard/call", token, call_id=c["call_id"])

    def form(action, label, primary=False, fields=True, placeholder="What happened? e.g. Called back, booked for Tuesday"):
        inputs = (f"<input name='note' placeholder='{e(placeholder)}' aria-label='Note'>"
                  f"<input name='by' placeholder='Your name' aria-label='Your name' value='{e(_viewer_name())}'>") if fields else ""
        return (f"<form method='post' action='/dashboard/call/action'>"
                f"<input type='hidden' name='call_id' value='{e(c['call_id'])}'><input type='hidden' name='token' value='{e(token)}'>"
                f"<input type='hidden' name='action' value='{action}'><input type='hidden' name='back' value='{e(back)}'>{inputs}"
                f"<button class='btn{' primary' if primary else ''}' type='submit'>"
                f"{_icon('undo' if action == 'reopen' else 'check')}{e(label)}</button></form>")

    if c.get("status") == "booked":
        return ""
    if c.get("handled_at"):
        who = f" by {e(c['handled_by'])}" if c.get("handled_by") else ""
        note = f" · “{e(c['handled_note'])}”" if c.get("handled_note") else ""
        return (f"<div class='handle'><div class='hint'>Done{who}, {e(_when({'started_at': c['handled_at']}))}{note}</div>"
                f"{form('reopen', 'Put it back on the to-do list', fields=False)}</div>")
    if group_of(c) == "follow_up":
        return (f"<div class='handle'><div class='hint'>When it's dealt with, mark it done. It leaves the to-do list.</div>"
                f"{form('done', 'Mark as done', primary=True)}</div>")
    if c.get("decision") in ("Not qualified", "Nurture"):
        return (f"<div class='handle'><div class='hint'>Think this caller is worth it after all? Overturn the agent's decision "
                f"and they go on the to-do list for a call back.</div>"
                f"{form('overturn', 'Overturn: call them back', placeholder='Why? e.g. Kharadi is fine for us')}</div>")
    return ""


def render_call(c: dict, events: list[dict] | None = None, token: str = "", flash: str | None = None) -> str:
    f = c.get("fields") or {}
    now = config.now_ist()
    icon = {"pass": "✓", "fail": "×", "unclear": "?"}
    word = {"pass": "Yes", "fail": "No", "unclear": "Not clear"}
    checks = "".join(
        f"<li><span class='ci {g['status']}' aria-hidden='true'>{icon.get(g['status'], '?')}</span><div>"
        f"<div class='n'>{e(g['name'])} <span class='sub' style='font-weight:400'>· {word.get(g['status'], g['status'])}</span></div>"
        f"<div class='d'>{e(REASON_SHORT.get(g.get('reason_code') or '', ''))}{' — ' if g.get('reason_code') and g.get('note') else ''}"
        f"{e(g.get('note') or '')}</div></div></li>"
        for g in c.get("gates") or []) or "<li class='empty'>Not processed yet.</li>"
    factors = "".join(
        f"<div class='factor'><div class='top'><b>{e(l['factor'])}</b><span>{l['points']} of {l['max_points']}</span></div>"
        f"<div class='bar'><span style='width:{100 * l['points'] / max(1, l['max_points']):.0f}%'></span></div>"
        f"{'<q>' + e(l['quote']) + '</q>' if l.get('quote') else ''}</div>"
        for l in c.get("score_lines") or []) or "<p class='empty'>No score yet.</p>"
    flags = "".join(f"<li>{e(x)}</li>" for x in c.get("flags") or [])

    rooms = ", ".join(f.get("rooms") or [])
    size = f"{f['size_sqft']:,.0f} sq ft" if f.get("size_sqft") else ""
    chips = [("", _project(c))] if _project(c) != "—" else []
    chips += [("", x) for x in (size, rooms) if x]
    if f.get("timeline_text"):
        chips.append(("When: ", f["timeline_text"]))
    dm = {"owner_or_authorised": "Owner", "deciders_will_attend": "Deciders will attend",
          "researching_for_someone": "Researching for someone else"}.get(f.get("decision_maker") or "")
    if dm:
        chips.append(("", dm))
    if f.get("referrer_name"):
        chips.append(("Referred by ", f["referrer_name"]))
    fact_chips = "".join(f"<span class='fact'>{e(k)}<b>{e(str(v))}</b></span>" for k, v in chips)

    slot = f" for {_slot(c.get('slot_start'))}" if c.get("slot_start") else ""
    if c.get("status") == "booked":
        why = f"A good fit, and booked{slot}."
    elif c.get("status") == "cancelled":
        why = f"A good fit and booked{slot}, but the caller cancelled. The front desk should call them back."
    elif c.get("status") == "no_show":
        why = f"A good fit and booked{slot}, but the caller didn't show up."
    elif c.get("decision") == "Qualified":
        why = "A good fit, but no slot was booked on the call."
    elif c.get("decision") == "Escalate":
        why = "An existing client with a problem. The studio head was alerted straight away."
    else:
        why = plain_reason(c) or next_step(c)[0]
        why = why[:1].upper() + why[1:]
    if c.get("priority") == "P1" and c.get("priority_reasons"):
        why += " High priority (" + "; ".join(c["priority_reasons"]) + ")."

    step_text, urgency = next_step(c, now.date())
    if c.get("status") == "booked":
        todo, cls = f"Consultation {_slot(c.get('slot_start'))}" if c.get("slot_start") else "Consultation booked", "ok"
        hint = "The designer has the report card. Nothing else to do."
    elif c.get("handled_at"):
        todo, cls, hint = "Nothing. This one's done.", "ok", ""
    elif urgency in ("high", "medium"):
        todo, cls, hint = step_text, "high" if urgency == "high" else "", ""
    elif group_of(c) == "follow_up":
        todo, cls, hint = step_text, "", ""
    else:
        todo, cls, hint = "Nothing. The agent closed this call politely.", "ok", "It's in the designers' 7pm email."

    acts = []
    if c.get("caller_number"):
        acts.append(f"<a class='btn {'primary' if cls != 'ok' else ''}' href='tel:{e(c['caller_number'].replace(' ', ''))}'>"
                    f"{_icon('phone')}Call {e(c['caller_number'])}</a>")
    if c.get("invitee_email"):
        subject = urllib.parse.quote("Your consultation with Aangan Studio")
        acts.append(f"<a class='btn' href='mailto:{e(c['invitee_email'])}?subject={subject}'>{_icon('mail')}Email</a>")
    if c.get("recording_url"):
        acts.append(f"<a class='btn' href='{e(c['recording_url'])}' target='_blank' rel='noopener'>{_icon('play')}Listen</a>")
    if c.get("hubspot_deal_id") and config.HUBSPOT_PORTAL_ID:
        acts.append(f"<a class='btn' href='https://app.hubspot.com/contacts/{e(config.HUBSPOT_PORTAL_ID)}/record/0-3/"
                    f"{e(str(c['hubspot_deal_id']))}' target='_blank' rel='noopener'>{_icon('ext')}HubSpot</a>")
    if c.get("reschedule_url") and c.get("status") == "booked":
        acts.append(f"<a class='btn' href='{e(c['reschedule_url'])}' target='_blank' rel='noopener'>{_icon('ext')}Reschedule</a>")

    nextcard = (f"<div class='nextcard {cls}'><div class='label'>What happens next</div><p class='todo'>{e(todo)}</p>"
                + (f"<p class='sub'>{e(hint)}</p>" if hint else "")
                + (f"<div class='actions'>{''.join(acts)}</div>" if acts else "") + _handle_box(c, token) + "</div>")

    booking = [
        ("Consultation", _slot(c.get("slot_start")) if c.get("slot_start") else "Not booked"),
        ("Where", {"site_visit": "Site visit", "studio": "At the studio"}.get(c.get("visit_type") or "", "—")),
        ("Designer", c.get("designer_name") or c.get("designer_email") or "—"),
        ("Report card to designer", "Sent " + _when({"started_at": c["report_card_sent_at"]}) if c.get("report_card_sent_at") else "Not sent"),
        ("In the 7pm email", _when({"started_at": c["digest_sent_at"]}) if c.get("digest_sent_at") else "—"),
        ("HubSpot", _hubspot(c.get("hubspot_deal_id"))),
        ("Phone", c.get("caller_number") or "—"),
        ("Email", c.get("invitee_email") or "—"),
        ("Call length", _fmt_dur(c.get("duration_sec"))),
        ("How they found us", (f.get("source") or "unknown").replace("_", " ").capitalize()),
    ]
    if c.get("status") == "nurture" and c.get("follow_up_on"):
        booking.insert(0, ("Follow up on", _date(c["follow_up_on"])))
    dl = "".join(f"<div><dt>{e(k)}</dt><dd>{v if k == 'HubSpot' else e(str(v))}</dd></div>" for k, v in booking)

    timeline = "".join(
        f"<li><span class='t'>{e(_ago(ev.get('created_at'), now))}</span>{_event_line(ev)}</li>"
        for ev in (events or []) if ev.get("kind") != "vaani_webhook") or "<li class='empty'>Nothing yet.</li>"
    score = c.get("score")
    ini = _initials(c)
    summary = (f"<p class='sub' style='margin-top:10px'>Phone system's summary: {e(c['summary'])}</p>" if c.get("summary") else "")
    body = (f"<a class='back' href='{e(_q('/dashboard/calls', token))}'>{_icon('back')}Back</a>"
            f"<div class='callhead'><span class='avatar' aria-hidden='true'>{e(ini) if ini else _icon('user', 24)}</span>"
            f"<div><h1>{e(_caller(c))}</h1><div class='meta'>{_badge(c)}<span>{e(_when(c))}"
            f"{' · after hours' if c.get('after_hours') else ''}</span>{_interest(score)}</div></div></div>"
            f"{nextcard}"
            f"<div class='card' style='margin-bottom:16px'><div class='cardhead'><h2>What the agent found</h2></div>"
            f"<p class='why'>{e(why)}</p>{summary}<div class='facts-inline'>{fact_chips}</div>"
            + (f"<ul class='flags'>{flags}</ul>" if flags else "") + "</div>"
            f"<div class='grid2' style='margin-top:0'><div class='stack'>"
            f"<div class='card'><div class='cardhead'><h2>The conversation</h2></div>{_transcript_html(c.get('transcript') or '')}</div>"
            f"<div class='card'><div class='cardhead'><h2>Details</h2></div><dl class='facts'>{dl}</dl></div></div>"
            f"<div class='stack'><div class='card'><div class='cardhead'><h2>How the agent decided</h2></div>"
            f"<p class='sub' style='margin:-6px 0 8px'>A caller needs a yes on all five to be booked.</p><ul class='checks'>{checks}</ul>"
            f"<details class='more'><summary>How interested they sounded: {score if score is not None else '—'} of 100</summary>"
            f"<p class='sub' style='margin:8px 0 4px'>Every point is backed by something the caller said.</p>{factors}</details></div>"
            f"<div class='card'><div class='cardhead'><h2>What's happened since</h2></div><ul class='timeline'>{timeline}</ul></div>"
            f"</div></div>")
    return _page(f"{_caller(c)} · Aangan", body, token, "calls", flash=flash)


def _hubspot(deal_id):
    if not deal_id:
        return "—"
    if config.HUBSPOT_PORTAL_ID:
        url = f"https://app.hubspot.com/contacts/{config.HUBSPOT_PORTAL_ID}/record/0-3/{deal_id}"
        return f"<a href='{e(url)}' target='_blank' rel='noopener'>Open deal {e(str(deal_id))}</a>"
    return e(f"Deal {deal_id}")
