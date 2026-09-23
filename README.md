# Event Compliance Agent

Catering-operations review tooling: contract-version tracking with automatic
change detection, a per-event allergen scanner, and a deterministic-first
escalate/review decision layer that only calls an LLM where rules genuinely
can't resolve ambiguity.

**Full rationale, architecture, and known limitations:**
[docs/DESIGN.md](docs/DESIGN.md).

## Built With

Python · Streamlit · Google Gemini API (`gemini-3.5-flash-lite`) ·
Google Cloud Firestore · python-dateutil

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
- **3 automated eval harnesses**, one deterministic per decision layer plus
  a live LLM-judgment eval (see Evaluation).

The repo also carries an earlier, broader prototype
(`app_compliance_agent_full.py` / `agent.py`) that this app grew out of —
parked, not actively developed, but left in and still runnable since it
covers allergen/contract-vs-production-sheet checks `app.py` doesn't.

## Architecture — Contract Version Tracker (`app.py`)

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

## Legacy agent's severity ladder (`agent.py` / `app_compliance_agent_full.py`)

| Finding | Decision |
|---|---|
| High-severity allergen conflict OR recall exposure | **Always escalate** — hard rule, no override (`NEVER_AUTO_CLEAR`) |
| Other high severity | Escalate by rule |
| Low severity, non-allergen | Auto-clear by rule |
| Medium severity (ambiguous) | LLM judgment if configured, else routed to human review |

