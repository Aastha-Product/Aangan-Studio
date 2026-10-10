"""Business actions shared by the voice agent, webhooks and cron jobs.

- escalate()                 instant alert to studio head (T09)
- booking_pending()          qualified caller without a booking -> front desk confirms next working morning
- process_completed_call()   transcript -> extract -> 5 gates -> decision -> log -> follow-up actions
- handle_calendly_event()    invitee.created / canceled / no-show -> status, report card, HubSpot, alerts
- maybe_send_report_card()   sends once the call has BOTH a booking and a qualification result
Alerts never raise: a failed email is logged, the flow carries on.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from qualify.extract import extract_fields
from qualify.rules import evaluate
from qualify.transcripts import Call

from . import calcom, config, emails, hubspot
from .calendly import booking_from_payload, parse_time
from .mailer import send_email
from .speech_guard import find_price_reasons
from .store import utcnow


def _bump_usage(store, call_id: str, key: str, amount=1):
    row = store.get_call(call_id) or {}
    usage = dict(row.get("usage") or {})
    usage[key] = (usage.get(key) or 0) + amount
    store.update_call(call_id, {"usage": usage})


def notify(store, call_id: str | None, to, subject: str, text: str, html: str | None = None,
           reply_to: str | None = None) -> bool:
    to_list = [to] if isinstance(to, str) else list(to or [])
    to_list = [t for t in to_list if t]
    try:
        send_email(to_list, subject, text, html, reply_to)
        store.log_event(call_id, "email_sent", {"to": to_list, "subject": subject})
        if call_id and store.get_call(call_id):
            _bump_usage(store, call_id, "emails")
        return True
    except Exception as e:  # noqa: BLE001 — alerts must never break a call or webhook
        store.log_event(call_id, "email_failed", {"to": to_list, "subject": subject, "error": str(e)[:300]})
        return False


# --- during the call -------------------------------------------------------------------------

def escalate(store, call_id: str, caller_number: str | None, caller_name: str, project: str, issue: str) -> bool:
    subject, text = emails.escalation_alert(call_id, caller_number, caller_name, project, issue)
    sent = notify(store, call_id, config.STUDIO_HEAD_ALERT_EMAIL, subject, text)
    store.update_call(call_id, {"escalated_at": utcnow(), "status": "escalated",
                                "caller_name": caller_name or None})
    return sent


def booking_pending(store, call_id: str, reason: str) -> None:
    row = store.update_call(call_id, {"status": "booking_pending"}) or store.get_call(call_id) or {"call_id": call_id}
    subject, text = emails.front_desk_alert("Confirm a consultation slot next working morning", row, f"Reason: {reason}")
    notify(store, call_id, config.FRONT_DESK_EMAIL, subject, text)


# --- after the call --------------------------------------------------------------------------

def _status_for(decision: str, current: str | None) -> str:
    if decision == "Qualified":
        return current if current in ("booked", "booking_pending", "cancelled", "no_show") else "booking_pending"
    return {"Nurture": "nurture", "Not qualified": "not_qualified",
            "Escalate": "escalated", "No data": "no_data"}.get(decision, "unknown")


def process_completed_call(store, call_id: str, transcript: str, started_at: datetime | None = None,
                           after_hours: bool | None = None) -> dict:
    row = store.get_call(call_id) or {"call_id": call_id}
    started = started_at or (parse_time(row["started_at"]) if row.get("started_at") else datetime.now(timezone.utc))
    started_ist = started.astimezone(config.IST)
    if after_hours is None:
        t = started_ist.time()
        after_hours = t < config.STUDIO_OPENS or t >= config.STUDIO_CLOSES

    call = Call(call_id, started_ist.date(), started_ist.time(),
                f"{call_id} · Phone · {started_ist:%d %B} · {started_ist:%I:%M%p}", transcript)
    record = extract_fields(call, use_cache=False)
    res = evaluate(call_id, record["fields"], transcript, after_hours)

    # Price audit. With our voice bridge every agent line was already guarded, so this never fires there.
    # If Vaani's own LLM ran the call, the guard could not block in real time — catch it here instead.
    spoken_prices = [line for line in transcript.splitlines()
                     if line.lower().startswith("agent:") and find_price_reasons(line.split(":", 1)[1])]
    if spoken_prices:
        res.flags.insert(0, "⚠ AGENT SPOKE A PRICE on this call. Review the transcript")
        store.log_event(call_id, "price_spoken", {"lines": spoken_prices})
        notify(store, call_id, config.STUDIO_HEAD_ALERT_EMAIL, f"Agent spoke a price on call {call_id}",
               "The phone agent said a price (THE CUT was broken):\n\n" + "\n".join(spoken_prices)
               + "\n\nTighten the agent prompt in the Vaani dashboard.")

    usage = dict(row.get("usage") or {})
    usage["gemini"] = record["usage"]
    status = _status_for(res.decision, row.get("status"))
    if row.get("status") == "booked" and res.decision != "Qualified":
        # The calendar already holds the slot (booked live on the call). Keep it, and tell the designer why to look.
        status = "booked"
        res.flags.insert(0, f"⚠ Booked on the call, but the five checks say {res.decision}"
                            f"{' (' + res.reason_code + ')' if res.reason_code else ''}. Review before the consultation")
    changes = {
        "call_id": call_id, "transcript": transcript, "fields": record["fields"],
        "caller_name": row.get("caller_name") or record["fields"].get("caller_name"),
        "decision": res.decision, "priority": res.priority or None, "reason_code": res.reason_code or None,
        "all_fail_codes": res.all_fail_codes, "gates": [asdict(g) for g in res.gates],
        "score": res.score, "score_lines": [asdict(l) for l in res.score_lines], "flags": res.flags,
        "priority_reasons": res.priority_reasons, "after_hours": after_hours,
        "status": status, "processed_at": utcnow(), "usage": usage,
    }
    if res.decision == "Nurture":
        changes["follow_up_on"] = (started_ist.date() + timedelta(days=config.NURTURE_FOLLOW_UP_DAYS)).isoformat()
    store.upsert_call(changes)

    if res.decision == "Escalate" and not row.get("escalated_at"):
        f = record["fields"]
        escalate(store, call_id, row.get("caller_number"), f.get("caller_name") or "",
                 f.get("property_description") or "", f.get("existing_client_quote") or "existing client complaint")
    if res.decision == "Qualified" and status == "booking_pending" and row.get("status") != "booking_pending":
        booking_pending(store, call_id, "qualified on the call but no slot was booked")
    maybe_send_report_card(store, call_id)
    return store.get_call(call_id)


def maybe_send_report_card(store, call_id: str) -> bool:
    row = store.get_call(call_id)
    if not row or row.get("status") != "booked" or not row.get("decision") or row.get("report_card_sent_at"):
        return False
    designer = row.get("designer_email") or (config.DESIGNER_EMAILS[0] if config.DESIGNER_EMAILS else None)
    subject, text, html = emails.report_card(row)
    if notify(store, call_id, designer, subject, text, html, reply_to=config.FRONT_DESK_EMAIL or None):
        store.update_call(call_id, {"report_card_sent_at": utcnow()})
    if config.HUBSPOT_TOKEN and not row.get("hubspot_deal_id") and row.get("invitee_email"):
        try:
            contact = hubspot.upsert_contact(row["invitee_email"], row.get("invitee_name") or row.get("caller_name") or "",
                                             row.get("caller_number"))
            f = row.get("fields") or {}
            deal = hubspot.create_deal(contact, f"{row.get('invitee_name') or 'Caller'} — "
                                                f"{f.get('property_description') or 'interior project'} "
                                                f"{f.get('location_text') or ''} [{call_id}]".strip())
            store.update_call(call_id, {"hubspot_contact_id": contact, "hubspot_deal_id": deal, "deal_outcome": "open"})
        except Exception as e:  # noqa: BLE001
            store.log_event(call_id, "hubspot_failed", {"error": str(e)[:300]})
    return True


# --- booking webhooks (Calendly + Cal.com share one flow) ------------------------------------------
# A normalised booking dict: call_id, ref (booking id we store in invitee_uri), old_ref (reschedule), event,
# invitee_email, invitee_name, slot_start, designer_email, designer_name, cancel_url, reschedule_url.

def _find_booking_row(store, b: dict) -> dict | None:
    for field, value in (("call_id", b.get("call_id")), ("invitee_uri", b.get("ref")),
                         ("invitee_uri", b.get("old_ref")), ("invitee_email", b.get("invitee_email"))):
        if value:
            row = store.get_call(value) if field == "call_id" else store.find_call(field, value)
            if row:
                return row
    return None


def _digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())[-10:]


def _during_call(row: dict) -> bool:
    if not row.get("started_at") or row.get("call_id", "").startswith(("calcom-", "calendly-")):
        return False
    end = parse_time(row["ended_at"]) if row.get("ended_at") else None
    now = datetime.now(timezone.utc)
    return end is None or now - end <= timedelta(minutes=5)


def match_recent_call(store, b: dict, now: datetime | None = None) -> tuple[dict | None, str]:
    """A booking made by Vaani's own Cal.com tool carries no call id. Find the call it came from:
    same phone number, else the same first name, else the only call in progress. Never guess between two."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    open_calls = [c for c in store.list_calls(since)
                  if not str(c.get("call_id", "")).startswith(("calcom-", "calendly-"))
                  and c.get("status") not in ("booked", "cancelled", "no_show")
                  and c.get("decision") in (None, "Qualified")]
    latest = lambda cs: max(cs, key=lambda c: c.get("started_at") or c.get("created_at") or "")  # noqa: E731
    phone = _digits(b.get("invitee_phone"))
    if len(phone) == 10:
        hits = [c for c in open_calls if _digits(c.get("caller_number")) == phone]
        if hits:
            return latest(hits), "phone number"
    first = (b.get("invitee_name") or "").strip().split(" ")[0].lower()
    if len(first) >= 3:
        hits = [c for c in open_calls if first in (c.get("caller_name") or "").lower().split()]
        if len(hits) == 1:
            return hits[0], "caller name"
    recent = [c for c in open_calls
              if (parse_time(c.get("started_at") or c.get("created_at")) if (c.get("started_at") or c.get("created_at"))
                  else now) >= now - timedelta(minutes=45)]
    if len(recent) == 1:
        return recent[0], "only call in progress"
    return None, ""


