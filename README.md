# Event Compliance Agent

An autonomous decision agent for catering-operations compliance: it
perceives contracts (photo/PDF), remembers prior versions, reasons about
what changed, and acts — escalating or flagging each finding for review
on its own, calling an LLM only where its own rules can't resolve
ambiguity, and logging why behind every decision.

**Full rationale, architecture, and known limitations:**
[docs/DESIGN.md](docs/DESIGN.md).

## Built With

Python · Streamlit · Google Gemini API (`gemini-3.5-flash-lite`) ·
Google Cloud Firestore · python-dateutil

## Agent Capabilities

| Capability | Implementation |
|---|---|
| **Perception** | `extract.py` — Gemini vision turns a photographed/scanned contract into structured state; `classify_document()` decides what KIND of file something is before that even runs |
| **Memory** | Firestore — full per-event change history, plus a per-dish allergen reference that accumulates across every scan |
| **Reasoning** | `contract_agent.py` — rule engine + Gemini judgment call for genuinely ambiguous cases |
| **Action** | autonomous escalate / review decision on every change (deliberately no third, silent "auto-clear" tier — nothing is ever dismissed without a human seeing it), notification generation, no human step required to trigger it |
| **Guardrails** | daily call-rate circuit breaker, output/schema validation, extraction confidence promotes every "review" to "escalate" when the upstream read wasn't trusted |
| **Explainability** | every decision is logged with the reasoning that produced it — no black-box output |

## Features

- **Division-isolated storage** — Good Tidings and Goodies To Go never
  share a collection, lookup, or comparison.
- **Matched by (Event ID, Event Date)**, not Event ID alone — a reschedule
  and a different event reusing the same number are never silently
  conflated; ambiguous cases are surfaced for an explicit human decision.
- **Batch upload**, reconciled by each document's own print timestamp when
  two files land on the same event.
- **Two-tier decision layer** — every change is Act-on-this or
  Worth-a-glance, never silently logged away.
- **Notifications tab** — day-grouped, checkbox-reviewed, unresolved items
  carried forward so nothing is missed by checking only the latest one.
- **Allergen Scan tab** — Gemini-inferred ingredients (labeled as
  inference, never as verified), confirmed findings persisted to a
  growing per-dish allergen reference.
- **Guardrails** — PII cleanup, a daily API call-count circuit breaker,
  and output validation on every extraction (see below).
- **Intake agent** — watches a local folder, classifies each file (is
  this actually a contract?), and routes it through the same pipeline
  as a manual upload — no browser, no clicking Upload (see below).
- **3 automated eval harnesses** covering the decision layer, the LLM
  judgment layer (live), and extraction accuracy (live) (see Evaluation).

## Architecture

```
                    Streamlit UI (app.py)
   Good Tidings │ Goodies To Go │ Notifications │ Allergen Scan
                            │
                            │  upload photo/PDF
                            ▼
              extract.py  (Gemini vision → JSON)
                 returns structured record + _confidence
                            │
                            ▼
        _validate_extracted_dict()   [guardrail: type/range checks]
                            │
                            ▼
     contract_store.py — lookup by (event_id, event_date)
        ├─ exact match         → diff_records() → overwrite record
        ├─ same id, new date   → ask human: link reschedule / keep separate
        └─ no match            → store as new baseline
                            │
                            ▼
     contract_agent.py — evaluate_changes()
        ├─ rule-decided   (location, event_time, guest_count Δ,
        │                  menu item added/removed/qty-changed,
        │                  field blanked-out ⇒ likely extraction miss)
        └─ ambiguous text → llm_client.judge_contract_change()  (Gemini)
                            │
                            ▼
          Firestore — per-division change history + notifications
                            │
                            ▼
          Notifications tab — grouped by day, checkbox-reviewed,
          unresolved items carried into the next notification
```

## Intake Agent (`src/intake_agent.py`)

Watches one local folder for contract files (`INTAKE_FOLDER_PATH`,
default `intake/`) and auto-routes them, so getting a contract into the
system becomes "save the file here" instead of "open the app and click
Upload." One shared inbox, not one per division — each document's own
header/footer already declares its division (the same field the manual
upload flow reads), so a mixed daily batch gets routed to the right
Firestore collection per file automatically.

```
   intake/ (watched)
        │  new file lands
        ▼
   classify_document()  (Gemini — cheap, runs BEFORE the full extraction)
        │
        ├─ not a contract → intake/needs_review/ + a .txt note explaining why
        │
        ▼ "contract"
   contract_store.extract_and_classify() → contract_store.diff_and_store()
   (the EXACT SAME pipeline app.py's manual upload uses — no second,
    possibly-drifting copy of the decision logic)
        │
        ├─ ambiguous (division unreadable, reschedule ambiguity,
        │  no event ID, extraction failed) → intake/needs_review/ + a note.
        │  Never resolved automatically — left for a human via the normal
        │  Streamlit upload/confirmation flow, same as any other upload.
        │
        ▼ resolved
   Firestore write (permanent from this point — independent of what
   happens to the source file afterward) + notification if anything
   changed → intake/processed/
```

