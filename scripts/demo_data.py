"""Fill out/demo_store.json with SYNTHETIC calls so the dashboard can be previewed before real calls exist.

    .venv/Scripts/python scripts/demo_data.py
    .venv/Scripts/python scripts/dev_server.py --demo
Never point production at this file.
"""
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from backend.store import LocalStore  # noqa: E402

DEMO_PATH = config.ROOT / "out" / "demo_store.json"
AREAS = ["Kothrud", "Baner", "Wakad", "Aundh", "Viman Nagar", "Hadapsar", "Undri", "Pimple Saudagar", "Nashik", "Kharadi"]
OUTCOMES = [("Qualified", "P1", None)] * 5 + [("Qualified", "P2", None)] * 6 + [
    ("Not qualified", None, "OUT_OF_AREA"), ("Not qualified", None, "ADVICE_ONLY"), ("Not qualified", None, "ADVICE_ONLY"),
    ("Not qualified", None, "OUT_OF_SCOPE_TYPE"), ("Not qualified", None, "TOO_SMALL"),
    ("Not qualified", None, "BUDGET_MISALIGNED"), ("Nurture", None, "TIMELINE_IMPOSSIBLE"), ("Nurture", None, None),
    ("Escalate", None, "EXISTING_CLIENT_COMPLAINT")]


def main():
    random.seed(7)
    if DEMO_PATH.exists():
        DEMO_PATH.unlink()
    store = LocalStore(DEMO_PATH)
    now = config.now_ist()
    for i in range(48):
        start = now - timedelta(days=random.randint(0, 20), hours=random.randint(0, 23), minutes=random.randint(0, 59))
        decision, priority, reason = random.choice(OUTCOMES)
        booked = decision == "Qualified" and random.random() < 0.85
        status = ("booked" if booked else "booking_pending") if decision == "Qualified" else \
            {"Not qualified": "not_qualified", "Nurture": "nurture", "Escalate": "escalated"}[decision]
        if booked and random.random() < 0.12:
            status = random.choice(["cancelled", "no_show"])
        dur = random.randint(150, 480)
        store.upsert_call({
            "call_id": f"demo-{i:03d}", "caller_name": f"Demo caller {i}", "caller_number": "+9198XXXXXX" + f"{i:02d}",
            "started_at": start.isoformat(), "answered_at": (start + timedelta(seconds=random.randint(1, 3))).isoformat(),
            "ended_at": (start + timedelta(seconds=dur)).isoformat(), "duration_sec": dur, "answered": True,
            "after_hours": not (10 <= start.hour < 19), "decision": decision, "priority": priority, "reason_code": reason,
            "score": random.randint(55, 100) if decision == "Qualified" else random.randint(5, 45), "status": status,
            "fields": {"location_text": random.choice(AREAS)},
            "booked_on_call": booked, "booked_at": (start + timedelta(seconds=dur - 40)).isoformat() if booked else None,
            "report_card_sent_at": start.isoformat() if booked else None,
            "had_to_reask": booked and random.random() < 0.1,
            "deal_outcome": random.choice(["open", "open", "won", "lost"]) if booked else None,
            "usage": {"claude": {"model": "claude-opus-5-5", "input": random.randint(8000, 20000), "output": random.randint(800, 2500),
                                 "cache_read": random.randint(40000, 90000), "cache_write": 4200},
                      "gemini": {"input_tokens": 3500, "output_tokens": 600, "thinking_tokens": 800},
                      "emails": 1 if booked else 0},
            "created_at": start.astimezone(config.IST).isoformat(),
        })
    for _ in range(2):
        store.log_event("demo-001", "speech_guard_block", {"original": "(demo)", "reasons": ["lakh/crore"]})
    print(f"wrote {DEMO_PATH}")


if __name__ == "__main__":
    main()