def apply_booking_event(store, kind: str, b: dict, source: str) -> str:
    """kind: created | rescheduled | cancelled | no_show | no_show_cleared."""
    row = _find_booking_row(store, b)
    store.log_event(row and row["call_id"], f"{source}_webhook", {"kind": kind, "ref": b.get("ref")})

    if kind in ("no_show", "no_show_cleared"):
        if not row:
            return "no matching call"
        if kind == "no_show":
            row = store.update_call(row["call_id"], {"status": "no_show"})
            s, t = emails.front_desk_alert("No-show at consultation: please follow up", row)
            notify(store, row["call_id"], config.FRONT_DESK_EMAIL, s, t)
        else:
            store.update_call(row["call_id"], {"status": "booked"})
        return kind

    if kind in ("created", "rescheduled"):
        if not row and not b.get("old_ref"):   # booked by Vaani's own calendar tool: find the call it came from
            row, how = match_recent_call(store, b)
            if row:
                store.log_event(row["call_id"], "booking_matched", {"by": how, "ref": b.get("ref")})
        if not row:  # booked directly on the calendar page, not via the agent
            row = store.upsert_call({"call_id": f"{source}-{(b.get('ref') or '').rsplit('/', 1)[-1].replace(':', '-')}",
                                     "status": "booked", "caller_name": b.get("invitee_name"),
                                     "caller_number": b.get("invitee_phone")})
        is_reschedule = kind == "rescheduled" or bool(b.get("old_ref")) or bool(row.get("report_card_sent_at"))
        row = store.update_call(row["call_id"], {
            "status": "booked", "slot_start": b.get("slot_start"), "event_uri": b.get("event"),
            "invitee_uri": b.get("ref"), "invitee_email": b.get("invitee_email") or row.get("invitee_email"),
            "invitee_name": b.get("invitee_name") or row.get("invitee_name"),
            "designer_email": b.get("designer_email") or row.get("designer_email"),
            "designer_name": b.get("designer_name") or row.get("designer_name"),
            "cancel_url": b.get("cancel_url"), "reschedule_url": b.get("reschedule_url"),
            "booked_at": row.get("booked_at") or utcnow(), "booking_provider": source,
            "visit_type": b.get("visit_type") or row.get("visit_type"),
            "site_address": b.get("site_address") or row.get("site_address"),
            # booked while the caller was still on the line (Vaani's calendar tool) or within minutes of hanging up
            "booked_on_call": row.get("booked_on_call") or _during_call(row),
        })
        if is_reschedule:
            s, t = emails.time_changed(row)
            notify(store, row["call_id"], row.get("designer_email") or config.DESIGNER_EMAILS, s, t)
            return "rescheduled"
        maybe_send_report_card(store, row["call_id"])
        return "booked"

    if kind == "cancelled":
        if not row:
            return "no matching call"
        row = store.update_call(row["call_id"], {"status": "cancelled"})
        s, t = emails.cancelled(row)
        notify(store, row["call_id"], row.get("designer_email") or config.DESIGNER_EMAILS, s, t)
        s, t = emails.front_desk_alert("Consultation cancelled: call back next working morning", row)
        notify(store, row["call_id"], config.FRONT_DESK_EMAIL, s, t)
        return "cancelled"
    return f"ignored {kind}"


