"""Nikhil's dashboard (build step 9): what the agent handled, what it booked, and what it cost.

compute_metrics() is pure (testable); render_*() return HTML strings (server-rendered, inline SVG, light/dark).
Costs are computed here from raw usage stored per call, so changing a rate never needs a data migration.
"""
import html
import statistics
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config
from .calendly import parse_time

REASON_ORDER = ["OUT_OF_AREA", "OUT_OF_SCOPE_TYPE", "ADVICE_ONLY", "TOO_SMALL", "TIMELINE_IMPOSSIBLE",
                "BUDGET_MISALIGNED", "NOT_DECISION_MAKER", "EXISTING_CLIENT_COMPLAINT", "NO_INFO"]


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


def compute_metrics(calls: list[dict], events: list[dict], now: datetime, days: int | None) -> dict:
    if days:
        start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # month to date
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    in_range = [c for c in calls if (_ts(c.get("started_at") or c.get("created_at")) or now) >= start]

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

    day_keys = []
    d = start.date()
    while d <= now.date():
        day_keys.append(d)
        d += timedelta(days=1)
    per_day = {k: {"calls": 0, "bookings": 0} for k in day_keys}
    for c in in_range:
        t = _ts(c.get("started_at") or c.get("created_at"))
        if t and t.astimezone(config.IST).date() in per_day:
            per_day[t.astimezone(config.IST).date()]["calls"] += 1
            if c.get("booked_on_call"):
                per_day[t.astimezone(config.IST).date()]["bookings"] += 1

    blocks = [e for e in events if (_ts(e.get("created_at")) or now) >= start and e.get("kind") == "speech_guard_block"]
    pct = lambda a, b: (100 * len(a) / len(b)) if b else None  # noqa: E731
    return {
        "start": start, "now": now, "calls": len(in_range), "answered": len(answered),
        "answered_pct": pct(answered, in_range),
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
        "per_day": [(k, v["calls"], v["bookings"]) for k, v in per_day.items()],
        "rows": sorted(in_range, key=lambda c: c.get("started_at") or c.get("created_at") or "", reverse=True),
    }


# --- HTML ------------------------------------------------------------------------------------------

CSS = """
.viz-root{color-scheme:light;--page:#f9f9f7;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--series-1:#2a78d6;--series-2:#eb6834;
--good:#006300;--critical:#d03b3b;--warning-bg:#fff5db}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--page:#0d0d0d;
--surface-1:#1a1a19;--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--good:#0ca30c;--critical:#d03b3b;--warning-bg:#3a2f12}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#fff;
--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--series-1:#3987e5;--series-2:#d95926;--good:#0ca30c;--critical:#d03b3b;--warning-bg:#3a2f12}
*{box-sizing:border-box}body{margin:0}
.viz-root{background:var(--page);color:var(--text-primary);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
min-height:100vh;padding:24px 16px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 12px}
.sub{color:var(--text-secondary);margin:0 0 16px}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 20px}
.filters a{padding:5px 12px;border-radius:999px;border:1px solid var(--border);color:var(--text-secondary);text-decoration:none}
.filters a.on{background:var(--surface-1);color:var(--text-primary);font-weight:600}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:16px}
.hero{display:grid;grid-template-columns:minmax(220px,1fr) 3fr;gap:16px;margin-bottom:16px}
.hero .big{font-size:56px;font-weight:600;line-height:1.05}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
.tile .label{color:var(--text-secondary);font-size:12.5px}.tile .value{font-size:24px;font-weight:600;margin-top:2px}
.tile .note{color:var(--muted);font-size:12px}
.grid2{display:grid;grid-template-columns:3fr 2fr;gap:16px;margin-bottom:16px}
.legend{display:flex;gap:14px;color:var(--text-secondary);font-size:12.5px;margin-bottom:8px}
.key{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.bars .row{display:grid;grid-template-columns:190px 1fr 36px;align-items:center;gap:8px;margin:6px 0}
.bars .name{color:var(--text-secondary);font-size:12.5px;overflow-wrap:anywhere}
.bars .track{height:14px}.bars .fill{height:14px;background:var(--series-1);border-radius:0 4px 4px 0}
.bars .val{font-variant-numeric:tabular-nums;color:var(--text-primary);font-size:12.5px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--text-secondary);font-weight:600}td.num{font-variant-numeric:tabular-nums}
.tablewrap{overflow-x:auto}
.warn{background:var(--warning-bg);border:1px solid var(--border);border-radius:8px;padding:8px 12px;margin:0 0 16px;font-size:13px}
a{color:var(--series-1)}
details summary{cursor:pointer;color:var(--text-secondary);font-size:12.5px;margin-top:8px}
pre{white-space:pre-wrap;font:13px/1.5 ui-monospace,Consolas,monospace}
@media (max-width:760px){.hero,.grid2{grid-template-columns:1fr}.bars .row{grid-template-columns:120px 1fr 32px}}
"""

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


