# Event Compliance Agent

Two Streamlit apps for real catering-operations review work, both built
around the same principle: deterministic rules handle everything they
can, and an LLM (Gemini, free tier) is only ever asked to weigh in where
rule-based logic genuinely can't resolve ambiguity — never as the first
or only line of defense on a safety-relevant finding.

**Full design rationale, architecture diagram, feature-by-feature detail,
and known limitations:** see [docs/DESIGN.md](docs/DESIGN.md). This
README covers what each app does at a glance and how to run it.

## `app.py` — Contract Version Tracker (current, primary app)

Upload a contract (photo/PDF), and it's compared against whatever version
is already on file for that same event — every field, every menu line
item, tracked and reviewable over time. Built for one specific real-world
problem: a contract changes after someone's already acted on the old
version, and nobody catches it.

- **Good Tidings / Goodies To Go** kept in completely separate storage.
- **Matched by (Event ID, Event Date)** — a genuine reschedule and a
  different event reusing the same number are never silently conflated;
  you decide which one it is whenever that's ambiguous.
- **Batch upload** — a whole morning's contracts at once, reconciled by
  each document's own print timestamp if two land on the same event.
- **Notifications tab** — every change, grouped by day, checkbox-reviewed,
  with unresolved items carried forward so nothing gets missed by only
  checking the latest one.
- **Allergen Scan tab** — a quick per-dish or per-packing-list allergen
  check, with Gemini-inferred ingredients (clearly labeled, never treated
  as verified) and a persistent per-dish allergen reference that grows as
  you confirm findings.

Run it: see "Running the Contract Version Tracker" below. Full detail on
every feature above: [docs/DESIGN.md](docs/DESIGN.md#contract-version-tracker-apppy).

## `app_compliance_agent_full.py` — the original, broader compliance agent

Reviews a production sheet against its signed contract and flags what a
human needs to act on before the event — menu mismatches, guest-count
changes, and allergen conflicts, including "hidden" allergens that don't
show up in an ingredient's name (satay sauce carrying tree nuts, Caesar
dressing carrying egg and anchovy). Five tabs: sample events, your own
contract/production-sheet JSON, a photo/PDF, a contract-version diff, and
a standalone allergen scanner — plus an optional live FDA recall check.

**Live demo (no setup needed):** see `demo/` — a self-contained,
rule-based walkthrough on three sample events.

Uses Google's **Gemini API** (`gemini-3.5-flash-lite`) for the LLM-assisted
pieces in both apps — free tier, no credit card, comfortably covers normal
use at zero cost.

## Evaluation

```bash
python evaluation/evaluate.py
```

Scored against a small, hand-labeled set of 3 sample events (see
`evaluation/labeled_cases.json`):

| Metric | Score |
|---|---|
| Escalation precision | 1.00 |
| Escalation recall | 1.00 |
| Allergen-conflict recall (safety-critical subset) | 1.00 |

