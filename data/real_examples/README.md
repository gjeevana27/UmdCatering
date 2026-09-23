# Real examples (private — not committed to the public repo)

Drop real photos here as you take them on the job:
- `contracts/` — Menu Packing List photos
- `pull_sheets/` — kitchen-board / "Secure X" checklist photos

This whole folder is in `.gitignore` (see project root) so it never gets
pushed to GitHub — these photos have real client names, phone numbers, and
addresses in them, and that stays local to your machine only.

## What this is actually for

1. **Validating and tightening the extraction prompts.** Run a real photo
   through `extract.py` (or the web app's "Upload photo/PDF" tab), see
   where it misreads something, and tell me — I'll adjust the prompt in
   `CONTRACT_SCHEMA_PROMPT` / `PULL_SHEET_SCHEMA_PROMPT` to handle it. This
   is the validation pass the current prompts still need (see README
   Limitations) — they were written by reading your example photos
   carefully, but never run against a live model.
2. **Eventually building a real eval set.** The current
   `evaluation/labeled_cases.json` is 3 hand-labeled synthetic events —
   honest, but small. A handful of real (anonymized) contract/production-
   sheet pairs with known outcomes ("this discrepancy was real, this one
   wasn't") would make the precision/recall numbers mean a lot more.
3. **Extraction accuracy ground truth** — `evaluation/evaluate_extraction.py`
   scores `extract.extract_contract_record()`'s field-by-field accuracy
   against real photos, which no other eval in this repo tests. Build an
   example:
   ```bash
   export GEMINI_API_KEY=...
   python evaluation/label_extraction_example.py data/real_examples/contracts/some_photo.jpg
   ```
   This writes a draft ground-truth JSON under `contracts/ground_truth/`,
   pre-filled with the model's own guess so you're correcting rather than
   typing from scratch. Open it next to the photo, fix every field that's
   wrong (menu items especially — add anything missed, remove anything
   hallucinated), then set `"_verified": true`. The eval refuses to score
   any file still left `false`, so an unchecked draft can never silently
   count as ground truth and inflate the score. `ground_truth/` is
   gitignored the same as the photos — these values (event_id, location,
   guest counts) are still real client data typed out in plain text.

## If you ever want to include a real example in the actual public repo

Don't commit a real photo directly. Instead:
- Redact client name, phone, address, and email before saving a copy
  anywhere public (crop/blur, or just re-type the non-identifying parts
  into a synthetic JSON like the existing `data/sample_events/` ones).
- Or ask me to help turn one real example into an anonymized synthetic
  one — same structure, same discrepancies, fictional client info.
