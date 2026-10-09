"""Nikhil's dashboard (build step 9): what the agent handled, what it booked, and what it cost.

compute_metrics() is pure (testable); render_*() return HTML strings (server-rendered, inline SVG, light/dark).
Costs are computed here from raw usage stored per call, so changing a rate never needs a data migration.

Every call falls in one of three groups, used by the chart, the filters and the table:
  booked     a consultation is on the calendar
  follow_up  someone at the studio has to act (slot to confirm, escalation, cancellation, no-show, missed call, nurture)
  closed     not taken forward (not a fit, or no conversation)
"""
import html
import json
import statistics
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config
from .calendly import parse_time, spoken_label
from .emails import plain_reason

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


def setup_gaps() -> list[str]:
    """Settings this deployment still needs before a real call can go all the way through."""
    gaps = []
    if config.BOOKING_PROVIDER == "calcom" and not config.CALCOM_EVENT_TYPE_ID:
        gaps.append("Booking calendar: CALCOM_EVENT_TYPE_ID is not set, so the agent cannot offer or book slots")
    if config.BOOKING_PROVIDER == "calendly" and not config.CALENDLY_EVENT_TYPE_URI:
        gaps.append("Booking calendar: CALENDLY_EVENT_TYPE_URI is not set, so the agent cannot offer or book slots")
    if not config.RESEND_API_KEY:
        gaps.append("Email: RESEND_API_KEY is not set, so no report cards, digests or alerts are sent")
    if not config.HUBSPOT_TOKEN:
        gaps.append("CRM: HUBSPOT_ACCESS_TOKEN is not set, so booked calls don't reach HubSpot")
    return gaps


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
    return c.get("caller_name") or c.get("invitee_name") or f.get("caller_name") or c.get("caller_number") or "Unknown caller"


def _project(c):
    f = c.get("fields") or {}
    kind = f.get("property_description") or {
        "residential": "Home", "office_clinic_studio": "Office", "restaurant_hotel_hospitality": "Restaurant / hotel",
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
    "escalated": ("critical", "!", "Escalated"),
    "cancelled": ("serious", "×", "Cancelled"),
    "no_show": ("serious", "×", "No-show"),
    "missed": ("critical", "!", "Missed call"),
    "nurture": ("neutral", "↻", "Nurture"),
    "nurture_followed_up": ("neutral", "✓", "Followed up"),
    "not_qualified": ("neutral", "–", "Not a fit"),
    "no_data": ("neutral", "–", "No conversation"),
}


def _badge(c):
    if c.get("status") != "booked" and c.get("handled_at"):
        cls, icon, label = "good", "✓", "Done"
    elif c.get("status") != "booked" and c.get("overturned_at"):
        cls, icon, label = "warning", "↺", "Overturned"
    else:
        cls, icon, label = BADGES.get(c.get("status") or "", ("neutral", "…", "Processing"))
    return f"<span class='badge {cls}'><span class='bi' aria-hidden='true'>{icon}</span>{e(label)}</span>"