Allergen-conflict recall is reported separately from overall recall on
purpose — a missed allergen escalation and a missed menu-count mismatch
aren't the same failure, and averaging them would let a safety-critical
miss hide behind an otherwise decent score. (This eval set is small —
see [Limitations](docs/DESIGN.md#limitations).)

## Project structure

```
event-compliance-agent/
├── app.py                       Contract Version Tracker -- divisions / notifications / allergen scan
├── app_compliance_agent_full.py the original, broader compliance agent (5-tab Streamlit app)
├── .streamlit/
│   ├── config.toml              theme (tracked -- no secrets in it)
│   └── secrets.toml.example     template for Firestore/Gemini secrets on Streamlit Cloud
├── src/
│   ├── contract_store.py        Firestore storage/lookup/diff for the Contract Version Tracker
│   │                             (event_id+date matching, notifications, dish-allergen notes)
│   ├── contract_agent.py        decision layer for contract changes (escalate/review, whitespace-
│   │                             normalized diffing, blank-field-regression rule)
│   ├── allergen_reference.py    9-category allergen map, direct + hidden-carrier terms
│   ├── parser.py                loads contract.json / production_sheet.json
│   │                             + builds objects from extracted (photo/PDF) data
│   │                             + PullSheet schema for kitchen-board checklists
│   ├── discrepancy_engine.py    deterministic fact-finding (no LLM)
│   ├── llm_client.py            optional Gemini call for ambiguous cases only
│   ├── extract.py               photo/PDF -> structured JSON via Gemini vision
│   │                             (contract / production sheet / pull sheet / single-dish /
│   │                             packing-list-for-allergens schemas)
│   ├── allergen_scan.py         standalone one-dish allergen scanner (text = free/local)
│   ├── recall_checker.py        live openFDA recall lookup (real-time, opt-in)
│   ├── contract_diff.py         diffs two contract versions, flags what changed
│   ├── pull_sheet_check.py      fuzzy contract-vs-pull-sheet coverage check
│   └── agent.py                 decision layer + audit trail + report
├── data/sample_events/          4 sample events (995, 1002, 1140, 2201 — contract-change demo)
├── evaluation/
│   ├── labeled_cases.json       hand-labeled ground truth
│   └── evaluate.py              precision/recall/safety-recall scorer
├── demo/                        self-contained interactive walkthrough (static HTML)
├── docs/DESIGN.md               full rationale, architecture, design decisions, limitations
└── requirements.txt
```

## Running it

```bash
git clone <this-repo>
cd event-compliance-agent
pip install -r requirements.txt

# Rule-based only (no API key needed):
python src/agent.py data/sample_events/event_995

# With LLM-assisted ambiguous-case review (free Gemini key -- see below):
export GEMINI_API_KEY=...
python src/agent.py data/sample_events/event_995

# Evaluate against labeled ground truth:
python evaluation/evaluate.py

# Live recall check from the CLI:
python src/agent.py data/sample_events/event_995 --live-recalls

# Contract-change detection from the CLI:
python src/agent.py data/sample_events/event_2201 --compare-contract data/sample_events/event_2201/contract_v1.json

# Standalone allergen scan (text mode needs no key at all):
python src/allergen_scan.py "chicken thigh, satay sauce, lime, caesar dressing"
python src/allergen_scan.py --photo path/to/dish.jpg   # needs GEMINI_API_KEY
```

Get a free `GEMINI_API_KEY` (no credit card) at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).

## Running the Contract Version Tracker (`app.py`)

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...             # required -- extraction has no rule-based fallback
export FIRESTORE_CREDENTIALS_PATH=... # required -- path to a service-account JSON file
# export APP_PASSCODE=...             # optional -- gates access if deploying publicly
streamlit run app.py
```

Opens at `http://localhost:8501`. Four tabs: **Good Tidings** and
**Goodies To Go** (each with its own upload + batch processing), the
**Notifications** review queue, and **Allergen Scan** (the standalone
dish/packing-list checker). See "Deploying" below for Firestore setup on
Streamlit Cloud/Hugging Face Spaces.

## Running the full compliance agent (`app_compliance_agent_full.py`)

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...   # optional: enables photo/PDF extraction, the
                              # photo mode of the allergen scanner, and
                              # LLM-assisted ambiguous-case review
streamlit run app_compliance_agent_full.py
```

Opens at `http://localhost:8501`. Five tabs: sample events, your own JSON,
photo/PDF upload, contract-version comparison, and the standalone allergen
scanner. The live recall check is a toggle in the sidebar — off by default,
since it makes a real network call to api.fda.gov.

## Deploying

Both web apps are normal Streamlit apps, so any of these work (all free):

- **Streamlit Community Cloud** — push this repo to GitHub, go to
  [share.streamlit.io](https://share.streamlit.io), "New app," point it at
  this repo. For `app_compliance_agent_full.py`, add `GEMINI_API_KEY`
  under the app's Secrets. For **`app.py`** (the Contract Version
  Tracker), it also needs Firestore credentials — paste the
  service-account JSON's full contents as a secret named
  `FIRESTORE_SERVICE_ACCOUNT_JSON` (see `.streamlit/secrets.toml.example`
  for the exact format), plus `GEMINI_API_KEY` and, optionally,
  `APP_PASSCODE` if the deployment will be public.
- **Hugging Face Spaces** — new Space, SDK: Streamlit, push this repo (or
  connect the GitHub repo). Same secrets as above, added as Space secrets.

A note on cost/safety when deploying publicly: Gemini's free tier is
generous for personal use, but a public URL means anyone who finds it can
trigger calls against your key (and, for `app.py`, read/write your
Firestore data if `APP_PASSCODE` isn't set). Keep keys out of a public
deployment's secrets if you're not actively demoing it, set a passcode,
or take the deployment down between demos, rather than leaving an
open instance running indefinitely.

The static demo (`demo/index.html`) is separate from the web app — it needs
no Python backend at all, so it can go on **GitHub Pages** directly: push
this repo, then in the repo's Settings → Pages, set the source to the
`demo/` folder on your main branch.
