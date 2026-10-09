"""Email content: designer report card, 7pm digest, instant alerts. Templates: docs/report_card_template.md.

These go to Aangan staff only (never to callers), so caller quotes are included verbatim.
"""
import hashlib
import hmac
import html
import urllib.parse

from . import config
from .calendly import parse_time, spoken_label

REASON_EXPLAINED = {
    "OUT_OF_AREA": "Property is outside Pune / PCMC",
    "ADVICE_ONLY": "Wanted ideas or advice only, not design + execution",
    "OUT_OF_SCOPE_TYPE": "A project type we don't take",
    "TOO_SMALL": "Below our minimum scope",
    "TIMELINE_IMPOSSIBLE": "Needed sooner than we can deliver",
    "BUDGET_MISALIGNED": "Volunteered a budget clearly below the scope",
    "NOT_DECISION_MAKER": "Caller can't decide and the deciders won't be involved",
    "EXISTING_CLIENT_COMPLAINT": "Existing client with a complaint, escalated to studio head",
    "NO_INFO": "No conversation on the call",
}
_REASON_QUOTE_FIELD = {
    "OUT_OF_AREA": "location_quote", "ADVICE_ONLY": "service_quote", "OUT_OF_SCOPE_TYPE": "service_quote",
    "TOO_SMALL": "size_quote", "TIMELINE_IMPOSSIBLE": "timeline_quote", "BUDGET_MISALIGNED": "budget_quote",
    "NOT_DECISION_MAKER": "decision_maker_quote", "EXISTING_CLIENT_COMPLAINT": "existing_client_quote",
}


def reask_sig(call_id: str) -> str:
    return hmac.new((config.APP_SECRET or "dev").encode(), call_id.encode(), hashlib.sha256).hexdigest()[:24]


def reask_link(call_id: str) -> str:
    q = urllib.parse.urlencode({"call_id": call_id, "sig": reask_sig(call_id)})
    return f"{config.PUBLIC_BASE_URL}/api/reask?{q}"


def _slot_text(row: dict) -> str:
    return spoken_label(parse_time(row["slot_start"])) if row.get("slot_start") else "not booked"


def _project_line(f: dict) -> str:
    kind = f.get("property_description") or f.get("property_category") or "project"
    return f"{kind} {f.get('location_text') or ''}".strip()


def plain_reason(row: dict) -> str:
    code = row.get("reason_code") or ""
    f = row.get("fields") or {}
    base = REASON_EXPLAINED.get(code, "Exploring, no commitment yet" if row.get("decision") == "Nurture" else "")
    note = next((g.get("note") for g in (row.get("gates") or []) if g.get("reason_code") == code and g.get("note")), "")
    quote = f.get(_REASON_QUOTE_FIELD.get(code, ""), "") or f.get("exploring_quote") or ""
    parts = [base]
    if note:
        parts.append(f"({note})")
    if quote:
        parts.append(f'— "{quote}"')
    return " ".join(p for p in parts if p)


# --- report card -------------------------------------------------------------------------

def report_card(row: dict) -> tuple[str, str, str]:
    f = row.get("fields") or {}
    lines = row.get("score_lines") or []
    breakdown = " · ".join(f"{l['factor']} {l['points']}/{l['max_points']}" for l in lines)
    gates = row.get("gates") or []
    passed = sum(1 for g in gates if g.get("status") != "fail")
    gate_line = "  ".join(
        f"{g['number']} {g['name'].split(' (')[0]} {'✅' if g['status'] == 'pass' else '❔' if g['status'] == 'unclear' else '❌'}"
        for g in gates)
    quotes = [l["quote"] for l in lines if l.get("quote")]
    rooms = ", ".join(f.get("rooms") or []) or "—"
    size = f"{f['size_sqft']:,.0f} sq ft" if f.get("size_sqft") else "size not given"
    p_reason = "; ".join(row.get("priority_reasons") or []) or "flexible timeline"
    name = row.get("invitee_name") or row.get("caller_name") or f.get("caller_name") or "Caller"

    subject = (f"[{row.get('priority') or 'P2'}] Consultation {_slot_text(row)} — {name}, "
               f"{_project_line(f)} — Interest {row.get('score', 0)}/100")
    where = (f"Site visit — {row['site_address']}" if row.get("site_address") else "Site visit")         if row.get("visit_type") == "site_visit" else "Studio"
    text = f"""CONSULTATION BOOKED: {_slot_text(row)} · {where}
Booking: {row.get('event_uri') or '—'} ({row.get('booking_provider') or 'calendar'}) · Reschedule/cancel links are with the client
PRIORITY: {row.get('priority') or 'P2'} — {p_reason}
INTEREST SCORE: {row.get('score', 0)}/100  ({breakdown})

CALLER
{name} · {row.get('caller_number') or '—'} · {row.get('invitee_email') or '—'} · Called at {row.get('started_at') or '—'} · {row.get('duration_sec') or '—'} sec
Source: {f.get('source', 'unknown')}{' (' + f['referrer_name'] + ')' if f.get('referrer_name') else ''}

PROJECT
Type: {f.get('property_category', '—')} / {f.get('scope_unit', '—')}
Location: {f.get('location_text') or '—'}
Size: {size} · Rooms: {rooms}
Current state: {f.get('current_state', 'unknown')}{' · rented' if f.get('rented') else ''}

TIMELINE
Target: {f.get('timeline_text') or 'not discussed'}

RUBRIC ({passed} of {len(gates)} passed)
{gate_line}

WHAT THEY SAID (quotes behind the score)
""" + "\n".join(f'"{q}"' for q in quotes) + """

FLAGS FOR THE DESIGNER
""" + ("\n".join(f"- {x}" for x in row.get("flags") or []) or "- none") + f"""

Full transcript: {config.PUBLIC_BASE_URL}/dashboard/call?call_id={urllib.parse.quote(row['call_id'])}
Had to re-ask the basics? Click: {reask_link(row['call_id'])}
"""
    body_html = (f"<pre style='font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;white-space:pre-wrap'>"
                 f"{html.escape(text)}</pre>"
                 f"<p><a href='{html.escape(reask_link(row['call_id']))}'>☐ I had to re-ask the basics</a></p>")
    return subject, text, body_html