Deliberately scoped to the input end only: this agent decides *what kind
of file something is* and *whether it's ready to route*, and never
touches extraction values or `contract_agent.py`'s escalate/review
decisions. That boundary is what makes it worth calling agentic — real,
open-ended classification of unknown input — without weakening the
deterministic, auditable decision core everywhere else in this project.

Run it (separate from the Streamlit app, keeps running in its own
terminal):
```bash
export GEMINI_API_KEY=...
export FIRESTORE_CREDENTIALS_PATH=...
python src/intake_agent.py
```
Stop with Ctrl+C. Logs to both the console and `intake/intake_agent.log`.

## Decision rules (`contract_agent.py`)

| Change | Decision | Rule |
|---|---|---|
| Field had content → now blank | **Escalate** | checked first, ahead of every other rule — treated as a likely extraction miss |
| `location` / `event_time` changed | **Escalate** | always-escalate fields, any magnitude |
| `guest_count` Δ ≥ 15% | **Escalate** | `SIGNIFICANT_QUANTITY_CHANGE` threshold |
| `guest_count` Δ < 15% | Review | shown, not urgent |
| Menu item added / removed | **Escalate** | unconditional |
| Menu item qty/unit changed | **Escalate** | unconditional |
| Menu description reworded | LLM judgment | ambiguous free text |
| `event_type` reworded | LLM judgment | ambiguous free text |
| Any "review" item, but extraction confidence was low | **Escalate** | agent doesn't fully trust its own upstream reading |

## Guardrails

| Guardrail | Mechanism | Where |
|---|---|---|
| PII cleanup | every uploaded document deleted from temp disk in a `finally` block, success or failure | `_temp_upload_file()`, 2 upload call sites in `app.py` |
| Cost circuit breaker | thread-safe in-process daily call counter, checked before every Gemini attempt including retries | `rate_guard.py`, default 300/day, `GEMINI_DAILY_CALL_LIMIT` to override |
| Output validation | every extraction's int/list/confidence fields checked; bad type raises, negative count auto-corrects with confidence downgraded to `low` | `extract._validate_extracted_dict()`, all extraction entry points |

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| App framework | Streamlit | `app.py` |
| LLM | Gemini API (`gemini-3.5-flash-lite`) | extraction (vision), ambiguous-case judgment |
| Storage | Google Cloud Firestore | per-division records, notifications, dish-allergen reference |
| Reasoning / decision engine | `contract_agent.py` | autonomous escalate / review on every change (no auto-clear tier), Gemini consulted for ambiguous judgment calls |
| Agent memory | Firestore change history + persistent allergen reference | carries context across runs rather than treating each upload as stateless |
| Rate/cost control | `rate_guard.py` | shared daily call-count circuit breaker |
| Intake / folder watching | `watchdog` | `intake_agent.py` — live filesystem events for the intake folder |

## Evaluation

Three separate harnesses, one per pipeline stage — deliberately kept
separate rather than one blended score, since they test different things
and two of them cost money to run. The deterministic harness runs
automatically on every push via [GitHub Actions](.github/workflows/evals.yml).

| Harness | Tests | Cases | Metric | Score |
|---|---|---|---|---|
| `evaluation/evaluate_contract_agent.py` | `contract_agent.py` decision rules | 60 synthetic scenarios, incl. boundary/adversarial cases | Decision accuracy | **1.00 (60/60)** |
| `evaluation/evaluate_llm_judgment.py` | `llm_client.judge_contract_change()` — **live**, real API calls | 36 scenarios | Decision-category accuracy | **0.97 (35/36)** |
| `evaluation/evaluate_extraction.py` | `extract.extract_contract_record()` vs. real photos — **live** | 0 so far (harness ready, needs real examples — see below) | Field-level accuracy + confidence calibration | *n/a yet* |

```bash
python evaluation/evaluate_contract_agent.py     # no API key needed, deterministic

export GEMINI_API_KEY=...
python evaluation/evaluate_llm_judgment.py       # live, opt-in, ~$0.01/run
python evaluation/evaluate_extraction.py         # live, opt-in, needs ground truth below
```