def _tile(label, value, note=""):
    return (f"<div class='card tile'><div class='label'>{e(label)}</div><div class='value'>{e(str(value))}</div>"
            f"<div class='note'>{e(note)}</div></div>")


def _trend_svg(per_day) -> str:
    """Two series, one axis (both are counts). Lines 2px, end markers r=4 with surface ring, native tooltips."""
    W, H, L, R, T, B = 640, 200, 32, 12, 10, 26
    n = len(per_day)
    if not n:
        return ""
    top = max([1] + [max(c, b) for _, c, b in per_day])
    step = max(1, -(-top // 4))
    ymax = step * 4
    x = lambda i: L + (W - L - R) * (i / max(1, n - 1))  # noqa: E731
    y = lambda v: T + (H - T - B) * (1 - v / ymax)       # noqa: E731
    parts = [f"<svg viewBox='0 0 {W} {H}' width='100%' role='img' aria-label='Calls and bookings per day'>"]
    for k in range(5):
        v = step * k
        parts.append(f"<line x1='{L}' x2='{W - R}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='var(--grid)' stroke-width='1'/>"
                     f"<text x='{L - 6}' y='{y(v) + 4:.1f}' text-anchor='end' font-size='11' fill='var(--muted)'>{v}</text>")
    for i in sorted({0, n // 2, n - 1}):
        anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
        parts.append(f"<text x='{x(i):.1f}' y='{H - 6}' text-anchor='{anchor}' font-size='11' fill='var(--muted)'>"
                     f"{per_day[i][0].strftime('%d %b')}</text>")
    for idx, color in ((1, "var(--series-1)"), (2, "var(--series-2)")):
        pts = " ".join(f"{x(i):.1f},{y(row[idx]):.1f}" for i, row in enumerate(per_day))
        parts.append(f"<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='2' "
                     f"stroke-linejoin='round' stroke-linecap='round'/>")
        lx, ly = x(n - 1), y(per_day[-1][idx])
        parts.append(f"<circle cx='{lx:.1f}' cy='{ly:.1f}' r='4' fill='{color}' stroke='var(--surface-1)' stroke-width='2'/>")
    for i, (d, c, b) in enumerate(per_day):   # hover targets wider than the marks
        w = (W - L - R) / max(1, n - 1)
        parts.append(f"<rect x='{x(i) - w / 2:.1f}' y='{T}' width='{w:.1f}' height='{H - T - B}' fill='transparent'>"
                     f"<title>{d.strftime('%a %d %b')}: {c} calls, {b} booked on the call</title></rect>")
    parts.append("</svg>")
    return "".join(parts)


def render_dashboard(m: dict, period: str, token: str) -> str:
    q = lambda p: "?" + urllib.parse.urlencode({"period": p, **({"token": token} if token else {})})  # noqa: E731
    filters = "".join(f"<a class='{'on' if period == p else ''}' href='{q(p)}'>{label}</a>"
                      for p, label in (("1", "Today"), ("7", "Last 7 days"), ("30", "Last 30 days"), ("mtd", "Month to date")))
    warn = ""
    if m["cost_missing"]:
        warn = (f"<div class='warn'>⚠ Cost is incomplete. Set these in the environment: "
                f"{e(', '.join(m['cost_missing']))}</div>")
    tiles = "".join([
        _tile("Calls answered", f"{m['answered']} of {m['calls']}", _fmt_pct(m["answered_pct"])),
        _tile("Time to answer (median)", _fmt_secs(m["median_answer_sec"]), "target under 5 min, 24×7"),
        _tile("After-hours calls handled", m["after_hours"], "outside 10am–7pm"),
        _tile("Qualified rate", _fmt_pct(m["qualified_pct"]), f"{m['qualified']} qualified"),
        _tile("Call → booking (median)", "—" if m["median_call_to_booking_min"] is None
              else f"{m['median_call_to_booking_min']:.0f} min", f"{m['booked_any']} bookings in total"),
        _tile("Cancellations / no-shows", f"{m['cancelled']} / {m['no_show']}"),
        _tile("Designer had to re-ask", _fmt_pct(m["reask_pct"]), f"of {m['report_cards']} report cards"),
        _tile("Won / lost (HubSpot)", f"{m['won']} / {m['lost']}"),
        _tile("Escalations", m["escalations"], "instant alert to studio head"),
        _tile("Price lines blocked", m["guard_blocks"], "speech guard caught and replaced"),
        _tile("Cost per call", _fmt_inr(m["cost_per_call_inr"]), "Vaani + Claude + Gemini + email"),
        _tile("Cost per booked consultation", _fmt_inr(m["cost_per_booking_inr"])),
    ])
    top = max([1] + [c for _, c in m["reasons"]])
    bars = "".join(f"<div class='row'><div class='name'>{e(r)}</div><div class='track'>"
                   f"<div class='fill' style='width:{100 * c / top:.1f}%' title='{e(r)}: {c}'></div></div>"
                   f"<div class='val'>{c}</div></div>" for r, c in m["reasons"]) or "<p class='sub'>No rejections in this period.</p>"
    trend_table = "".join(f"<tr><td>{d:%a %d %b}</td><td class='num'>{c}</td><td class='num'>{b}</td></tr>"
                          for d, c, b in m["per_day"])
    rows = "".join(
        f"<tr><td>{e(_when(c))}</td><td><a href='/dashboard/call?{urllib.parse.urlencode({'call_id': c['call_id'], **({'token': token} if token else {})})}'>"
        f"{e(c.get('caller_name') or c.get('caller_number') or c['call_id'])}</a></td>"
        f"<td>{e((c.get('fields') or {}).get('location_text') or '—')}</td>"
        f"<td>{e(c.get('decision') or 'processing')} {e(c.get('priority') or '')}</td>"
        f"<td>{e(c.get('reason_code') or '')}</td><td class='num'>{c.get('score') if c.get('score') is not None else '—'}</td>"
        f"<td>{e(c.get('status') or '')}</td><td class='num'>{_fmt_inr(to_inr(call_cost(c)))}</td></tr>"
        for c in m["rows"][:200])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aangan Phone Agent</title><style>{CSS}</style></head><body class="viz-root"><div class="wrap">
<h1>Aangan Studio phone agent</h1>
<p class="sub">Every call answered, qualified and routed, with what it cost. {m['start']:%d %b} – {m['now']:%d %b %Y}</p>
<div class="filters">{filters}</div>{warn}
<div class="hero"><div class="card"><div class="label sub" style="margin:0">Consultations booked on the call</div>
<div class="big">{m['booked_on_call']}</div><div class="sub" style="margin:4px 0 0">from {m['calls']} calls · total running cost {_fmt_inr(m['total_cost_inr'])}</div></div>
<div class="tiles">{tiles}</div></div>
<div class="grid2"><div class="card"><h2>Calls and bookings per day</h2>
<div class="legend"><span><span class="key" style="background:var(--series-1)"></span>Calls</span>
<span><span class="key" style="background:var(--series-2)"></span>Booked on the call</span></div>{_trend_svg(m['per_day'])}
<details><summary>Table view</summary><table><tr><th>Day</th><th>Calls</th><th>Booked</th></tr>{trend_table}</table></details></div>
<div class="card bars"><h2>Not booked, by reason</h2>{bars}</div></div>
<div class="card"><h2>Calls</h2><div class="tablewrap"><table><tr><th>When</th><th>Caller</th><th>Area</th><th>Outcome</th>
<th>Reason</th><th>Score</th><th>Status</th><th>Cost</th></tr>{rows}</table></div></div>
</div></body></html>"""


def _when(c):
    t = _ts(c.get("started_at") or c.get("created_at"))
    return t.astimezone(config.IST).strftime("%d %b %H:%M") if t else "—"


def render_call(c: dict) -> str:
    f = c.get("fields") or {}
    gates = "".join(f"<tr><td>{g['number']}</td><td>{e(g['name'])}</td><td>{e(g['status'])}</td>"
                    f"<td>{e(g.get('reason_code') or '')}</td><td>{e(g.get('note') or '')}</td></tr>"
                    for g in c.get("gates") or [])
    score = "".join(f"<tr><td>{e(l['factor'])}</td><td class='num'>{l['points']}/{l['max_points']}</td>"
                    f"<td>{e(l.get('quote') or '')}</td><td>{e(l.get('note') or '')}</td></tr>" for l in c.get("score_lines") or [])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aangan Call Detail</title><style>{CSS}</style></head><body class="viz-root"><div class="wrap">
<h1>{e(c.get('caller_name') or c.get('caller_number') or c['call_id'])}</h1>
<p class="sub">{e(_when(c))} · {e(c.get('decision') or 'processing')} {e(c.get('priority') or '')} {e(c.get('reason_code') or '')}
· score {c.get('score', '—')} · status {e(c.get('status') or '')}</p>
<div class="grid2"><div class="card"><h2>Five checks</h2><table><tr><th>#</th><th>Check</th><th>Result</th><th>Code</th><th>Note</th></tr>{gates}</table></div>
<div class="card"><h2>Interest score</h2><table><tr><th>Factor</th><th>Points</th><th>Quote</th><th></th></tr>{score}</table></div></div>
<div class="card" style="margin-bottom:16px"><h2>Flags</h2><p>{'<br>'.join(e(x) for x in c.get('flags') or []) or '—'}</p>
<p class="sub">Slot: {e(c.get('slot_start') or '—')} · Designer: {e(c.get('designer_email') or '—')} · Email: {e(c.get('invitee_email') or '—')}
· Location: {e(f.get('location_text') or '—')}</p></div>
<div class="card"><h2>Transcript</h2><pre>{e(c.get('transcript') or '')}</pre></div>
</div></body></html>"""
