"""Talk to the agent in your terminal, as if you were a caller. Needs ANTHROPIC_API_KEY.

    .venv/Scripts/python scripts/talk.py
    .venv/Scripts/python scripts/talk.py --script my_call.txt     # one caller line per row

Uses a local JSON store (out/local_store.json). Calendly booking works only if CALENDLY_* is set;
otherwise the agent gets a tool error and falls back to booking_pending, exactly as on a real outage.
At the end (type 'bye' or Ctrl+C) the call is qualified (needs GEMINI_API_KEY) and the decision is printed.
"""
import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from backend.agent import CallSession  # noqa: E402
from backend.store import LocalStore  # noqa: E402


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", help="file with one caller line per row")
    ap.add_argument("--caller", default="+919800000000")
    args = ap.parse_args()

    store = LocalStore(config.LOCAL_STORE_PATH)
    s = CallSession(f"local-{uuid.uuid4().hex[:8]}", args.caller, store=store)
    print(f"\n[call {s.call_id} · model {s.model} · effort {s.effort}]\nAGENT: {s.opening}")
    lines = Path(args.script).read_text(encoding="utf-8").splitlines() if args.script else None
    try:
        while not s.ended:
            if lines is not None:
                if not lines:
                    break
                text = lines.pop(0).strip()
                if not text:
                    continue
                print(f"YOU:   {text}")
            else:
                text = input("YOU:   ").strip()
            if text.lower() in ("bye", "/end", "exit"):
                break
            print(f"AGENT: {s.caller_says(text)}")
    except (KeyboardInterrupt, EOFError):
        pass
    print(f"\n[call ended · Claude tokens {s.usage} · price lines blocked: {s.blocks}]")
    row = s.finish(process=bool(config.env("GEMINI_API_KEY")))
    if row and row.get("decision"):
        print(f"DECISION: {row['decision']} {row.get('priority') or ''} {row.get('reason_code') or ''} "
              f"· score {row.get('score')} · status {row.get('status')}")
        for f in row.get("flags") or []:
            print(f"  flag: {f}")
    else:
        print("(not qualified yet: add GEMINI_API_KEY to .env to run post-call qualification)")


if __name__ == "__main__":
    main()