def handle_calendly_event(store, body: dict) -> str:
    event, payload = body.get("event"), body.get("payload") or {}
    if event in ("invitee_no_show.created", "invitee_no_show.deleted"):
        kind = "no_show" if event == "invitee_no_show.created" else "no_show_cleared"
        return apply_booking_event(store, kind, {"ref": payload.get("invitee")}, "calendly")
    b = booking_from_payload(payload)
    norm = {"call_id": b["call_id"], "ref": b["invitee_uri"], "old_ref": b["old_invitee"], "event": b["event_uri"],
            "invitee_email": b["invitee_email"], "invitee_name": b["invitee_name"], "slot_start": b["slot_start"],
            "designer_email": b["designer_email"], "designer_name": b["designer_name"],
            "cancel_url": b["cancel_url"], "reschedule_url": b["reschedule_url"]}
    if event == "invitee.created":
        return apply_booking_event(store, "created", norm, "calendly")
    if event == "invitee.canceled":
        if b["rescheduled"]:
            store.log_event(None, "calendly_webhook", {"kind": "reschedule_in_progress", "ref": norm["ref"]})
            return "reschedule in progress"  # a new invitee.created follows
        return apply_booking_event(store, "cancelled", norm, "calendly")
    return f"ignored {event}"


def handle_calcom_event(store, body: dict) -> str:
    trigger, payload = body.get("triggerEvent"), body.get("payload") or {}
    b = calcom.booking_from_payload(trigger, payload)
    if trigger == "BOOKING_CREATED":
        return apply_booking_event(store, "created", b, "calcom")
    if trigger == "BOOKING_RESCHEDULED":
        return apply_booking_event(store, "rescheduled", b, "calcom")
    if trigger == "BOOKING_CANCELLED":
        return apply_booking_event(store, "cancelled", b, "calcom")
    if trigger == "BOOKING_NO_SHOW_UPDATED":
        return apply_booking_event(store, "no_show" if b.get("no_show") else "no_show_cleared", b, "calcom")
    return f"ignored {trigger}"
