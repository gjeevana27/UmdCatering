"""
LIVE evaluation of llm_client.judge_contract_change() -- the ambiguous-
change judgment layer contract_agent.py falls back to when a rule can't
decide (a reworded event_type, a menu description edit).

Unlike evaluate.py and evaluate_contract_agent.py, this makes REAL
Gemini API calls -- it's the only one of the three evals that actually
tests whether the LLM's judgment is currently good, not just whether the
surrounding code correctly plugs its output in. Deliberately kept
separate and never auto-run by anything else: it costs a small amount
(a handful of short text-only judgment calls, well under a cent total
at gemini-3.5-flash-lite pricing) and requires GEMINI_API_KEY.

Scored on decision CATEGORY only ("escalate" vs "review"), not the
exact reasoning text -- the category is far more stable call to call
than the model's exact wording. A mismatch here means the current
model's judgment disagrees with the hand-labeled expectation on a case
chosen to have a clear, defensible answer -- treat it as a prompt-
quality signal worth investigating, not an automatic pass/fail gate the
way the two deterministic evals are.

Run from the project root (requires GEMINI_API_KEY):
    export GEMINI_API_KEY=...
    python evaluation/evaluate_llm_judgment.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import llm_client  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "labeled_llm_judgments.json"


def main():
    if not llm_client.is_llm_available():
        print("GEMINI_API_KEY is not set -- this eval makes real API calls "
              "and can't run without one.")
        print("export GEMINI_API_KEY=...   # free key, no credit card: "
              "https://aistudio.google.com/apikey")
        return 1

    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]

    correct = 0
    mismatches = []

    print("=" * 60)
    print("EVALUATION -- llm_client.judge_contract_change() (LIVE -- makes "
          "real, billed API calls)")
    print("=" * 60)

    for case in cases:
        try:
            result = llm_client.judge_contract_change(
                field_label=case["field_label"],
                old_value=case["old_value"],
                new_value=case["new_value"],
                context=case.get("context", ""),
                item_label=case.get("item_label", ""),
            )
        except Exception as e:
            print(f"[ERROR] {case['id']}: LLM call failed: {e}")
            mismatches.append({"id": case["id"], "error": str(e)})
            continue

        actual = result.get("decision")
        expected = case["expected_decision"]
        ok = actual == expected
        correct += int(ok)
        status = "OK" if ok else "MISMATCH"
        print(f"[{status}] {case['id']}: expected={expected}, actual={actual}")
        print(f"    reasoning: {result.get('reasoning', '')}")
        if not ok:
            mismatches.append({"id": case["id"], "expected": expected, "actual": actual})

    total = len(cases)
    accuracy = correct / total if total else float("nan")

    print("\n" + "=" * 60)
    print("AGGREGATE")
    print("=" * 60)
    print(f"Decision-category accuracy: {correct}/{total} ({accuracy:.2f})")
    if mismatches:
        print(f"Mismatched/errored cases: {mismatches}")

    print("\nNote: a mismatch means the model's live judgment disagreed "
          "with a hand-labeled expectation on a case chosen to have a "
          "clear answer -- worth a look at the prompt in llm_client.py, "
          "not necessarily a bug elsewhere in the codebase.")

    return 0 if not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
