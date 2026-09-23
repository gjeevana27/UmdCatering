"""
Extraction accuracy eval -- the biggest gap in this project's evaluation
coverage. Every downstream decision (contract_agent.py's escalate/review
calls, the notifications a chef actually sees) depends on
extract.extract_contract_record() reading a real photo correctly. The
other three harnesses in this folder all assume the extracted JSON is
already correct and only test what happens after that -- none of them
touch the vision-to-JSON step itself.

This is a LIVE eval (real, billed Gemini vision calls) scored against
hand-labeled ground truth built from REAL contract photos, not synthetic
ones -- synthetic text can't tell you whether the model actually reads a
handwritten correction or a blurry phone photo right. See
data/real_examples/contracts/README.md and
evaluation/label_extraction_example.py for how to build examples. That
folder is gitignored (real customer PII), so this eval has nothing to
score until you've added some yourself -- it prints setup instructions
instead of a fake/empty score in that case.

Reports:
  - field-level accuracy per scalar field (exact match after the same
    whitespace normalization contract_store.py uses for diffing)
  - accuracy broken out SEPARATELY for the fields that actually drive
    contract_agent.py's escalate/review decisions (location, event_time,
    guest_count, event_type) -- a miss on one of these risks a wrong or
    missed escalation downstream, which is a different class of failure
    than a miss on event_id or division
  - menu item name recall/precision (items the model missed vs. ones it
    hallucinated that aren't really on the document) and qty_unit/
    description accuracy on the items that WERE correctly matched
  - confidence calibration: mean per-record accuracy bucketed by the
    extraction's own self-reported _confidence, to check whether "low"
    actually correlates with more errors or the model is just guessing

Run from the project root (requires GEMINI_API_KEY):
    export GEMINI_API_KEY=...
    python evaluation/evaluate_extraction.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import extract  # noqa: E402
import llm_client  # noqa: E402
from contract_store import _normalize_text  # noqa: E402

CONTRACTS_DIR = (Path(__file__).resolve().parent.parent / "data" /
                  "real_examples" / "contracts")
GROUND_TRUTH_DIR = CONTRACTS_DIR / "ground_truth"

SCALAR_FIELDS = ["event_id", "event_date", "event_time", "location",
                  "event_type", "guest_count", "division"]

# The fields that actually drive contract_agent.py's escalate/review
# decisions (see README's decision-rule table / docs/DESIGN.md). A miss
# here is a categorically worse failure than a miss on event_id or
# division, which are identity/routing fields, not decision inputs.
ESCALATION_RELEVANT_FIELDS = {"location", "event_time", "guest_count", "event_type"}


def _norm(v) -> str:
    return _normalize_text(str(v if v is not None else ""))


def _match_menu_items(gt_items, ext_items):
    gt_by_name = {_norm(i["recipe_name"]).lower(): i for i in gt_items}
    ext_by_name = {_norm(i.get("recipe_name", "")).lower(): i for i in ext_items}
    matched = set(gt_by_name) & set(ext_by_name)
    missing = sorted(set(gt_by_name) - set(ext_by_name))   # ground-truth items the model missed
    extra = sorted(set(ext_by_name) - set(gt_by_name))     # items the model hallucinated

    qty_correct = sum(1 for k in matched
                       if _norm(gt_by_name[k].get("qty_unit", "")) ==
                          _norm(ext_by_name[k].get("qty_unit", "")))
    desc_correct = sum(1 for k in matched
                        if _norm(gt_by_name[k].get("description", "")) ==
                           _norm(ext_by_name[k].get("description", "")))
    return {
        "gt_count": len(gt_by_name), "ext_count": len(ext_by_name),
        "matched": len(matched), "missing": missing, "extra": extra,
        "qty_correct": qty_correct, "desc_correct": desc_correct,
    }


def _load_examples():
    if not GROUND_TRUTH_DIR.exists():
        return [], 0
    examples, skipped_unverified = [], 0
    for p in sorted(GROUND_TRUTH_DIR.glob("*.json")):
        gt = json.loads(p.read_text(encoding="utf-8"))
        if not gt.get("_verified"):
            skipped_unverified += 1
            continue
        examples.append((p, gt))
    return examples, skipped_unverified


def evaluate_one(gt):
    photo_path = CONTRACTS_DIR / gt["photo"]
    if not photo_path.exists():
        return None, f"photo not found: {photo_path}"
    try:
        result = extract.extract_contract_record(str(photo_path))
    except extract.ExtractionError as e:
        return None, f"extraction failed: {e}"

    field_results = {}
    for f in SCALAR_FIELDS:
        gt_val, ext_val = gt.get(f), result.get(f)
        ok = (gt_val == ext_val) if f == "guest_count" else (_norm(gt_val) == _norm(ext_val))
        field_results[f] = {"gt": gt_val, "extracted": ext_val, "correct": ok}

    menu = _match_menu_items(gt.get("menu_items", []), result.get("menu_items", []))
    menu_all_correct = (not menu["missing"] and not menu["extra"]
                         and menu["qty_correct"] == menu["gt_count"])

    n_correct = sum(1 for r in field_results.values() if r["correct"])
    record_correct = n_correct + (1 if menu_all_correct else 0)
    record_total = len(field_results) + 1

    return {
        "photo": gt["photo"],
        "confidence": result.get("_confidence", "unknown"),
        "confidence_notes": result.get("_confidence_notes", ""),
        "fields": field_results,
        "menu": menu,
        "record_accuracy": record_correct / record_total,
    }, None


def main():
    if not llm_client.is_llm_available():
        print("GEMINI_API_KEY is not set -- this eval makes real vision API "
              "calls and can't run without one.")
        print("export GEMINI_API_KEY=...   # free key, no credit card: "
              "https://aistudio.google.com/apikey")
        return 1

    examples, skipped_unverified = _load_examples()

    if not examples:
        print("No verified ground-truth examples found under")
        print(f"  {GROUND_TRUTH_DIR}")
        if skipped_unverified:
            print(f"\n{skipped_unverified} draft(s) found but still marked "
                  "\"_verified\": false -- open each one, check it against "
                  "its photo, correct any wrong field, then flip _verified "
                  "to true. A draft is the MODEL's own guess, not ground "
                  "truth -- scoring it unverified would just be comparing "
                  "the model to itself.")
        else:
            print("\nThis eval has nothing to score until you add some. To "
                  "build one:")
            print("  1. Drop a real contract photo into "
                  "data/real_examples/contracts/ (gitignored -- real PII "
                  "stays local, never committed)")
            print("  2. python evaluation/label_extraction_example.py "
                  "data/real_examples/contracts/<photo>")
            print("  3. Open the generated ground-truth JSON, correct every "
                  "field against the real photo, set \"_verified\": true")
        return 1

    print("=" * 60)
    print(f"EVALUATION -- extract.extract_contract_record() (LIVE -- makes "
          f"real, billed API calls) -- {len(examples)} example(s)")
    print("=" * 60)

    results = []
    errors = []
    for path, gt in examples:
        record, err = evaluate_one(gt)
        if err:
            print(f"\n[ERROR] {gt.get('photo', path.name)}: {err}")
            errors.append({"photo": gt.get("photo", path.name), "error": err})
            continue
        results.append(record)

        print(f"\n{record['photo']}  (confidence: {record['confidence']}, "
              f"accuracy: {record['record_accuracy']:.2f})")
        for field, r in record["fields"].items():
            if not r["correct"]:
                tag = "  <-- escalation-relevant" if field in ESCALATION_RELEVANT_FIELDS else ""
                print(f"  [WRONG] {field}: expected {r['gt']!r}, "
                      f"got {r['extracted']!r}{tag}")
        m = record["menu"]
        if m["missing"]:
            print(f"  [MISSED menu items]: {m['missing']}")
        if m["extra"]:
            print(f"  [HALLUCINATED menu items]: {m['extra']}")
        if m["matched"] and m["qty_correct"] < m["matched"]:
            print(f"  [qty_unit wrong on matched items]: "
                  f"{m['qty_correct']}/{m['matched']} correct")

    if not results:
        print("\nEvery example failed extraction outright -- see errors above.")
        return 1

    print("\n" + "=" * 60)
    print("AGGREGATE")
    print("=" * 60)

    for field in SCALAR_FIELDS:
        checked = [r["fields"][field]["correct"] for r in results]
        acc = sum(checked) / len(checked)
        tag = " (escalation-relevant)" if field in ESCALATION_RELEVANT_FIELDS else ""
        print(f"  {field}{tag}: {sum(checked)}/{len(checked)} ({acc:.2f})")

    esc_checked = [r["fields"][f]["correct"] for r in results for f in ESCALATION_RELEVANT_FIELDS]
    esc_acc = sum(esc_checked) / len(esc_checked) if esc_checked else float("nan")
    print(f"\nEscalation-relevant field accuracy (combined): "
          f"{sum(esc_checked)}/{len(esc_checked)} ({esc_acc:.2f})")

    total_gt_items = sum(r["menu"]["gt_count"] for r in results)
    total_matched = sum(r["menu"]["matched"] for r in results)
    total_ext_items = sum(r["menu"]["ext_count"] for r in results)
    total_qty_correct = sum(r["menu"]["qty_correct"] for r in results)
    menu_recall = total_matched / total_gt_items if total_gt_items else float("nan")
    menu_precision = total_matched / total_ext_items if total_ext_items else float("nan")
    qty_acc = total_qty_correct / total_matched if total_matched else float("nan")
    print(f"\nMenu item recall (found real items / all real items):    "
          f"{total_matched}/{total_gt_items} ({menu_recall:.2f})")
    print(f"Menu item precision (real items / all items returned):   "
          f"{total_matched}/{total_ext_items} ({menu_precision:.2f})")
    print(f"qty_unit accuracy on matched items:                      "
          f"{total_qty_correct}/{total_matched} ({qty_acc:.2f})")

    print("\nConfidence calibration (mean record accuracy per self-reported "
          "_confidence bucket):")
    for bucket in ("high", "medium", "low", "unknown"):
        bucket_records = [r for r in results if r["confidence"] == bucket]
        if not bucket_records:
            continue
        mean_acc = sum(r["record_accuracy"] for r in bucket_records) / len(bucket_records)
        print(f"  {bucket}: n={len(bucket_records)}, mean accuracy={mean_acc:.2f}")
    print("If \"low\"/\"medium\" buckets don't show a lower mean accuracy "
          "than \"high\", the model's self-reported confidence isn't "
          "actually tracking its own error rate -- worth knowing before "
          "trusting it anywhere in the pipeline (e.g. the low-confidence "
          "-> auto-escalate promotion in contract_agent.evaluate_changes()).")

    if errors:
        print(f"\n{len(errors)} example(s) failed extraction outright: {errors}")

    return 0 if not errors and all(r["record_accuracy"] == 1.0 for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
