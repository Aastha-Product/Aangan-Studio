"""Fill out/demo_store.json with SYNTHETIC calls so the dashboard can be previewed before real calls exist.

Built from the 20 case-study phone calls (T01–T20) and their cached extractions (out/extractions/, written by
scripts/run_acceptance.py), so every demo call has a real transcript, five checks, score quotes and flags.
Each call is replayed 2–3 times on random days of the last three weeks.

    .venv/Scripts/python scripts/demo_data.py
    .venv/Scripts/python scripts/dev_server.py --demo
Never point production at this file.
"""
import json
import random
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from backend.store import LocalStore  # noqa: E402
from qualify.rules import evaluate  # noqa: E402
from qualify.transcripts import load_phone_calls  # noqa: E402

DEMO_PATH = config.ROOT / "out" / "demo_store.json"
CACHE = config.ROOT / "out" / "extractions"
STATUS = {"Not qualified": "not_qualified", "Nurture": "nurture", "Escalate": "escalated", "No data": "no_data"}


def _transcript(text: str) -> str:
    """Case-study format ('Front Desk:' / 'Caller:', wrapped lines) -> the stored 'Agent:' / 'Caller:' format."""
    out = []
    for line in text.splitlines()[1:]:
        if line.startswith("Front Desk:"):
            out.append("Agent:" + line[len("Front Desk:"):])
        elif line.startswith("Caller:"):
            out.append(line)
        elif line.strip() and out:
            out[-1] += " " + line.strip()
    return "\n".join(out)


def main():
    random.seed(7)
    if DEMO_PATH.exists():
        DEMO_PATH.unlink()
    if not any(CACHE.glob("T*.json")):
        sys.exit("No cached extractions in out/extractions. Run scripts/run_acceptance.py first.")
    store = LocalStore(DEMO_PATH)
    calls = load_phone_calls()
    now = config.now_ist()
    n = 0
    for tid, call in sorted(calls.items()):
        cached = CACHE / f"{tid}.json"
        if not cached.exists():
            continue
        fields = json.loads(cached.read_text(encoding="utf-8"))["fields"]
        for rep in range(random.choice([2, 3])):
            start = (now - timedelta(days=random.randint(0, 20))).replace(
                hour=random.choice([8, 9, 11, 13, 15, 17, 20, 21, 22]), minute=random.randint(0, 59), second=0)
            if start > now:
                start -= timedelta(days=1)
            after_hours = not (10 <= start.hour < 19)
            transcript = _transcript(call.text)
            res = evaluate(tid, fields, transcript, after_hours)
            cid = f"demo-{tid}-{rep}"
            dur = random.randint(150, 480)
            row = {
                "call_id": cid, "caller_name": fields.get("caller_name"),
                "caller_number": f"+91 98{random.randint(10, 99)}0 {random.randint(10000, 99999)}",
                "started_at": start.isoformat(), "answered_at": (start + timedelta(seconds=random.randint(1, 4))).isoformat(),
                "ended_at": (start + timedelta(seconds=dur)).isoformat(), "duration_sec": dur, "answered": True,
                "after_hours": after_hours, "transcript": transcript, "fields": fields,
                "decision": res.decision, "priority": res.priority or None, "reason_code": res.reason_code or None,
                "gates": [asdict(g) for g in res.gates], "score": res.score,
                "score_lines": [asdict(s) for s in res.score_lines], "flags": res.flags,
                "priority_reasons": res.priority_reasons, "processed_at": (start + timedelta(seconds=dur + 20)).isoformat(),
                "status": STATUS.get(res.decision, "booking_pending"),
                "usage": {"claude": {"model": "claude-opus-5-5", "input": random.randint(8000, 20000),
                                     "output": random.randint(800, 2500), "cache_read": random.randint(40000, 90000),
                                     "cache_write": 4200},
                          "gemini": {"input_tokens": 1550, "output_tokens": 600, "thinking_tokens": 1500}, "emails": 0},
                "created_at": start.isoformat(),
            }
            if tid == "T08":
                row.update(answered=True, duration_sec=4, transcript="", caller_name=None)
            if res.decision == "Qualified" and random.random() < 0.85:
                slot = (start + timedelta(days=random.randint(2, 9))).replace(hour=random.choice([11, 14, 16]), minute=0)
                row.update(status="booked", booked_on_call=True, booked_at=(start + timedelta(seconds=dur - 40)).isoformat(),
                           slot_start=slot.isoformat(), designer_email="designer@aangan.example",
                           designer_name=random.choice(["Riya Kulkarni", "Aryan Shah"]),
                           invitee_name=fields.get("caller_name"), invitee_email="caller@example.com",
                           report_card_sent_at=(start + timedelta(seconds=dur + 30)).isoformat(),
                           hubspot_deal_id=str(random.randint(10**10, 10**11)),
                           had_to_reask=random.random() < 0.1,
                           deal_outcome=random.choice(["open", "open", "open", "won", "lost"]))
                row["usage"]["emails"] = 1
                if random.random() < 0.12:
                    row["status"] = random.choice(["cancelled", "no_show"])
            if res.decision == "Escalate":
                row["escalated_at"] = (start + timedelta(seconds=dur)).isoformat()
            store.upsert_call(row)
            store.log_event(cid, "email_sent", {"to": ["designer@aangan.example"],
                                                "subject": "Report card" if row["status"] == "booked" else "Daily digest"})
            n += 1
    for _ in range(2):
        store.log_event("demo-T02-0", "speech_guard_block", {"original": "(demo)", "reasons": ["lakh/crore"]})
    print(f"wrote {n} demo calls to {DEMO_PATH}")


if __name__ == "__main__":
    main()