The 60-case `contract_agent.py` set and the 36-case live LLM set were
both scaled up specifically to include boundary and adversarial cases
(an exact-threshold guest count, an OCR-garbled number, cosmetic-only
menu reformatting) rather than only the obvious example of each rule —
the deterministic set still lands at 1.00 (it's genuinely deterministic
code, so that's expected), but the live LLM eval landed at **35/36
(0.97)**, not a padded 1.00: the model escalated a "Corporate Meeting" →
"Corporate Meeting - AV Needed" change that was labeled `review`
(reasoning: AV setup isn't the caterer's own operational concern). Left
in and reported as-is rather than dropped or relabeled after the fact —
a real, disclosed disagreement is worth more than a clean-looking score.

**Extraction accuracy** (`evaluate_extraction.py`) scores
`extract.extract_contract_record()` field-by-field against real,
hand-labeled contract photos — the one step every other harness assumes
is already correct. It has nothing to score yet because building ground
truth needs real documents this repo doesn't ship with (gitignored, real
client PII). To add an example:
```bash
export GEMINI_API_KEY=...
python evaluation/label_extraction_example.py data/real_examples/contracts/some_photo.jpg
```
which drops a draft ground-truth JSON pre-filled with the model's own
guess — open it next to the photo, correct every field, set
`"_verified": true`. See
[data/real_examples/README.md](data/real_examples/README.md).

**Not yet covered by an automated eval:** the standalone allergen
scanner. See [docs/DESIGN.md#limitations](docs/DESIGN.md#limitations).

## Project Structure

```
event-compliance-agent/
├── app.py                       Good-to-Go -- divisions / notifications / allergen scan
├── .streamlit/
│   ├── config.toml              theme (tracked -- no secrets in it)
│   └── secrets.toml.example     template for Firestore/Gemini secrets on Streamlit Cloud
├── src/
│   ├── contract_store.py        Firestore storage/lookup/diff for Good-to-Go
│   ├── contract_agent.py        decision layer for contract changes (see rule table above)
│   ├── allergen_reference.py    9-category allergen map, direct + hidden-carrier terms
│   ├── llm_client.py            Gemini call for ambiguous cases only
│   ├── extract.py               photo/PDF -> structured JSON via Gemini vision
│   ├── allergen_scan.py         standalone one-dish allergen scanner (text = free/local)
│   ├── rate_guard.py            shared daily Gemini call-count circuit breaker
│   └── intake_agent.py          watches a local folder, classifies + auto-routes contracts
├── intake/                      gitignored -- watched folder, real customer documents (local only)
│   ├── processed/                moved here after successful routing
│   └── needs_review/             moved here + a .txt note, needs a human
├── data/real_examples/          gitignored -- real photos + hand-labeled ground truth (local only)
│   └── contracts/ground_truth/  ground truth for evaluate_extraction.py
├── evaluation/
│   ├── labeled_contract_changes.json / evaluate_contract_agent.py  contract_agent.py eval
│   ├── labeled_llm_judgments.json / evaluate_llm_judgment.py    llm_client.py eval (live)
│   ├── evaluate_extraction.py                                   extract.py eval (live, needs ground truth)
│   └── label_extraction_example.py                              helper: drafts ground truth from a real photo
├── docs/DESIGN.md               full rationale, architecture, design decisions, limitations
└── requirements.txt
```

## Getting Started

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...             # required -- extraction has no rule-based fallback
export FIRESTORE_CREDENTIALS_PATH=... # required -- path to a service-account JSON file
streamlit run app.py
```

Opens at `http://localhost:8501`. Four tabs: Good Tidings, Goodies To Go,
Notifications, Allergen Scan.

### CLI

```bash
# Standalone allergen scan (text mode needs no key):
python src/allergen_scan.py "chicken thigh, satay sauce, lime, caesar dressing"
python src/allergen_scan.py --photo path/to/dish.jpg   # needs GEMINI_API_KEY
```

Free `GEMINI_API_KEY` (no credit card):
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Gemini API key |
| `FIRESTORE_CREDENTIALS_PATH` | Yes | path to a Firestore service-account JSON file |
| `APP_PASSCODE` | No | gates access if deploying publicly |
| `GEMINI_DAILY_CALL_LIMIT` | No | overrides the 300/day circuit breaker in `rate_guard.py` |
| `INTAKE_FOLDER_PATH` | No | overrides `intake_agent.py`'s watched folder (default: `intake/` under the repo) |

## Deploying

`app.py` is a plain Streamlit app — Streamlit Community Cloud or Hugging
Face Spaces both work free. It additionally needs the Firestore
service-account JSON pasted as a `FIRESTORE_SERVICE_ACCOUNT_JSON` secret
(see `.streamlit/secrets.toml.example`).

A public deployment means anyone with the URL can trigger calls against
your key (and read/write your Firestore data without `APP_PASSCODE` set)
— keep keys out of a public deployment's secrets unless actively
demoing, or take it down between demos.

## License

For educational and portfolio demonstration purposes.
