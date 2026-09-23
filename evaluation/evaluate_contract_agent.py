"""
Evaluates contract_agent.py's decision layer -- Good-to-Go's
escalate/review classifier -- against hand-labeled synthetic change
scenarios (labeled_contract_changes.json).

Deliberately scoped to RULE-decided cases only -- no GEMINI_API_KEY
needed, fully deterministic, no network dependency. Genuinely ambiguous
free-text changes (an event_type reword, a menu description edit) go
through llm_client.judge_contract_change() instead of a rule, and aren't
covered here -- see evaluate_llm_judgment.py for that path.

Run from the project root:
    python evaluation/evaluate_contract_agent.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import contract_agent  # noqa: E402
import contract_store as store  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "labeled_contract_changes.json"


def _build_change(case: dict):
    """Constructs the same FieldChange/MenuChange objects
    contract_store.diff_records() would normally produce, directly from
    a labeled case -- lets this eval exercise contract_agent.py's
    decision functions in isolation, without needing two full
    ContractRecord fixtures to diff against each other."""
    if case["type"] == "field":
        old_val, new_val = case["old_value"], case["new_value"]
        # Mirrors diff_records()'s own likely_extraction_miss computation
        # exactly, since that flag isn't set automatically when building
        # a FieldChange directly like this.
        likely_miss = bool(old_val.strip()) and not new_val.strip()
        return store.FieldChange(field=case["field"], old_value=old_val,
                                  new_value=new_val, likely_extraction_miss=likely_miss)

    # "menu"
    if case["change_type"] in ("added", "removed"):
        return store.MenuChange(change_type=case["change_type"],
                                 recipe_name=case["recipe_name"],
                                 detail=f"{case['change_type']} item")

    # "changed" -- this eval only covers qty/unit changes (see module
    # docstring for why description-only changes, which hit the LLM
    # judge, aren't included).
    detail = f"qty/unit: '{case['old_qty']}' → '{case['new_qty']}'"
    return store.MenuChange(change_type="changed", recipe_name=case["recipe_name"],
                             detail=detail)


def main():
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]

    correct = 0
    mismatches = []

    print("=" * 60)
    print("EVALUATION -- contract_agent.py decision layer")
    print("=" * 60)

    for case in cases:
        change = _build_change(case)
        decision = (contract_agent._decide_field_change(change) if case["type"] == "field"
                    else contract_agent._decide_menu_change(change))

        expected = case["expected_decision"]
        actual = decision.decision
        ok = actual == expected
        correct += int(ok)
        status = "OK" if ok else "MISMATCH"
        print(f"[{status}] {case['id']}: expected={expected}, actual={actual} "
              f"(decided by: {decision.made_by})")
        if not ok:
            mismatches.append({"id": case["id"], "expected": expected, "actual": actual})

    total = len(cases)
    accuracy = correct / total if total else float("nan")

    print("\n" + "=" * 60)
    print("AGGREGATE")
    print("=" * 60)
    print(f"Accuracy: {correct}/{total} ({accuracy:.2f})")
    if mismatches:
        print(f"Mismatched cases: {mismatches}")

    print("\nNote: scoped to rule-decided cases only (no GEMINI_API_KEY "
          "needed) -- LLM-judged paths (an event_type reword, a menu "
          "description edit) aren't covered by this automated eval; see "
          "docs/DESIGN.md's Limitations section.")

    return 0 if not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
