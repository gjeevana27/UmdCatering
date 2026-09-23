"""
Speeds up building ground-truth examples for evaluate_extraction.py.

Hand-typing every field of a contract from scratch is slow. Instead, this
runs extract_contract_record() on a real photo ONCE and writes its own
guess as a starting-point JSON file under
data/real_examples/contracts/ground_truth/ -- you then open that file
next to the photo, fix whatever's wrong (menu_items especially: add
anything the model missed, remove anything it hallucinated), and flip
"_verified" to true.

The "_verified": false default is load-bearing, not decoration:
evaluate_extraction.py refuses to score any ground-truth file still
flagged false, specifically so an un-checked draft can never silently
count as verified truth and make the score look better than it is --
that would just be comparing the model's extraction to its own earlier
guess, which is exactly the "written to pass" trap this eval exists to
avoid.

Usage:
    export GEMINI_API_KEY=...
    python evaluation/label_extraction_example.py data/real_examples/contracts/some_photo.jpg
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import extract  # noqa: E402
import llm_client  # noqa: E402

GROUND_TRUTH_DIR = (Path(__file__).resolve().parent.parent / "data" /
                     "real_examples" / "contracts" / "ground_truth")


def main():
    if len(sys.argv) != 2:
        print("Usage: python evaluation/label_extraction_example.py path/to/photo.jpg")
        return 1

    if not llm_client.is_llm_available():
        print("GEMINI_API_KEY is not set -- extraction has no rule-based "
              "fallback for reading a photo.")
        print("export GEMINI_API_KEY=...   # free key, no credit card: "
              "https://aistudio.google.com/apikey")
        return 1

    photo_path = Path(sys.argv[1])
    if not photo_path.exists():
        print(f"No such file: {photo_path}")
        return 1

    print(f"Running extract_contract_record() on {photo_path.name} ...")
    try:
        result = extract.extract_contract_record(str(photo_path))
    except extract.ExtractionError as e:
        print(f"Extraction failed: {e}")
        return 1

    template = {
        "_verified": False,
        "_instructions": (
            f"This is the MODEL's own guess, not verified ground truth. Open "
            f"{photo_path.name}, check every field below against the real "
            f"document, and correct anything wrong -- menu_items especially: "
            f"add any item the model missed, remove any it hallucinated. Set "
            f"_verified to true only once every field is confirmed correct."
        ),
        "photo": photo_path.name,
        "event_id": result.get("event_id", ""),
        "event_date": result.get("event_date", ""),
        "event_time": result.get("event_time", ""),
        "location": result.get("location", ""),
        "event_type": result.get("event_type", ""),
        "guest_count": result.get("guest_count", 0),
        "division": result.get("division", ""),
        "menu_items": [
            {
                "qty_unit": item.get("qty_unit", ""),
                "recipe_name": item.get("recipe_name", ""),
                "description": item.get("description", ""),
            }
            for item in result.get("menu_items", [])
        ],
    }

    GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GROUND_TRUTH_DIR / f"{photo_path.stem}.json"
    if out_path.exists():
        print(f"\n{out_path} already exists -- not overwriting. Delete it "
              f"first if you want a fresh draft.")
        return 1

    out_path.write_text(json.dumps(template, indent=2, ensure_ascii=False),
                         encoding="utf-8")

    print(f"\nWrote draft ground truth to {out_path}")
    print(f"Extraction's self-reported confidence: {result.get('_confidence')} "
          f"({result.get('_confidence_notes', '')})")
    print("\nNext: open that file next to the photo, correct every field, "
          "then set \"_verified\": true.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
