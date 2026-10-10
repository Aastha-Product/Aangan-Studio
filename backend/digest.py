"""7pm digest (build step 8): one email to designers listing today's unbooked calls + nurture follow-ups due.
Also refreshes HubSpot deal outcomes (won/lost) for the dashboard's conversion numbers."""
from datetime import datetime, timedelta, timezone

from . import actions, config, emails, hubspot
from .calendly import parse_time
from .store import utcnow

DIGEST_STATUSES = {"not_qualified", "nurture", "escalated", "booking_pending", "no_data"}


def _ist_date(iso: str | None):
    return parse_time(iso).astimezone(config.IST).date() if iso else None


def build(store, now: datetime | None = None):
    """Every unbooked call not yet in a digest. Not just today's: a call at 9pm comes after today's 7pm run,
    so it goes in tomorrow's. The 3-day window covers a missed cron run without resurfacing old history."""
    now = now or config.now_ist()
    today = now.date()
    since = (now - timedelta(days=3)).astimezone(timezone.utc).isoformat(timespec="seconds")
    calls = store.list_calls(since)
    rows = [c for c in calls if c.get("status") in DIGEST_STATUSES and not c.get("digest_sent_at")
            and (_ist_date(c.get("started_at") or c.get("created_at")) or today) <= today]
    follow_ups = [c for c in store.list_calls() if c.get("status") == "nurture" and c.get("follow_up_on")
                  and c["follow_up_on"] <= today.isoformat()]
    return rows, follow_ups


def run(store, now: datetime | None = None) -> dict:
    now = now or config.now_ist()
    from . import webcall
    webcall.sweep(store, limit=20)          # finish any web calls still waiting for their transcript
    rows, follow_ups = build(store, now)
    subject, text, html = emails.digest(rows, follow_ups, now.strftime("%a %d %b"))
    sent = actions.notify(store, None, config.DESIGNER_EMAILS, subject, text, html,
                          reply_to=config.FRONT_DESK_EMAIL or None)
    if sent:
        for r in rows:
            store.update_call(r["call_id"], {"digest_sent_at": utcnow()})
        for r in follow_ups:
            store.update_call(r["call_id"], {"status": "nurture_followed_up"})
    outcomes = sync_deal_outcomes(store)
    return {"sent": sent, "rows": len(rows), "follow_ups": len(follow_ups), "deal_outcomes_updated": outcomes}


def sync_deal_outcomes(store) -> int:
    if not config.HUBSPOT_TOKEN:
        return 0
    open_deals = {c["hubspot_deal_id"]: c["call_id"] for c in store.list_calls()
                  if c.get("hubspot_deal_id") and c.get("deal_outcome") in (None, "open")}
    if not open_deals:
        return 0
    try:
        outcomes = hubspot.deal_outcomes(list(open_deals))
    except Exception as e:  # noqa: BLE001
        store.log_event(None, "hubspot_failed", {"error": str(e)[:300]})
        return 0
    changed = 0
    for deal_id, outcome in outcomes.items():
        if outcome != "open":
            store.update_call(open_deals[deal_id], {"deal_outcome": outcome})
            changed += 1
    return changed