def _outcome(c):
    d = c.get("decision") or ""
    p = c.get("priority") or ""
    return f"{e(d)}{' · ' + e(p) if p else ''}" if d else "—"


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
--brand:#9a4a2b;--brand-ink:#fff;--focus:#2a78d6;--shadow:0 1px 2px rgba(11,11,11,.04)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--page:#0d0d0d;
--surface-1:#1a1a19;--surface-2:#232321;--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;
--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;
--seq-track:#184f95;--good-text:#0ca30c;--critical-text:#e66767;--good-bg:#132a13;--warning-bg:#3a2f12;--serious-bg:#3a2016;
--critical-bg:#3a1616;--neutral-bg:#2a2a28;--brand:#e0936f;--brand-ink:#1a1a19;--focus:#3987e5;--shadow:none}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--surface-2:#232321;
--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--seq-track:#184f95;
--good-text:#0ca30c;--critical-text:#e66767;--good-bg:#132a13;--warning-bg:#3a2f12;--serious-bg:#3a2016;--critical-bg:#3a1616;
--neutral-bg:#2a2a28;--brand:#e0936f;--brand-ink:#1a1a19;--focus:#3987e5;--shadow:none}
*{box-sizing:border-box}html,body{margin:0;background:var(--page)}
.viz-root{background:var(--page);color:var(--text-primary);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:inherit}a:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.ic{flex:none;vertical-align:-3px}
.wrap{max-width:1200px;margin:0 auto;padding:0 16px}
.topbar{background:var(--surface-1);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:5}
.topbar .wrap{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:60px;flex-wrap:wrap;padding-top:10px;padding-bottom:10px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none}
.mark{width:30px;height:30px;border-radius:8px;background:var(--brand);color:var(--brand-ink);display:grid;place-items:center;font-weight:700;font-size:15px}
.brand b{display:block;font-size:15px;line-height:1.2}.brand small{display:block;color:var(--text-secondary);font-size:12px}
.seg{display:flex;gap:2px;background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:3px;flex-wrap:wrap}
.seg a{padding:5px 12px;border-radius:7px;color:var(--text-secondary);text-decoration:none;font-size:13px;white-space:nowrap}
.seg a:hover{color:var(--text-primary)}
.seg a.on{background:var(--surface-1);color:var(--text-primary);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.08)}
main{padding:24px 0 48px}
.pagehead{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:22px;line-height:1.25;margin:0;letter-spacing:-.01em}h2{font-size:15px;margin:0}
.sub{color:var(--text-secondary);margin:2px 0 0;font-size:13px}
.headline{font-size:15px;margin:6px 0 0;max-width:780px;color:var(--text-secondary)}
.headline b{color:var(--text-primary);font-weight:600}.headline .urgent{color:var(--critical-text);font-weight:600}
.btn{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--border);background:var(--surface-1);color:var(--text-primary);
border-radius:10px;padding:7px 12px;font:inherit;font-size:13px;font-weight:600;text-decoration:none;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--surface-2)}
.btn.primary{background:var(--text-primary);color:var(--surface-1);border-color:var(--text-primary)}
.btn.primary:hover{opacity:.9}
.btn[aria-disabled="true"]{opacity:.45;pointer-events:none}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:14px;padding:18px;box-shadow:var(--shadow);min-width:0}
.cardhead{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.cardhead .sub{margin:0}
.notice{border-radius:12px;padding:12px 14px;margin:0 0 16px;font-size:13px;border:1px solid var(--border)}
.notice.warn{background:var(--warning-bg)}.notice b{display:block;margin-bottom:2px}
.notice ul{margin:4px 0 0;padding-left:18px}
.notice details summary{cursor:pointer;font-weight:600}
.hero{display:grid;grid-template-columns:minmax(240px,1fr) 2fr;gap:16px;margin-bottom:16px}
.big{font-size:56px;font-weight:650;line-height:1;margin:10px 0 6px;letter-spacing:-.02em}
.label{color:var(--text-secondary);font-size:13px}
.delta{display:inline-flex;align-items:center;gap:3px;font-size:12px;font-weight:600;color:var(--text-secondary);white-space:nowrap}
.delta.good{color:var(--good-text)}.delta.bad{color:var(--critical-text)}
.funnel{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.step{background:var(--surface-2);border-radius:10px;padding:12px;min-width:0}
.step .v{font-size:26px;font-weight:650;line-height:1.1;margin-top:4px}.step .n{color:var(--muted);font-size:12px;margin-top:2px}
.step .rail{height:6px;border-radius:3px;background:var(--seq-track);margin-top:10px;overflow:hidden}
.step .rail span{display:block;height:100%;background:var(--series-1);border-radius:3px}
.groups{display:flex;gap:16px;flex-wrap:wrap;margin-top:14px;font-size:13px;color:var(--text-secondary)}
.groups b{color:var(--text-primary)}
.key{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.section-title{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:24px 0 10px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{padding:14px 16px}.tile .label{font-size:12.5px}.tile .value{font-size:24px;font-weight:650;margin-top:4px;line-height:1.2}
.tile .note{color:var(--muted);font-size:12px;margin-top:2px}
.grid2{display:grid;grid-template-columns:3fr 2fr;gap:16px;margin-top:16px}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--text-secondary);font-size:12.5px}
.chart{position:relative}.chart svg{display:block;width:100%;height:auto}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
padding:8px 10px;font-size:12.5px;box-shadow:0 4px 16px rgba(0,0,0,.12);white-space:nowrap;opacity:0;transition:opacity .1s;z-index:2}
.tip b{display:block;margin-bottom:2px}.tip .r{display:flex;align-items:center;gap:6px;color:var(--text-secondary)}
.tip .r span:last-child{margin-left:auto;padding-left:12px;color:var(--text-primary);font-variant-numeric:tabular-nums}
.hbars .row{display:grid;grid-template-columns:minmax(120px,170px) 1fr;gap:10px;align-items:center;margin:9px 0}
.hbars .name{color:var(--text-secondary);font-size:13px;line-height:1.3}
.hbars .track{display:flex;align-items:center;gap:8px;min-width:0}
.hbars .fill{height:14px;background:var(--series-1);border-radius:0 4px 4px 0;min-width:3px}
.hbars .val{font-size:12.5px;font-variant-numeric:tabular-nums}
.attn{list-style:none;margin:0;padding:0}
.attn li{border-top:1px solid var(--grid);display:flex;align-items:center;gap:6px}.attn li:first-child{border-top:0}
.attn a.row{flex:1;min-width:0;display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;padding:10px 4px;text-decoration:none;border-radius:8px}
.attn a.row:hover{background:var(--surface-2)}
.attn .who{font-weight:600}.attn .what{color:var(--text-secondary);font-size:12.5px}
.attn .when{color:var(--muted);font-size:12px;white-space:nowrap}
.attn .tel{width:34px;height:34px;display:grid;place-items:center;border-radius:9px;border:1px solid var(--border);color:var(--text-secondary);flex:none}
.attn .tel:hover{color:var(--text-primary);background:var(--surface-2)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}.dot.high{background:var(--critical)}.dot.medium{background:var(--warning)}
.empty{color:var(--text-secondary);font-size:13px;padding:8px 0}
.toolbar{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:12px}
.toolbar .right{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--border);background:var(--surface-1);color:var(--text-secondary);border-radius:999px;padding:5px 12px;
font:inherit;font-size:13px;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.chip:hover{color:var(--text-primary)}.chip[aria-pressed="true"]{background:var(--text-primary);color:var(--surface-1);border-color:var(--text-primary)}
.chip .c{font-variant-numeric:tabular-nums;opacity:.75}
.search{border:1px solid var(--border);background:var(--surface-1);color:var(--text-primary);border-radius:10px;padding:7px 12px;
font:inherit;font-size:13px;width:240px;max-width:100%}
.tablewrap{overflow-x:auto;margin:0 -18px;padding:0 18px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px 10px;border-bottom:1px solid var(--grid);vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:12px;white-space:nowrap}
tbody tr[data-href]{cursor:pointer}tbody tr[data-href]:hover td{background:var(--surface-2)}
tr.dayrow td{background:var(--page);color:var(--text-secondary);font-size:12px;font-weight:600;padding:7px 10px;border-bottom:1px solid var(--grid)}
tr.dayrow td span{color:var(--muted);font-weight:400}
td.num,th.num{font-variant-numeric:tabular-nums;text-align:right}
td .main{font-weight:600;text-decoration:none}td .main:hover{text-decoration:underline}
td .secondary{color:var(--muted);font-size:12px}
td.nowrap{white-space:nowrap}
.badge{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:2px 9px 2px 7px;font-size:12px;font-weight:600;white-space:nowrap;
background:var(--neutral-bg);color:var(--text-primary)}
.badge .bi{font-size:11px;width:14px;height:14px;border-radius:50%;display:inline-grid;place-items:center;color:#fff;background:var(--muted)}
.badge.good{background:var(--good-bg)}.badge.good .bi{background:var(--good)}
.badge.warning{background:var(--warning-bg)}.badge.warning .bi{background:var(--warning);color:#0b0b0b}
.badge.serious{background:var(--serious-bg)}.badge.serious .bi{background:var(--serious);color:#0b0b0b}
.badge.critical{background:var(--critical-bg)}.badge.critical .bi{background:var(--critical)}
.meter{display:inline-flex;align-items:center;gap:8px;justify-content:flex-end}
.meter .bar{width:48px;height:6px;border-radius:3px;background:var(--seq-track);overflow:hidden}
.meter .bar span{display:block;height:100%;background:var(--series-1);border-radius:3px}
details.tv summary{cursor:pointer;color:var(--text-secondary);font-size:12.5px;margin-top:10px}
details.tv table{margin-top:8px}
.back{display:inline-flex;align-items:center;gap:4px;color:var(--text-secondary);text-decoration:none;font-size:13px;margin-bottom:12px}
.back:hover{color:var(--text-primary)}
.callhead{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:16px}
.callhead .meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px;color:var(--text-secondary);font-size:13px}
.scorebox{text-align:right}.scorebox .big{font-size:40px;margin:0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.why{font-size:15px;margin:0;line-height:1.5}
.nextbox{display:grid;grid-template-columns:1fr auto;gap:16px;align-items:start}
.handle{background:var(--surface-2);border-radius:12px;padding:14px;margin-top:14px}
.handle form{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.handle input{border:1px solid var(--border);background:var(--surface-1);color:var(--text-primary);border-radius:10px;padding:7px 10px;font:inherit;font-size:13px}
.handle input[name=note]{flex:1 1 260px;min-width:0}.handle input[name=by]{width:150px}
.handle .done{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap}
dl.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px 20px;margin:0}
dl.facts dt{color:var(--muted);font-size:12px}dl.facts dd{margin:2px 0 0;overflow-wrap:anywhere}
.checks{list-style:none;margin:0;padding:0}
.checks li{display:grid;grid-template-columns:24px 1fr;gap:10px;padding:10px 0;border-top:1px solid var(--grid)}
.checks li:first-child{border-top:0}
.ci{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;color:#fff}
.ci.pass{background:var(--good)}.ci.fail{background:var(--critical)}.ci.unclear{background:var(--warning);color:#0b0b0b}
.checks .n{font-weight:600}.checks .d{color:var(--text-secondary);font-size:12.5px}
.factor{padding:10px 0;border-top:1px solid var(--grid)}.factor:first-child{border-top:0}
.factor .top{display:flex;justify-content:space-between;gap:8px;font-size:13px}
.factor .top b{font-weight:600}.factor .top span{font-variant-numeric:tabular-nums;color:var(--text-secondary)}
.factor .bar{height:6px;border-radius:3px;background:var(--seq-track);margin:6px 0;overflow:hidden}
.factor .bar span{display:block;height:100%;background:var(--series-1);border-radius:3px}
.factor q{display:block;color:var(--text-secondary);font-size:12.5px;font-style:italic}
.flags{margin:0;padding-left:18px}.flags li{margin:3px 0}
.timeline{list-style:none;margin:0;padding:0}
.timeline li{display:grid;grid-template-columns:110px 1fr;gap:10px;padding:7px 0;font-size:13px;border-top:1px solid var(--grid)}
.timeline li:first-child{border-top:0}.timeline .t{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.timeline .bad{color:var(--critical-text);font-weight:600}
.chat{display:flex;flex-direction:column;gap:8px;max-height:640px;overflow-y:auto;padding-right:4px}
.msg{max-width:78%;padding:9px 12px;border-radius:14px;font-size:13.5px;line-height:1.45}
.msg .who{display:block;font-size:11px;font-weight:600;color:var(--muted);margin-bottom:2px;text-transform:uppercase;letter-spacing:.04em}
.msg.agent{align-self:flex-start;background:var(--surface-2);border-bottom-left-radius:4px}
.msg.caller{align-self:flex-end;background:var(--seq-track);border-bottom-right-radius:4px}
.stack{display:grid;gap:16px;align-content:start}
.footer{color:var(--muted);font-size:12px;margin-top:28px;text-align:center}
@media (max-width:860px){.hero,.grid2{grid-template-columns:1fr}.funnel{grid-template-columns:repeat(2,1fr)}
.big{font-size:44px}.search{width:100%}.msg{max-width:92%}.timeline li{grid-template-columns:90px 1fr}
.nextbox{grid-template-columns:1fr}.scorebox{text-align:left}.toolbar .right{width:100%;flex-wrap:nowrap}
.toolbar .right .search{flex:1;width:auto}.topbar{position:static}.tile .value{font-size:21px}}
@media (max-width:520px){.hide-sm{display:none}}
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
  // "Handled by" remembers the last name typed on this device
  document.querySelectorAll('input[name=by]').forEach(function(i){
    try { i.value = i.value || localStorage.getItem('aangan_by') || ''; } catch (e) {}
    i.form && i.form.addEventListener('submit', function(){ try { localStorage.setItem('aangan_by', i.value); } catch (e) {} });
  });
  // Calls table: group chips + search, day headers follow their rows
  var table = document.getElementById('calls');
  var group = 'all', q = '';
  if (table) {
    var rows = Array.prototype.slice.call(table.querySelectorAll('tbody tr[data-group]'));
    var days = Array.prototype.slice.call(table.querySelectorAll('tbody tr.dayrow'));
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
    rows.forEach(function(r){
      r.addEventListener('click', function(ev){ if (ev.target.closest('a,button')) return; window.location = r.getAttribute('data-href'); });
    });
  }
  // Overview refreshes itself every 2 minutes while it's on screen and nobody is filtering
  if (document.body.getAttribute('data-refresh')) {
    setInterval(function(){
      var busy = q || group !== 'all' || document.querySelector('details[open]') || document.activeElement.tagName === 'INPUT';
      if (document.visibilityState === 'visible' && !busy) window.location.reload();
    }, 120000);
  }
})();
"""

FAVICON_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='#9a4a2b'/>"
               "<text x='16' y='22' font-family='system-ui,sans-serif' font-size='18' font-weight='700' fill='#fff' "
               "text-anchor='middle'>A</text></svg>")
FAVICON = "data:image/svg+xml," + urllib.parse.quote(FAVICON_SVG)


def _page(title: str, body: str, token: str, period: str | None = None, refresh: bool = False) -> str:
    seg = ""
    if period is not None:
        seg = "<nav class='seg' aria-label='Period'>" + "".join(
            f"<a class='{'on' if period == p else ''}' href='{e(_q('/dashboard', token, period=p))}'>{label}</a>"
            for p, label in (("1", "Today"), ("7", "7 days"), ("30", "30 days"), ("mtd", "This month"))) + "</nav>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<link rel="icon" href="{FAVICON}"><title>{e(title)}</title><style>{CSS}</style></head>
<body class="viz-root"{' data-refresh="1"' if refresh else ''}>
<header class="topbar"><div class="wrap"><a class="brand" href="{e(_q('/dashboard', token))}"><span class="mark">A</span>
<span><b>Aangan Studio</b><small>Phone enquiry agent</small></span></a>{seg}</div></header>
<main><div class="wrap">{body}
<p class="footer">Every call is answered by the agent, qualified on Nikhil's five checks, and logged here. Prices are never quoted.</p>
</div></main><script>{JS}</script></body></html>"""


def _delta(cur, prev, good_when_up=True, unit="", vs="previous period"):
    """'▲ 4 vs previous 30 days'. Colour = direction × whether up is good; always with an arrow and a word."""
    if cur is None or prev is None:
        return ""
    diff = cur - prev
    if abs(diff) < 0.5:
        return f"<span class='delta' title='Same as the {e(vs)}'>= same as {e(vs)}</span>"
    up = diff > 0
    cls = "good" if up == good_when_up else "bad"
    return (f"<span class='delta {cls}'>{'▲' if up else '▼'} {abs(diff):.0f}{unit} "
            f"<span style='font-weight:400;color:var(--muted)'>vs {e(vs)}</span></span>")


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


# --- dashboard -------------------------------------------------------------------------------------

def render_dashboard(m: dict, period: str, token: str, gaps: list[str] | None = None) -> str:
    period_label = {"1": "today", "7": "in the last 7 days", "30": "in the last 30 days", "mtd": "this month"}.get(period, "")
    vs = {"1": "yesterday", "7": "previous 7 days", "30": "previous 30 days", "mtd": "same days last period"}.get(period, "previous period")
    prev = m.get("prev")
    notices = ""
    if gaps:
        notices += ("<div class='notice warn' role='status'><b>Setup still needed before real calls work end to end</b><ul>"
                    + "".join(f"<li>{e(g)}</li>" for g in gaps) + "</ul></div>")
    if m["cost_missing"] and m["calls"]:
        notices += (f"<div class='notice warn' role='status'><details><summary>Cost figures are incomplete</summary>"
                    f"Add these rates to the environment so cost per call can be worked out: "
                    f"{e(', '.join(m['cost_missing']))}</details></div>")

    if not m["ever"]:
        body = (f"<div class='pagehead'><div><h1>Overview</h1><p class='sub'>No calls yet</p></div></div>{notices}"
                "<div class='card' style='text-align:center;padding:48px 18px'><h2>No calls have come in yet</h2>"
                "<p class='sub' style='max-width:460px;margin:8px auto 0'>Once the studio number forwards to the agent, "
                "every call shows up here within a minute of hanging up: who called, what they want, whether it was "
                "booked, and why not if it wasn't.</p></div>")
        return _page("Aangan Phone Agent", body, token, period, refresh=True)

    g = m["groups"]
    urgent = sum(1 for _, _, u in m["attention"] if u == "high")
    after = f" ({m['after_hours']} after hours)" if m["after_hours"] else ""
    headline = (f"{period_label[:1].upper() + period_label[1:]} the agent answered <b>{_plural(m['answered'], 'call')}</b>"
                f"{after} and booked <b>{_plural(m['booked_on_call'], 'consultation')}</b> on the call.")
    if urgent:
        headline += f" <span class='urgent'>{_plural(urgent, 'caller')} need{'s' if urgent == 1 else ''} a call back now.</span>"
    elif m["attention"]:
        headline += f" {_plural(len(m['attention']), 'follow-up')} open."
    else:
        headline += " Nothing is waiting on the studio."

    base = max(1, m["answered"])
    funnel = "".join(
        f"<div class='step'><div class='label'>{label}</div><div class='v'>{v}</div><div class='n'>{note}</div>"
        f"<div class='rail' title='{v} of {m['answered']} answered calls'><span style='width:{100 * v / base:.0f}%'></span></div></div>"
        for label, v, note in (
            ("Calls answered", m["answered"], f"of {m['calls']} · {m['after_hours']} after hours"),
            ("Real conversations", m["talked"], "caller said what they need"),
            ("Qualified", m["qualified"], f"{_fmt_pct(m['qualified_pct'])} of conversations"),
            ("Booked", m["booked_any"], f"{g['booked']} still on · {m['cancelled'] + m['no_show']} cancelled or no-show"),
        ))
    legend = "".join(f"<span title='{e(GROUP_HINT[k])}'><span class='key' style='background:{GROUP_COLOR[k]}'></span>{label} "
                     f"<b>{g[k]}</b></span>" for k, label in GROUPS)
    hero = (f"<div class='hero'><div class='card'><div class='label'>Consultations booked on the call</div>"
            f"<div class='big'>{m['booked_on_call']}</div>"
            f"<div>{_delta(m['booked_on_call'], prev and prev['booked_on_call'], vs=vs)}</div>"
            f"<div class='label' style='margin-top:8px'>from {_plural(m['calls'], 'call')} {period_label}"
            f" · running cost {_fmt_inr(m['total_cost_inr'])}</div></div>"
            f"<div class='card'><div class='cardhead'><h2>Where the calls went</h2></div>"
            f"<div class='funnel'>{funnel}</div><div class='groups'>{legend}</div></div></div>")

    if m["attention"]:
        items = "".join(
            f"<li><a class='row' href='{e(_q('/dashboard/call', token, call_id=c['call_id']))}'><span class='dot {u}' aria-hidden='true'></span>"
            f"<span><span class='who'>{e(_caller(c))}</span> <span class='what'>· {e(_project(c))}</span><br>"
            f"<span class='what'>{'Urgent: ' if u == 'high' else ''}{e(text)}</span></span><span class='when'>{e(_when(c))}</span></a>"
            + (f"<a class='tel' href='tel:{e(c['caller_number'].replace(' ', ''))}' title='Call {e(c['caller_number'])}' "
               f"aria-label='Call {e(_caller(c))}'>{_icon('phone')}</a>" if c.get("caller_number") else "")
            + "</li>"
            for c, text, u in m["attention"][:8])
        more = (f"<p class='sub' style='margin-top:8px'>+{len(m['attention']) - 8} more: filter the table below by "
                f"Needs follow-up</p>") if len(m["attention"]) > 8 else ""
        attention = f"<ul class='attn'>{items}</ul>{more}"
    else:
        attention = "<p class='empty'>Nothing waiting. Every recent caller is booked or closed.</p>"

    tiles_calls = "".join([
        _tile("Time to answer (median)", _fmt_secs(m["median_answer_sec"]), "target under 5 min, day or night"),
        _tile("After-hours calls handled", m["after_hours"], "outside 10am–7pm",
              _delta(m["after_hours"], prev and prev["after_hours"], vs=vs)),
        _tile("Qualified rate", _fmt_pct(m["qualified_pct"]), f"{m['qualified']} qualified",
              _delta(m["qualified_pct"], prev and prev["qualified_pct"], unit=" pts", vs=vs)),
        _tile("Call → booking (median)", "—" if m["median_call_to_booking_min"] is None
              else f"{m['median_call_to_booking_min']:.0f} min", f"{m['booked_any']} bookings in total"),
    ])
    tiles_after = "".join([
        _tile("Cancellations / no-shows", f"{m['cancelled']} / {m['no_show']}", "front desk calls them back"),
        _tile("Won / lost (HubSpot)", f"{m['won']} / {m['lost']}", "designer marks the deal"),
        _tile("Designer had to re-ask", _fmt_pct(m["reask_pct"]), f"of {m['report_cards']} report cards"),
        _tile("Escalations", m["escalations"], "instant alert to studio head"),
        _tile("Price lines blocked", m["guard_blocks"], "speech guard caught and replaced"),
    ])
    tiles_cost = "".join([
        _tile("Total running cost", _fmt_inr(m["total_cost_inr"]), "rates not set" if m["cost_missing"] else period_label),
        _tile("Cost per call", _fmt_inr(m["cost_per_call_inr"]), "Vaani + Claude + Gemini + email"),
        _tile("Cost per booked consultation", _fmt_inr(m["cost_per_booking_inr"])),
    ])

    trend_table = "".join(f"<tr><td>{d:%a %d %b}</td><td class='num'>{b}</td><td class='num'>{f}</td><td class='num'>{c}</td></tr>"
                          for d, b, f, c in m["per_day_groups"])
    chart = (f"<div class='card'><div class='cardhead'><h2>Calls per day</h2><div class='legend'>"
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
               f"<p class='sub' style='margin-top:12px'>Designers get these in the 7pm digest and can overturn any of them "
               f"from the call's page.</p></div>")

    chips = "".join(f"<button type='button' class='chip' data-g='{k}' aria-pressed='{'true' if k == 'all' else 'false'}'"
                    f"{' title=' + chr(39) + e(GROUP_HINT[k]) + chr(39) if k in GROUP_HINT else ''}>"
                    f"{label} <span class='c'>{n}</span></button>"
                    for k, label, n in [("all", "All", m["calls"])] + [(k, label, g[k]) for k, label in GROUPS])
    today = m["now"].date()
    by_day: dict = {}
    for c in m["rows"][:300]:
        t = _ts(c.get("started_at") or c.get("created_at"))
        by_day.setdefault(t.astimezone(config.IST).date() if t else today, []).append(c)
    rows = []
    for d, cs in by_day.items():
        rows.append(f"<tr class='dayrow' data-day='{d}'><td colspan='7'>{e(_day_label(d, today))} "
                    f"<span>· {_plural(len(cs), 'call')} · {sum(1 for c in cs if group_of(c) == 'booked')} booked</span></td></tr>")
        for c in cs:
            href = e(_q('/dashboard/call', token, call_id=c['call_id']))
            t = _ts(c.get("started_at") or c.get("created_at"))
            rows.append(
                f"<tr data-group='{group_of(c)}' data-day='{d}' data-href='{href}'>"
                f"<td class='nowrap'>{t.astimezone(config.IST).strftime('%I:%M %p').lstrip('0') if t else '—'}"
                f"{'<div class=secondary>after hours</div>' if c.get('after_hours') else ''}</td>"
                f"<td><a class='main' href='{href}'>{e(_caller(c))}</a>"
                f"<div class='secondary'>{e(c.get('caller_number') or '') if _caller(c) != c.get('caller_number') else ''}</div></td>"
                f"<td>{e(_project(c))}</td><td>{_badge(c)}<div class='secondary'>{_outcome(c)}</div></td>"
                f"<td>{e(next_step(c, today)[0])}</td>"
                f"<td class='num'>{_meter(c.get('score'))}</td>"
                f"<td class='num'>{_fmt_inr(to_inr(call_cost(c)))}</td></tr>")
    export = e(_q("/dashboard/export.csv", token, period=period))
    table = (f"<div class='card' style='margin-top:16px'><div class='cardhead'><h2>All calls</h2>"
             f"<span class='sub'>{_plural(m['calls'], 'call')} {period_label} · click a row for the full call</span></div>"
             f"<div class='toolbar'><div class='chips' role='group' aria-label='Filter calls'>{chips}</div>"
             f"<div class='right'><input id='search' class='search' type='search' placeholder='Search name, area, number…' "
             f"aria-label='Search calls'><a class='btn' href='{export}'>{_icon('download')}<span class='hide-sm'>Export</span> CSV</a></div></div>"
             f"<div class='tablewrap'><table id='calls'><thead><tr><th>Time</th><th>Caller</th><th>Project</th><th>Outcome</th>"
             f"<th>Next step</th><th class='num'>Interest</th><th class='num'>Cost</th></tr></thead><tbody>{''.join(rows)}"
             f"<tr id='nomatch' hidden><td colspan='7' class='empty'>No calls match.</td></tr></tbody></table></div></div>")

    body = (f"<div class='pagehead'><div><h1>Overview</h1><p class='headline'>{headline}</p>"
            f"<p class='sub'>{m['start']:%d %b} – {m['now']:%d %b %Y} · updated {m['now'].strftime('%I:%M %p').lstrip('0')} IST"
            f" · refreshes every 2 minutes</p></div></div>{notices}{hero}"
            f"<div class='grid2' style='margin-top:0'><div class='card'><div class='cardhead'><h2>Needs attention</h2>"
            f"<span class='sub'>open items from the last 7 days</span></div>{attention}</div>{reasons}</div>"
            f"<div class='grid2'>{chart}<div class='tiles' style='align-content:start'>{tiles_calls}</div></div>"
            f"<p class='section-title'>After the booking</p><div class='tiles'>{tiles_after}</div>"
            f"<p class='section-title'>Cost</p><div class='tiles'>{tiles_cost}</div>{table}")
    return _page("Aangan Phone Agent", body, token, period, refresh=True)


def _meter(score):
    if score is None:
        return "—"
    s = max(0, min(100, int(score)))
    return (f"<span class='meter' title='Interest score {s}/100'><span class='bar'><span style='width:{s}%'></span></span>"
            f"<span>{s}</span></span>")


# --- CSV export ------------------------------------------------------------------------------------

def render_csv(rows: list[dict], now: datetime) -> str:
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Time (IST)", "Caller", "Phone", "Email", "Project", "Area", "Decision", "Priority",
                "Reason", "Status", "Next step", "Interest score", "Consultation", "Designer", "HubSpot deal",
                "After hours", "Duration (sec)", "Call id"])
    for c in rows:
        f = c.get("fields") or {}
        t = _ts(c.get("started_at") or c.get("created_at"))
        t = t.astimezone(config.IST) if t else None
        w.writerow([t.strftime("%Y-%m-%d") if t else "", t.strftime("%H:%M") if t else "", _caller(c),
                    c.get("caller_number") or "", c.get("invitee_email") or "", _project(c), f.get("location_text") or "",
                    c.get("decision") or "", c.get("priority") or "", REASON_SHORT.get(c.get("reason_code") or "", ""),
                    c.get("status") or "", next_step(c, now.date())[0], c.get("score") if c.get("score") is not None else "",
                    _slot(c.get("slot_start")) if c.get("slot_start") else "", c.get("designer_name") or c.get("designer_email") or "",
                    c.get("hubspot_deal_id") or "", "yes" if c.get("after_hours") else "no", c.get("duration_sec") or "",
                    c["call_id"]])
    return "﻿" + out.getvalue()      # BOM so Excel opens the ₹ and names correctly


# --- one call --------------------------------------------------------------------------------------

EVENT_LABELS = {
    "email_sent": "Email sent", "email_failed": "Email FAILED", "calcom_webhook": "Calendar update",
    "calendly_webhook": "Calendar update", "recording": "Call recording", "speech_guard_block": "Price line blocked",
    "price_spoken": "Agent spoke a price", "hubspot_failed": "HubSpot update FAILED", "error": "Error",
    "vaani_webhook": "Phone system", "dashboard_done": "Marked as done", "dashboard_reopen": "Reopened",
    "dashboard_overturn": "Rejection overturned",
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
        detail = ", ".join(p.get("reasons") or [])
    elif kind.startswith("dashboard_"):
        detail = " · ".join(x for x in ((f"by {p['by']}" if p.get("by") else ""), p.get("note") or "") if x)
    else:
        detail = p.get("error") or ""
    return f"<span><span class='{'bad' if bad else ''}'>{e(label)}</span>{': ' + e(detail) if detail else ''}</span>"


def _transcript_html(t: str) -> str:
    if not t:
        return "<p class='empty'>No transcript (the caller hung up before speaking, or the call is still being processed).</p>"
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
    def form(action, label, primary=False, fields=True):
        inputs = ("<input name='note' placeholder='What happened? e.g. Called back, booked for Tuesday' aria-label='Note'>"
                  "<input name='by' placeholder='Your name' aria-label='Your name'>") if fields else ""
        return (f"<form method='post' action='/dashboard/call/action'>"
                f"<input type='hidden' name='call_id' value='{e(c['call_id'])}'><input type='hidden' name='token' value='{e(token)}'>"
                f"<input type='hidden' name='action' value='{action}'>{inputs}"
                f"<button class='btn{' primary' if primary else ''}' type='submit'>"
                f"{_icon('undo' if action == 'reopen' else 'check')}{e(label)}</button></form>")

    if c.get("status") == "booked":
        return ""
    if c.get("handled_at"):
        who = f" by {e(c['handled_by'])}" if c.get("handled_by") else ""
        note = f": {e(c['handled_note'])}" if c.get("handled_note") else ""
        return (f"<div class='handle'><div class='done'><span><b>Done</b>{who} · "
                f"{e(_when({'started_at': c['handled_at']}))}{note}</span>{form('reopen', 'Reopen', fields=False)}</div></div>")
    group = group_of(c)
    if group == "follow_up":
        return f"<div class='handle'><div class='label' style='margin-bottom:8px'>Once someone has dealt with this, mark it done so it leaves the to-do list.</div>{form('done', 'Mark as done', primary=True)}</div>"
    if c.get("decision") in ("Not qualified", "Nurture"):
        return (f"<div class='handle'><div class='label' style='margin-bottom:8px'>Disagree with the agent? Overturn it and "
                f"the caller goes back on the to-do list for a call back.</div>{form('overturn', 'Overturn: call them back')}</div>")
    return ""


def render_call(c: dict, events: list[dict] | None = None, token: str = "") -> str:
    f = c.get("fields") or {}
    icon = {"pass": "✓", "fail": "×", "unclear": "?"}
    word = {"pass": "Passed", "fail": "Failed", "unclear": "Unclear"}
    checks = "".join(
        f"<li><span class='ci {g['status']}' aria-hidden='true'>{icon.get(g['status'], '?')}</span><div>"
        f"<div class='n'>{e(g['name'])} <span class='sub' style='font-weight:400'>· {word.get(g['status'], g['status'])}</span></div>"
        f"<div class='d'>{e(REASON_SHORT.get(g.get('reason_code') or '', ''))}{' — ' if g.get('reason_code') and g.get('note') else ''}"
        f"{e(g.get('note') or '')}</div></div></li>"
        for g in c.get("gates") or []) or "<li class='empty'>Not processed yet.</li>"
    factors = "".join(
        f"<div class='factor'><div class='top'><b>{e(l['factor'])}</b><span>{l['points']} / {l['max_points']}</span></div>"
        f"<div class='bar'><span style='width:{100 * l['points'] / max(1, l['max_points']):.0f}%'></span></div>"
        f"{'<q>' + e(l['quote']) + '</q>' if l.get('quote') else ''}"
        f"{'<div style=font-size:12px;color:var(--muted)>' + e(l['note']) + '</div>' if l.get('note') else ''}</div>"
        for l in c.get("score_lines") or []) or "<p class='empty'>No score yet.</p>"
    flags = "".join(f"<li>{e(x)}</li>" for x in c.get("flags") or [])

    rooms = ", ".join(f.get("rooms") or [])
    size = f"{f['size_sqft']:,.0f} sq ft" if f.get("size_sqft") else ""
    facts = [
        ("Called", f"{_when(c)}{' · after hours' if c.get('after_hours') else ''}"),
        ("Duration", _fmt_dur(c.get("duration_sec"))),
        ("Phone", c.get("caller_number") or "—"),
        ("Email", c.get("invitee_email") or "—"),
        ("Project", _project(c)),
        ("Size / rooms", " · ".join(x for x in (size, rooms) if x) or "—"),
        ("Timeline", f.get("timeline_text") or "not discussed"),
        ("Decision-maker", {"owner_or_authorised": "Owner / authorised", "deciders_will_attend": "Deciders will attend",
                            "researching_for_someone": "Researching for someone else"}.get(f.get("decision_maker") or "", "Not confirmed")),
        ("Source", (f.get("source") or "unknown").replace("_", " ").capitalize() + (f" ({f['referrer_name']})" if f.get("referrer_name") else "")),
    ]
    booking = [
        ("Consultation", _slot(c.get("slot_start")) if c.get("slot_start") else "Not booked"),
        ("Visit", {"site_visit": "Site visit", "studio": "At the studio"}.get(c.get("visit_type") or "", "—")),
        ("Designer", c.get("designer_name") or c.get("designer_email") or "—"),
        ("Report card", "Sent " + _when({"started_at": c["report_card_sent_at"]}) if c.get("report_card_sent_at") else "Not sent"),
        ("In the 7pm digest", _when({"started_at": c["digest_sent_at"]}) if c.get("digest_sent_at") else "—"),
        ("HubSpot deal", _hubspot(c.get("hubspot_deal_id"))),
    ]
    if c.get("status") == "nurture" and c.get("follow_up_on"):
        booking.append(("Follow up on", _date(c["follow_up_on"])))
    dl = lambda pairs: "".join(f"<div><dt>{e(k)}</dt><dd>{v if k == 'HubSpot deal' else e(str(v))}</dd></div>" for k, v in pairs)  # noqa: E731

    if c.get("status") == "booked":
        why = f"Qualified and booked: {_slot(c.get('slot_start'))}." if c.get("slot_start") else "Qualified and booked."
    elif c.get("decision") == "Qualified":
        why = "Qualified on all five checks, but not booked on the call. " + (
            "The follow-up is done." if c.get("handled_at") else next_step(c)[0] + ".")
    else:
        why = plain_reason(c) or next_step(c)[0]
    if c.get("priority_reasons"):
        why += " Priority: " + "; ".join(c["priority_reasons"]) + "."
    step_text, urgency = next_step(c, config.now_ist().date())

    acts = []
    if c.get("caller_number"):
        acts.append(f"<a class='btn primary' href='tel:{e(c['caller_number'].replace(' ', ''))}'>{_icon('phone')}Call {e(c['caller_number'])}</a>")
    if c.get("invitee_email"):
        subject = urllib.parse.quote("Your consultation with Aangan Studio")
        acts.append(f"<a class='btn' href='mailto:{e(c['invitee_email'])}?subject={subject}'>{_icon('mail')}Email caller</a>")
    if c.get("recording_url"):
        acts.append(f"<a class='btn' href='{e(c['recording_url'])}' target='_blank' rel='noopener'>{_icon('play')}Recording</a>")
    if c.get("hubspot_deal_id") and config.HUBSPOT_PORTAL_ID:
        acts.append(f"<a class='btn' href='https://app.hubspot.com/contacts/{e(config.HUBSPOT_PORTAL_ID)}/record/0-3/"
                    f"{e(str(c['hubspot_deal_id']))}' target='_blank' rel='noopener'>{_icon('ext')}HubSpot deal</a>")
    if c.get("reschedule_url") and c.get("status") == "booked":
        acts.append(f"<a class='btn' href='{e(c['reschedule_url'])}' target='_blank' rel='noopener'>{_icon('ext')}Reschedule</a>")

    timeline = "".join(
        f"<li><span class='t'>{e(_when({'started_at': ev.get('created_at')}))}</span>{_event_line(ev)}</li>"
        for ev in (events or []) if ev.get("kind") != "vaani_webhook") or "<li class='empty'>Nothing yet.</li>"

    score = c.get("score")
    summary = (f"<p class='sub' style='margin-top:10px'><b>Vaani summary:</b> {e(c['summary'])}</p>" if c.get("summary") else "")
    body = (f"<a class='back' href='{e(_q('/dashboard', token))}'>{_icon('back')}All calls</a>"
            f"<div class='callhead'><div><h1>{e(_caller(c))}</h1><div class='meta'>{_badge(c)}<span>{_outcome(c)}</span>"
            f"<span>· {e(_project(c))}</span><span>· {e(_when(c))}</span></div></div>"
            f"<div class='scorebox'><div class='label'>Interest score</div><div class='big'>{score if score is not None else '—'}"
            f"<span style='font-size:18px;color:var(--muted)'>/100</span></div></div></div>"
            f"<div class='card' style='margin-bottom:16px'><div class='cardhead'><h2>Outcome</h2>"
            + (f"<span class='badge {'critical' if urgency == 'high' else 'warning'}'><span class='bi' aria-hidden='true'>!</span>"
               f"Next: {e(step_text)}</span>" if urgency in ("high", "medium") else "")
            + f"</div><p class='why'>{e(why)}</p>{summary}"
            + (f"<ul class='flags' style='margin-top:10px'>{flags}</ul>" if flags else "")
            + (f"<div class='actions'>{''.join(acts)}</div>" if acts else "") + _handle_box(c, token) + "</div>"
            f"<div class='grid2' style='margin-top:0'><div class='stack'>"
            f"<div class='card'><div class='cardhead'><h2>Caller and project</h2></div><dl class='facts'>{dl(facts)}</dl></div>"
            f"<div class='card'><div class='cardhead'><h2>Booking and handoff</h2></div><dl class='facts'>{dl(booking)}</dl></div>"
            f"<div class='card'><div class='cardhead'><h2>Transcript</h2></div>{_transcript_html(c.get('transcript') or '')}</div></div>"
            f"<div class='stack'><div class='card'><div class='cardhead'><h2>Five checks</h2></div><ul class='checks'>{checks}</ul></div>"
            f"<div class='card'><div class='cardhead'><h2>Interest score</h2><span class='sub'>every point backed by a quote</span></div>{factors}</div>"
            f"<div class='card'><div class='cardhead'><h2>Activity</h2></div><ul class='timeline'>{timeline}</ul></div></div></div>")
    return _page(f"{_caller(c)} · Aangan Call", body, token)


def _hubspot(deal_id):
    if not deal_id:
        return "—"
    if config.HUBSPOT_PORTAL_ID:
        url = f"https://app.hubspot.com/contacts/{config.HUBSPOT_PORTAL_ID}/record/0-3/{deal_id}"
        return f"<a href='{e(url)}' target='_blank' rel='noopener'>Open deal {e(str(deal_id))}</a>"
    return e(f"Deal {deal_id}")