def time_changed(row: dict) -> tuple[str, str]:
    name = row.get("invitee_name") or row.get("caller_name") or "Caller"
    return (f"Time changed — {name}: now {_slot_text(row)}",
            f"{name} rescheduled their consultation. New time: {_slot_text(row)}.\nEvent: {row.get('event_uri')}")


def cancelled(row: dict) -> tuple[str, str]:
    name = row.get("invitee_name") or row.get("caller_name") or "Caller"
    return (f"Cancelled — {name} ({_slot_text(row)})",
            f"{name} cancelled their consultation ({_slot_text(row)}). "
            f"The front desk will call them back next working morning: {row.get('caller_number') or '—'}.")


# --- alerts --------------------------------------------------------------------------------

def escalation_alert(call_id: str, caller_number: str | None, caller_name: str, project: str, issue: str):
    return (f"URGENT: senior callback needed — {caller_name or 'caller'}",
            f"An existing client / complaint call needs a senior callback now.\n\n"
            f"Caller: {caller_name or '—'} · {caller_number or '—'}\nProject: {project or '—'}\nIssue: {issue}\n"
            f"Call id: {call_id}\nThe agent promised a senior callback.")


def front_desk_alert(kind: str, row: dict, detail: str = "") -> tuple[str, str]:
    name = row.get("invitee_name") or row.get("caller_name") or (row.get("fields") or {}).get("caller_name") or "caller"
    return (f"Front desk: {kind} — {name}",
            f"{kind}.\nCaller: {name} · {row.get('caller_number') or '—'}\n"
            f"Project: {_project_line(row.get('fields') or {})}\n{detail}\nCall id: {row['call_id']}")


# --- 7pm digest ------------------------------------------------------------------------------

def digest(rows: list[dict], follow_ups: list[dict], date_label: str) -> tuple[str, str, str]:
    def what(f):
        return f"{f.get('property_description') or f.get('property_category') or '—'}" + (
            f" ({', '.join(f['rooms'])})" if f.get("rooms") else "")

    table = []
    for r in rows:
        f = r.get("fields") or {}
        t = parse_time(r["started_at"]).astimezone(config.IST).strftime("%H:%M") if r.get("started_at") else "—"
        outcome = "booking pending" if r.get("status") == "booking_pending" else r.get("decision") or "—"
        table.append([t, r.get("caller_name") or f.get("caller_name") or r.get("caller_number") or "—",
                      f.get("location_text") or "—", what(f), outcome, r.get("reason_code") or "—", plain_reason(r)])

    head = ["Time", "Caller", "Area", "What they wanted", "Outcome", "Reason code", "Why, in plain words"]
    subject = f"Aangan daily digest {date_label}: {len(rows)} call(s) not booked" + (
        f", {len(follow_ups)} follow-up(s) due" if follow_ups else "")
    text = "\n".join(" | ".join(row) for row in [head] + table) if table else "No unbooked calls today."
    if follow_ups:
        text += "\n\nNURTURE FOLLOW-UPS DUE\n" + "\n".join(
            f"- {r.get('caller_name') or '—'} · {r.get('caller_number') or '—'} · {_project_line(r.get('fields') or {})}"
            for r in follow_ups)
    text += "\n\nReply \"overturn <caller>\" on any row and the front desk will call them back."

    cell = "padding:6px 8px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top;font-size:13px"
    html_rows = "".join("<tr>" + "".join(f"<td style='{cell}'>{html.escape(c)}</td>" for c in row) + "</tr>"
                        for row in table)
    body_html = (f"<p>{len(rows)} call(s) today were not booked.</p>"
                 f"<table style='border-collapse:collapse'><tr>"
                 + "".join(f"<th style='{cell}'>{h}</th>" for h in head) + f"</tr>{html_rows}</table>"
                 + (f"<h3>Nurture follow-ups due</h3><pre>{html.escape(text.split('NURTURE FOLLOW-UPS DUE')[1])}</pre>"
                    if follow_ups else "")
                 + "<p>Reply “overturn &lt;caller&gt;” on any row and the front desk will call them back.</p>")
    return subject, text, body_html
