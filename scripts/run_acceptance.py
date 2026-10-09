"""Build step 3 — acceptance test.

Runs the qualification pipeline on T01–T20 (data/enquiries.txt) and compares with
data/test_set_phone_calls.csv on decision, priority and reason code.

    python scripts/run_acceptance.py              # uses cached extractions where present
    python scripts/run_acceptance.py --refresh    # re-extract everything with Gemini
    python scripts/run_acceptance.py --only T07 T10

Writes out/acceptance_report.md and out/acceptance_results.json. Exit code 1 on any mismatch.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qualify.env import ROOT  # noqa: E402
from qualify.extract import extract_fields  # noqa: E402
from qualify.gemini import GeminiError  # noqa: E402
from qualify.rules import evaluate  # noqa: E402
from qualify.transcripts import load_phone_calls  # noqa: E402

OUT = ROOT / "out"


def load_expected() -> dict[str, dict]:
    with open(ROOT / "data" / "test_set_phone_calls.csv", encoding="utf-8", newline="") as fh:
        return {row["call_id"]: row for row in csv.DictReader(fh)}


def compare(res, exp) -> list[str]:
    diffs = []
    if res.decision != exp["expected_decision"]:
        diffs.append(f"decision {res.decision} ≠ {exp['expected_decision']}")
    if res.priority != exp["priority"]:
        diffs.append(f"priority {res.priority or '–'} ≠ {exp['priority'] or '–'}")
    if res.reason_code != exp["reason_code"]:
        diffs.append(f"reason {res.reason_code or '–'} ≠ {exp['reason_code'] or '–'}")
    return diffs


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-extract with Gemini, ignoring cache")
    ap.add_argument("--only", nargs="*", help="call ids to run, e.g. T07 T10")
    args = ap.parse_args()

    calls = load_phone_calls()
    expected = load_expected()
    ids = args.only or sorted(expected)

    rows, results, tokens = [], [], {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    for cid in ids:
        call, exp = calls[cid], expected[cid]
        try:
            record = extract_fields(call, refresh=args.refresh)
        except GeminiError as e:
            print(f"{cid}: extraction failed. {e}")
            return 2
        for k in tokens:
            tokens[k] += record["usage"].get(k, 0)
        res = evaluate(cid, record["fields"], call.text, call.after_hours)
        diffs = compare(res, exp)
        rows.append((cid, exp, res, diffs))
        results.append({"call_id": cid, "match": not diffs, "diffs": diffs, "expected": exp,
                        "result": res.to_dict(), "fields": record["fields"], "usage": record["usage"]})
        print(f"{cid}  {'MATCH   ' if not diffs else 'MISMATCH'}  {res.decision:<13} {res.priority:<2} "
              f"{res.reason_code:<26} score {res.score:>3}   {'; '.join(diffs)}")

    matched = sum(1 for *_, d in rows if not d)
    print(f"\n{matched}/{len(rows)} match.  Gemini tokens: {tokens}")

    OUT.mkdir(exist_ok=True)
    (OUT / "acceptance_results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "acceptance_report.md").write_text(render_report(rows, matched, tokens), encoding="utf-8")
    print(f"Report: {OUT / 'acceptance_report.md'}")
    return 0 if matched == len(rows) else 1


def render_report(rows, matched, tokens) -> str:
    md = ["# Acceptance test: T01–T20", "",
          f"**{matched}/{len(rows)} match** on decision + priority + reason code.  ",
          f"Gemini tokens: {tokens['input_tokens']:,} in · {tokens['output_tokens']:,} out · "
          f"{tokens['thinking_tokens']:,} thinking", "",
          "| Call | Expected | Got | Score | Match | Flags (got) | Flags (expected) |",
          "|---|---|---|---|---|---|---|"]
    for cid, exp, res, diffs in rows:
        e = " ".join(x for x in (exp["expected_decision"], exp["priority"], exp["reason_code"]) if x)
        g = " ".join(x for x in (res.decision, res.priority, res.reason_code) if x)
        md.append(f"| {cid} | {e} | {g} | {res.score} | {'✅' if not diffs else '❌ ' + '; '.join(diffs)} | "
                  f"{'<br>'.join(res.flags)} | {exp['flags']} |")

    md += ["", "## Per-call detail", ""]
    for cid, exp, res, diffs in rows:
        md.append(f"### {cid}: {res.decision} {res.priority} {res.reason_code}".rstrip())
        if res.priority_reasons:
            md.append(f"Priority because: {', '.join(res.priority_reasons)}  ")
        for g in res.gates:
            icon = {"pass": "✅", "fail": "❌", "unclear": "❔"}[g.status]
            md.append(f"- Gate {g.number} {g.name}: {icon} {g.reason_code} {g.note}".rstrip())
        if res.score_lines:
            md.append(f"- **Interest {res.score}/100**")
            for line in res.score_lines:
                q = f' "{line.quote}"' if line.quote else ""
                md.append(f"  - {line.factor} {line.points}/{line.max_points}{q} {line.note}".rstrip())
        md.append("")
    return "\n".join(md)


if __name__ == "__main__":
    sys.exit(main())
