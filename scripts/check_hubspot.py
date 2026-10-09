"""Check the app's own HubSpot connection (HUBSPOT_ACCESS_TOKEN in .env).

    python scripts/check_hubspot.py              # read-only: token works, pipeline + stage exist
    python scripts/check_hubspot.py --write-test # also: create a labelled test contact + deal exactly as a booking
                                                 # would, verify stage and EMPTY amount, then move both to the recycle bin
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, hubspot  # noqa: E402
from backend.http import HttpError, request_json  # noqa: E402


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not config.HUBSPOT_TOKEN:
        sys.exit("HUBSPOT_ACCESS_TOKEN is empty in .env")
    try:
        request_json("GET", f"{hubspot.API}/crm/v3/objects/contacts?limit=1", hubspot._h())
        print("PASS token can read contacts")
        pipes = request_json("GET", f"{hubspot.API}/crm/v3/pipelines/deals", hubspot._h())["results"]
    except HttpError as e:
        sys.exit(f"FAIL {e.status}: check the private app's scopes (contacts + deals read/write). {str(e.body)[:200]}")
    pipe = next((p for p in pipes if p["id"] == config.HUBSPOT_PIPELINE), None)
    stage = pipe and next((s for s in pipe["stages"] if s["id"] == config.HUBSPOT_STAGE_BOOKED), None)
    print(("PASS" if pipe else "FAIL") + f" pipeline {config.HUBSPOT_PIPELINE!r}" + (f" = {pipe['label']}" if pipe else ""))
    print(("PASS" if stage else "FAIL") + f" stage {config.HUBSPOT_STAGE_BOOKED!r}" + (f" = {stage['label']}" if stage else ""))

    if "--write-test" in sys.argv:
        email = f"aangan-agent-test-{uuid.uuid4().hex[:6]}@example.com"
        contact = hubspot.upsert_contact(email, "Aangan AgentTest", "+910000000000")
        deal = hubspot.create_deal(contact, "TEST — Aangan phone agent setup check (safe to ignore)")
        d = hubspot.get_deal(deal)
        props = d.get("properties", {})
        linked = [a["id"] for a in (d.get("associations", {}).get("contacts", {}).get("results") or [])]
        print(("PASS" if props.get("dealstage") == config.HUBSPOT_STAGE_BOOKED else "FAIL") + f" deal stage = {props.get('dealstage')}")
        print(("PASS" if not props.get("amount") else "FAIL") + f" deal amount empty (THE CUT) = {props.get('amount')!r}")
        print(("PASS" if contact in linked else "FAIL") + " deal linked to contact")
        hubspot.archive("deals", deal)
        hubspot.archive("contacts", contact)
        print("cleaned up: test deal + contact moved to HubSpot's recycle bin")


if __name__ == "__main__":
    main()