Full pipeline diagram (extract → parse → `discrepancy_engine.py` →
optional live recall check → `agent.py::decide()`):
[docs/DESIGN.md#architecture](docs/DESIGN.md#architecture).

## Guardrails

| Guardrail | Mechanism | Where |
|---|---|---|
| PII cleanup | every uploaded document deleted from temp disk in a `finally` block, success or failure | `_temp_upload_file()`, 5 upload call sites across `app.py` and the legacy script |
| Cost circuit breaker | thread-safe in-process daily call counter, checked before every Gemini attempt including retries | `rate_guard.py`, default 300/day, `GEMINI_DAILY_CALL_LIMIT` to override |
| Output validation | every extraction's int/list/confidence fields checked; bad type raises, negative count auto-corrects with confidence downgraded to `low` | `extract._validate_extracted_dict()`, all 6 extraction entry points |

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| App framework | Streamlit | `app.py` (primary); legacy script also runnable |
| LLM | Gemini API (`gemini-3.5-flash-lite`) | extraction (vision), ambiguous-case judgment |
| Storage | Google Cloud Firestore | per-division records, notifications, dish-allergen reference |
| Decision layer | pure Python (`contract_agent.py` / `agent.py`) | deterministic rules, no LLM in the fact-finding path |
| Rate/cost control | `rate_guard.py` | shared daily call-count circuit breaker |
| Live data (legacy app only) | openFDA Food Enforcement API | recall exposure check, opt-in |

## Evaluation

Three separate harnesses, one per decision layer — deliberately kept
separate rather than one blended score, since they test different things
and one of them costs money to run.

| Harness | Tests | Cases | Metric | Score |
|---|---|---|---|---|
| `evaluation/evaluate.py` | `agent.py` (legacy pipeline) | 3 real sample events | Escalation precision | **1.00** |
| | | | Escalation recall | **1.00** |
| | | | Allergen-conflict recall (safety-critical subset) | **1.00** |
| `evaluation/evaluate_contract_agent.py` | `contract_agent.py` decision rules | 8 synthetic scenarios | Decision accuracy | **1.00 (8/8)** |
| `evaluation/evaluate_llm_judgment.py` | `llm_client.judge_contract_change()` — **live**, real API calls | 8 scenarios | Decision-category accuracy | **1.00 (8/8)** |

```bash
python evaluation/evaluate.py
python evaluation/evaluate_contract_agent.py     # no API key needed, deterministic

export GEMINI_API_KEY=...
python evaluation/evaluate_llm_judgment.py       # live, opt-in, ~$0.001/run
```

Allergen-conflict recall is scored separately from overall recall on
purpose — a missed allergen escalation and a missed menu-count mismatch
are different failure classes; averaging them would hide a safety-critical
miss behind a decent blended number.

**Not yet covered by an automated eval:** `agent.py`'s own
`judge_ambiguous_finding()`, extraction accuracy itself, `contract_diff.py`,
`pull_sheet_check.py`, the standalone allergen scanners. See
[docs/DESIGN.md#limitations](docs/DESIGN.md#limitations).

## Project Structure

```
event-compliance-agent/
├── app.py                       Contract Version Tracker -- divisions / notifications / allergen scan
├── app_compliance_agent_full.py the original, broader compliance agent (5-tab Streamlit app)
├── .streamlit/
│   ├── config.toml              theme (tracked -- no secrets in it)
│   └── secrets.toml.example     template for Firestore/Gemini secrets on Streamlit Cloud
├── src/
│   ├── contract_store.py        Firestore storage/lookup/diff for the Contract Version Tracker
│   ├── contract_agent.py        decision layer for contract changes (see rule table above)
│   ├── allergen_reference.py    9-category allergen map, direct + hidden-carrier terms
│   ├── parser.py                loads contract.json / production_sheet.json
│   ├── discrepancy_engine.py    deterministic fact-finding (no LLM)
│   ├── llm_client.py            Gemini call for ambiguous cases only
│   ├── extract.py               photo/PDF -> structured JSON via Gemini vision
│   ├── allergen_scan.py         standalone one-dish allergen scanner (text = free/local)
│   ├── recall_checker.py        live openFDA recall lookup (legacy app only, opt-in)
│   ├── contract_diff.py         diffs two contract versions (legacy app)
│   ├── pull_sheet_check.py      fuzzy contract-vs-pull-sheet coverage check (legacy app)
│   ├── rate_guard.py            shared daily Gemini call-count circuit breaker
│   └── agent.py                 legacy decision layer + audit trail + report
├── data/sample_events/          4 sample events (contract-change demo)
├── evaluation/
│   ├── labeled_cases.json / evaluate.py                         agent.py eval
│   ├── labeled_contract_changes.json / evaluate_contract_agent.py  contract_agent.py eval
│   └── labeled_llm_judgments.json / evaluate_llm_judgment.py    llm_client.py eval (live)
├── demo/                        self-contained interactive walkthrough (static HTML)
├── docs/DESIGN.md               full rationale, architecture, design decisions, limitations
└── requirements.txt
```

## Getting Started

### Contract Version Tracker (`app.py`)

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...             # required -- extraction has no rule-based fallback
export FIRESTORE_CREDENTIALS_PATH=... # required -- path to a service-account JSON file
streamlit run app.py
```

Opens at `http://localhost:8501`. Four tabs: Good Tidings, Goodies To Go,
Notifications, Allergen Scan.

### Legacy compliance agent (`app_compliance_agent_full.py`)

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...   # optional -- enables photo/PDF extraction + LLM judgment
streamlit run app_compliance_agent_full.py
```

### CLI

```bash
# Rule-based only, no API key needed:
python src/agent.py data/sample_events/event_995

# With LLM-assisted ambiguous-case review:
export GEMINI_API_KEY=...
python src/agent.py data/sample_events/event_995

# Live recall check:
python src/agent.py data/sample_events/event_995 --live-recalls

# Contract-change detection:
python src/agent.py data/sample_events/event_2201 --compare-contract data/sample_events/event_2201/contract_v1.json

# Standalone allergen scan (text mode needs no key):
python src/allergen_scan.py "chicken thigh, satay sauce, lime, caesar dressing"
```

Free `GEMINI_API_KEY` (no credit card):
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes (`app.py`) / optional (legacy app, CLI) | Gemini API key |
| `FIRESTORE_CREDENTIALS_PATH` | Yes (`app.py`) | path to a Firestore service-account JSON file |
| `APP_PASSCODE` | No | gates access if deploying `app.py` publicly |
| `GEMINI_DAILY_CALL_LIMIT` | No | overrides the 300/day circuit breaker in `rate_guard.py` |

## Deploying

`app.py` is a plain Streamlit app — Streamlit Community Cloud or Hugging
Face Spaces both work free. It additionally needs the Firestore
service-account JSON pasted as a `FIRESTORE_SERVICE_ACCOUNT_JSON` secret
(see `.streamlit/secrets.toml.example`). The static demo (`demo/index.html`)
needs no backend at all and can be served directly from GitHub Pages.

A public deployment means anyone with the URL can trigger calls against
your key (and, for `app.py`, read/write your Firestore data without
`APP_PASSCODE` set) — keep keys out of a public deployment's secrets
unless actively demoing, or take it down between demos.

## License

For educational and portfolio demonstration purposes.
