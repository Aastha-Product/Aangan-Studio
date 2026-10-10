"""HubSpot CRM: contact upsert + deal at "Consultation booked" (amount always EMPTY — no pricing)."""
from . import config
from .http import request_json

API = "https://api.hubapi.com"
DEAL_TO_CONTACT = 3   # HubSpot-defined association type id


def _h():
    return {"Authorization": f"Bearer {config.HUBSPOT_TOKEN}"}


def find_contact(email: str) -> str | None:
    body = {"filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email"], "limit": 1}
    res = request_json("POST", f"{API}/crm/v3/objects/contacts/search", _h(), body)
    results = (res or {}).get("results") or []
    return results[0]["id"] if results else None


def upsert_contact(email: str, full_name: str, phone: str | None) -> str:
    first, _, last = (full_name or "").partition(" ")
    props = {"email": email, "firstname": first, "lastname": last}
    if phone:
        props["phone"] = phone
    existing = find_contact(email)
    if existing:
        request_json("PATCH", f"{API}/crm/v3/objects/contacts/{existing}", _h(), {"properties": props})
        return existing
    return request_json("POST", f"{API}/crm/v3/objects/contacts", _h(), {"properties": props})["id"]


def create_deal(contact_id: str, deal_name: str) -> str:
    props = {"dealname": deal_name, "pipeline": config.HUBSPOT_PIPELINE}
    if config.HUBSPOT_STAGE_BOOKED:
        props["dealstage"] = config.HUBSPOT_STAGE_BOOKED
    # NOTE: "amount" is deliberately never set (THE CUT — no pricing anywhere).
    body = {"properties": props,
            "associations": [{"to": {"id": contact_id},
                              "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                         "associationTypeId": DEAL_TO_CONTACT}]}]}
    return request_json("POST", f"{API}/crm/v3/objects/deals", _h(), body)["id"]


def update_deal(deal_id: str, props: dict) -> dict:
    assert "amount" not in props          # THE CUT — no pricing anywhere
    return request_json("PATCH", f"{API}/crm/v3/objects/deals/{deal_id}", _h(), {"properties": props})


def get_deal(deal_id: str) -> dict:
    q = "properties=dealname,dealstage,pipeline,amount&associations=contacts"
    return request_json("GET", f"{API}/crm/v3/objects/deals/{deal_id}?{q}", _h())


def archive(object_type: str, object_id: str) -> None:
    """Moves a record to HubSpot's recycle bin (restorable for 90 days). Used only by the setup check."""
    request_json("DELETE", f"{API}/crm/v3/objects/{object_type}/{object_id}", _h())


def deal_outcomes(deal_ids: list[str]) -> dict[str, str]:
    """{deal_id: 'won' | 'lost' | 'open'} via batch read."""
    out = {}
    for i in range(0, len(deal_ids), 100):
        chunk = deal_ids[i:i + 100]
        body = {"properties": ["hs_is_closed_won", "hs_is_closed", "dealstage"],
                "inputs": [{"id": d} for d in chunk]}
        res = request_json("POST", f"{API}/crm/v3/objects/deals/batch/read", _h(), body)
        for r in (res or {}).get("results", []):
            p = r.get("properties", {})
            won = str(p.get("hs_is_closed_won")).lower() == "true"
            closed = str(p.get("hs_is_closed")).lower() == "true"
            out[r["id"]] = "won" if won else "lost" if closed else "open"
    return out
