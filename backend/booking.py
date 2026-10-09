"""One booking interface for the agent, whichever calendar the studio uses (BOOKING_PROVIDER in .env)."""
from . import calcom, calendly, config


def provider() -> str:
    return config.BOOKING_PROVIDER


def open_slots(preference: str | None = None) -> list[dict]:
    return calcom.open_slots(preference) if provider() == "calcom" else calendly.open_slots(preference)


def book(slot_id: str, full_name: str, email: str, call_id: str, visit_type: str,
         site_address: str | None = None, phone: str | None = None) -> dict:
    """-> {ref, event, cancel_url, reschedule_url, designer_email?, designer_name?}. Raises HttpError."""
    if provider() == "calcom":
        return calcom.book(slot_id, full_name, email, call_id, visit_type, site_address, phone)
    inv = calendly.book(slot_id, full_name, email, call_id)
    return {"ref": inv.get("uri"), "event": inv.get("event"), "cancel_url": inv.get("cancel_url"),
            "reschedule_url": inv.get("reschedule_url"), "designer_email": None, "designer_name": None}


def is_slot_problem(err) -> bool:
    return calcom.is_slot_problem(err) if provider() == "calcom" else calendly.is_slot_problem(err)
