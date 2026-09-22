"""
Evaluates the agent's escalation decisions against hand-labeled ground truth.

Reports:
  - Escalation precision/recall: of the things the agent escalated, how many
    SHOULD have been escalated (precision), and of the things that SHOULD
    have been escalated, how many did the agent catch (recall)?
  - Safety recall: recall computed ONLY over allergen_conflict findings,
    reported separately because a missed allergen escalation is a
    categorically worse failure than a missed menu-count discrepancy, and
    averaging them together would hide that.

Run from the project root:
    python evaluation/evaluate.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent import review_event, DECISION_ESCALATE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LABELS_PATH = Path(__file__).resolve().parent / "labeled_cases.json"
EVENTS_DIR = ROOT / "data" / "sample_events"


def _finding_key(finding_type, item):
    return (finding_type, item.strip().lower())


def evaluate_event(event_id, labels):
    review = review_event(EVENTS_DIR / event_id)
    escalated_keys = {
        _finding_key(d.finding.finding_type, d.finding.item)
        for d in review.escalations()
    }

    must_escalate = {
        _finding_key(x["finding_type"], x["item"])
        for x in labels.get("must_escalate", [])
    }
    must_not_escalate = {
        _finding_key(x["finding_type"], x["item"])
        for x in labels.get("must_not_escalate", [])
    }

    true_positives = escalated_keys & must_escalate
    false_negatives = must_escalate - escalated_keys
    false_positives = escalated_keys & must_not_escalate

    return {
        "event_id": event_id,
        "escalated": escalated_keys,
        "true_positives": true_positives,
        "false_negatives": false_negatives,
        "false_positives": false_positives,
        "total_findings": len(review.decisions),
        "total_escalated": len(escalated_keys),
    }


def main():
    labels = json.loads(LABELS_PATH.read_text())
    labels.pop("_note", None)

    all_tp, all_fn, all_fp = 0, 0, 0
    allergen_labels_total, allergen_caught = 0, 0

    print("=" * 60)
    print("EVALUATION -- event compliance agent")
    print("=" * 60)

    for event_id, event_labels in labels.items():
        result = evaluate_event(event_id, event_labels)
        all_tp += len(result["true_positives"])
        all_fn += len(result["false_negatives"])
        all_fp += len(result["false_positives"])

        for key in {
            _finding_key(x["finding_type"], x["item"])
            for x in event_labels.get("must_escalate", [])
            if x["finding_type"] == "allergen_conflict"
        }:
            allergen_labels_total += 1
            if key in result["true_positives"]:
                allergen_caught += 1

        print(f"\nEvent {event_id}: {result['total_findings']} findings, "
              f"{result['total_escalated']} escalated")
        if result["false_negatives"]:
            print(f"  MISSED escalations (should have flagged): "
                  f"{result['false_negatives']}")
        if result["false_positives"]:
            print(f"  OVER-escalated (should NOT have flagged): "
                  f"{result['false_positives']}")
        if not result["false_negatives"] and not result["false_positives"]:
            print("  All labeled cases matched.")

    precision = all_tp / (all_tp + all_fp) if (all_tp + all_fp) else float("nan")
    recall = all_tp / (all_tp + all_fn) if (all_tp + all_fn) else float("nan")
    allergen_recall = (allergen_caught / allergen_labels_total
                        if allergen_labels_total else float("nan"))

    print("\n" + "=" * 60)
    print("AGGREGATE")
    print("=" * 60)
    print(f"Escalation precision: {precision:.2f}" if precision == precision
          else "Escalation precision: n/a (no escalations)")
    print(f"Escalation recall:    {recall:.2f}" if recall == recall
          else "Escalation recall: n/a (no labeled positives)")
    print(f"Allergen-conflict recall (safety-critical subset): "
          f"{allergen_recall:.2f}" if allergen_recall == allergen_recall
          else "Allergen-conflict recall: n/a")
    print("\nNote: allergen-conflict recall is reported separately from "
          "overall recall on purpose -- a missed allergen escalation is a "
          "different class of failure than a missed menu-count mismatch, "
          "and averaging them would hide a safety-critical miss behind "
          "decent overall numbers.")


if __name__ == "__main__":
    main()
