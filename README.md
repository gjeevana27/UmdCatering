# Event Compliance Agent

This repo now holds two separate tools:

## `app.py` — Contract Version Tracker (current, primary app)

Upload a contract (photo/PDF), and it's compared against whatever version
is already on file for that same event — every field, every menu line
item, tracked and reviewable over time. Built for one specific real-world
problem: a contract changes after someone's already acted on the old
version, and nobody catches it.

### Matching, storage, and reschedules

- **Two completely separate divisions** — Good Tidings and Goodies To Go
  — each in its own Firestore collection. A contract from one is never
  looked up, compared, or stored against the other.
- **Matched by (Event ID, Event Date) together**, not Event ID alone —
  the same event number can legitimately recur on a different date (a
  different booking reusing an old number), and those must never
  silently overwrite or get compared against each other.
  - Exact match on file → an update to a known booking: diff every
    field, then overwrite that record.
  - Same Event ID, a *different* date on file → surfaced for an explicit
    decision: **link it** (a genuine reschedule — diffs against the
    prior date's record, then retires it, so there's one continuous
    record per event lineage) or **keep both separate** (a different
    booking that happens to reuse the number, tracked independently from
    then on). Never silently merged either way.
  - No match under any date → stored as a new baseline, nothing to
    compare yet.
- **Duplicate-upload detection.** Every stored record keeps a hash of
  its source file. Re-uploading the exact same file (byte-for-byte) is
  recognized and skipped before any comparison runs — two separate
  Gemini reads of the identical image aren't guaranteed to extract every
  field identically, so diffing a file against itself could otherwise
  produce a false "change."
- **Extracts:** Event ID, event date, event time, location (read
  specifically from the document's `Location:` field, never substituted
  with a nearby `Event Address:` field even when it looks short or
  informal — an earlier version of the prompt conflated the two), event
  type, guest count, the Time & Desc section, the document's own "Print
  Date/Time" footer stamp, and the full menu list (qty-unit, recipe
  name, and the description underneath each item).

### Batch upload

Upload a whole morning's worth of contracts at once, per division —
every file is extracted first, then stored/diffed. If two files land on
the same (Event ID, Event Date) within the *same batch*, they're
reordered by the document's own Print Date/Time footer stamp (not
upload order, which doesn't reflect which one was actually produced
first) before either touches the database, so the earlier-printed one
always becomes the baseline and the later one is diffed against it.

### The decision layer (`src/contract_agent.py`)

Every change is triaged into **Act on this** (escalate) or **Worth a
glance** (review) — deliberately two tiers only, nothing gets silently
logged away as "too minor to matter." Deterministic rules handle the
clear-cut cases (a location or delivery-time change always escalates; a
menu item added, removed, or qty/unit-changed always escalates);
genuinely ambiguous free-text changes (a reworded special-instructions
line, a menu description edit with no quantity change) go to Gemini for
a one-line urgency call.

- **A field that had real content and now reads blank always escalates
  by rule**, ahead of every other check — a genuine contract edit
  essentially never wipes a field down to nothing, so this pattern is
  treated as a likely extraction miss to verify, not a real change,
  regardless of which field it is.
- **Text comparisons are whitespace-normalized** before diffing — two
  extraction calls on identical printed text can come back with
  different line-break placement (a known Gemini non-determinism), and
  without this, that alone used to produce a false "changed" finding.
- **The LLM judgment prompt keeps old/new values structurally separate**
  from an item's name or any other context, with an explicit instruction
  to reason only from the literal text given — an earlier version that
  concatenated everything into one string let the model hallucinate a
  "change" that referenced a dish's name as if it were part of the field
  that actually changed.
- The agent also doesn't fully trust its own upstream reading: when
  extraction confidence was low, every "worth a glance" gets promoted to
  "act on this" instead.

### Notifications

A dedicated tab logging every detected change, separate from the upload
flow itself:

- Grouped by day (Eastern time), newest first; each entry shows a
  needs-attention badge and a right-aligned timestamp, and expands on
  click to show exactly what changed.
- Every notification, and every individual change within it, has its
  own checkbox — checking one marks it reviewed and it drops out of the
  feed; once every change in a notification is checked, the whole
  notification clears automatically.
- **Unresolved items carry forward.** If an older notification for the
  same event still has an unchecked item when a newer one is created,
  that item is folded into the new notification (and the old one
  retired) — so checking only the latest notification for an event never
  misses something still outstanding from an earlier one.
- The tab pill rings and shows an unread count whenever anything's
  outstanding, and goes quiet once you're caught up.

### Allergen Scan tab

A standalone quick allergen check, folded in from the original
compliance agent below and extended for real packing lists:

- Paste an ingredient list directly (fully local, zero API calls), or
  upload one or more packing-list photos/PDFs at once.
- Packing lists rarely print a real ingredients list — just a dish name
  and maybe one description line — so this uses its own extraction
  prompt (`extract_packing_list_for_allergens()`, kept separate from the
  stricter `extract_production_sheet()` the full compliance pipeline
  relies on) that asks Gemini to infer a plausible full ingredient list
  from general culinary knowledge, clearly labeled as inference, never
  presented as a verified read of the document.
- Every inferred allergen finding has a confirm checkbox; confirming (or
  typing in an allergen you already know about a dish) saves it to a
  persistent, kitchen-wide reference keyed by dish name — the next time
  that exact dish shows up in any future scan, it's flagged directly
  instead of asking again.
- Multiple uploaded documents render side by side, two events per row.

Setup and running instructions: see "Running the Contract Version
Tracker" below.

## `app_compliance_agent_full.py` — the original, broader compliance agent

Everything described in the rest of this README (allergen detection,
production-sheet-vs-contract checks, live recall checking, the standalone
allergen scanner) — kept as a separate app, set aside for now, to build on
later. All of it still works exactly as documented below; just run this
file instead of `app.py` to use it:

```bash
streamlit run app_compliance_agent_full.py
```

---

An agent that reviews an event's production sheet against its signed contract
and flags what a human needs to act on before the event happens — menu
mismatches, guest-count changes, and allergen conflicts, including "hidden"
allergens that don't show up in an ingredient's name (a satay sauce carrying
tree nuts, a Caesar dressing carrying egg and anchovy).

**Live demo (no setup needed):** see `demo/` — a self-contained, rule-based
walkthrough of the agent's reasoning on three sample events.

**Live web app:** `app_compliance_agent_full.py` — a Streamlit interface
with five ways in: sample events, your own contract/production-sheet JSON,
a **photo or PDF** of a real document (extracted via Gemini's vision, then
run through the same pipeline), a **contract-version comparison**, and a
**standalone allergen scanner** for a single dish. Also supports an
optional **live FDA recall check** against api.fda.gov.

Uses Google's **Gemini API** (not a paid Anthropic key) for the LLM-assisted
pieces, via `gemini-3.5-flash-lite` — Gemini's free tier (no credit card)
comfortably covers normal use of this project at zero cost. (`gemini-3.5-flash`
was tried first, but its free tier turned out to be capped at just 20
requests/day/project — confirmed via a live 429 quota error, not documented
anywhere — which exhausts in a single pipeline run. `gemini-2.5-flash-lite`
was tried next but is no longer available to new users.)

## The problem

This comes directly from real event-production review work: verifying
production sheets against contracts and flagging allergens across recipe
sheets by hand, event by event. Two things make it worth automating
carefully rather than casually:

1. **Allergen risk hides in ingredient composition, not ingredient names.**
   "Satay sauce" doesn't say "peanuts." "Caesar dressing" doesn't say "egg"
   or "anchovy." A reviewer who's fast and experienced catches these; a
   naive keyword scan over dish names does not.
2. **A false negative here is not like a false negative anywhere else.**
   Missing a contract price discrepancy costs money. Missing a real allergen
   conflict can hurt someone. The system has to be built so those two
   failure modes are never scored, reported, or handled the same way.

## What the agent actually does

It does **not** approve menus. Its only autonomous actions are:
1. Deciding what to escalate to a human, and how urgently.
2. Auto-clearing findings that are unambiguously non-issues, so a reviewer's
   limited time goes to what actually needs it.
3. Producing a report with the evidence and reasoning behind every decision.

Every decision is logged with what triggered it (see `audit_trail` in
`agent.py`). Nothing is a black box.

## What's new beyond the CLI version

Real extensions on top of the original deterministic-checks-plus-LLM-
judgment pipeline:

- **Real kitchen allergen reference.** `src/allergen_reference.py` is now
  transcribed directly from the kitchen's own reference cards (the
  FDA "Big 9": milk, egg, fish, shellfish, tree nuts, peanuts, wheat, soy,
  sesame), not a synthetic seed list. Peanut and tree nuts are separate
  categories, per the kitchen's own list, even though the physical card
  combines them on one page. The milk/dairy category is still the
  original placeholder — that card hasn't been provided yet — and the
  module docstring flags exactly what's verified vs. not.
- **Contract-change detection now covers venue, time, date, and
  cancellation**, not just guest count/items/allergens/price.
  `contract_diff.py` flags a reschedule (date change), a venue change
  (high severity — delivery address and kitchen access both need
  rechecking, not just food quantities), a time change, and treats a
  cancellation as a hard-escalate case exactly like a newly-added allergen
  guarantee — no rule or model override can suppress it.
- **Division/brand organization.** `Contract` now carries a `division`
  field (e.g. "Good Tidings" vs. "Goodies To Go"). It's purely an
  organizing/filtering field — the compliance logic, allergen reference,
  and recipe data are identical and shared across every division. The web
  app's sample-events tab filters by division.

- **`src/extract.py`** — reads a photo or PDF of a real contract or
  production sheet and turns it into the same structured schema the rest
  of the pipeline expects, using Gemini's vision input. Every extraction
  comes back with a `_confidence` flag and notes — a misread ingredient is
  a data-entry error like any other, and it's surfaced before it can
  silently feed into a compliance decision, not after.
- **`src/allergen_scan.py`** — a standalone quick allergen check for ONE
  dish, separate from the full contract-vs-sheet workflow. Text input
  (paste an ingredient list) is fully local and free — zero API calls,
  it's just `allergen_reference.scan_recipe()` directly. Photo input uses
  Gemini to read the ingredients first, then the same local scan.
- **`src/recall_checker.py`** — queries the public openFDA Food Enforcement
  API in real time and cross-references active Class I/II recalls against
  the event's ingredients. This is the one genuinely time-sensitive data
  source in the project (everything else is static per-event data), so
  it's opt-in (`--live-recalls` / a toggle in the web app) rather than run
  on every review. A failed lookup is logged as "could not check," never
  silently reported as "nothing found" — see `agent._recall_findings()`.
- **`src/contract_diff.py`** — compares two versions of the same event's
  contract and flags what actually changed, converted into the same
  Finding/Decision shape as everything else. This targets a specific
  failure mode: a production sheet gets printed and hand-annotated, the
  client changes something afterward (guest count, a menu swap, a new
  allergy disclosure), and nobody re-checks the printed sheet against the
  update. A newly-added allergen guarantee is hard-escalated exactly like
  a live allergen conflict — see `event_2201` in the sample data for a
  worked example (a shellfish allergy disclosed after the sheet was
  printed, with shrimp skewers still sitting on the stale sheet).
- **`src/pull_sheet_check.py`** — a separate, fuzzier check for kitchen-
  board "Secure [item]: qty" pull sheets (a flat prep checklist grouped by
  event #, structurally different from a dish-by-dish production sheet).
  Fuzzy word-overlap matching against contracted items, since these two
  documents never use identical phrasing — every finding comes out at
  medium severity precisely because it's inherently less certain than the
  exact-set comparisons elsewhere in the project.
- **`app.py`** — a Streamlit web app wrapping all of the above, deployable
  for free on Streamlit Community Cloud or Hugging Face Spaces.

## Architecture

```
photo/PDF of contract or       contract.json +
production sheet               production_sheet.json
        |                              |
        v                              |
  extract.py (Gemini vision)           |
   -> JSON + _confidence flag          |
        |                              |
        +------------------------------+
                    |
                    v
   parser.py  ->  normalized Contract / ProductionSheet objects
              |
              v
   discrepancy_engine.py  (deterministic — no LLM here)
     - menu alignment      (contracted vs. produced items)
     - allergen conflicts  (guaranteed-free categories vs. ingredient scan)
     - undeclared allergens (recipe card label vs. actual ingredients)
     - guest count drift
              |
              v (optional, opt-in)
   recall_checker.py  ->  live openFDA query  ->  recall_exposure findings
              |
              v
       list[Finding] (type, severity, evidence)
              |
              v
   agent.py :: decide()
     - high-severity allergen conflict OR recall exposure -> ALWAYS escalate (hard rule, no override)
     - other high severity              -> escalate by rule
     - low severity, non-allergen       -> auto-clear by rule
     - medium severity (ambiguous)      -> ask LLM if configured,
                                            else route to human review
              |
              v
   EventReview  ->  format_report() / app.py UI  ->  report + audit trail
```

The deterministic engine and the decision layer are separate modules on
purpose. `discrepancy_engine.py` only establishes facts ("this ingredient
matches this allergen category"). `agent.py` is the only place that decides
what to *do* about a fact. That split is what makes the agent's behavior
auditable instead of "the LLM read the sheet and said it was fine."

### The one hard rule

`NEVER_AUTO_CLEAR` in `agent.py`: a high-severity allergen conflict is
**always** escalated, regardless of what any rule or LLM call would
otherwise decide. There is no code path that lets the system quietly clear
a real allergen match. This is a deliberate ceiling on the agent's
autonomy, not an oversight to fix later.

## Why an LLM is optional, not required

Every unambiguous finding (a contracted item missing entirely, a guaranteed
nut-free contract with nuts in a sauce) is resolved by
`discrepancy_engine.py` alone — no model call, no ambiguity, no risk of an
LLM talking itself out of a real flag. The model is only ever asked to
weigh in on genuinely ambiguous medium-severity cases (e.g., "does a 12-guest
increase communicated by email but never written into the contract count
as a discrepancy worth flagging?"). Without a `GEMINI_API_KEY`, the agent
still runs correctly — it just routes those ambiguous cases to a human
instead of guessing, which is the honest default for a food-safety-adjacent
tool. When a key is configured, Gemini's free tier (no credit card) is
sized generously enough that normal use of this project costs nothing.

## Evaluation

```
python evaluation/evaluate.py
```

Scored against a small, hand-labeled set of 3 sample events (see
`evaluation/labeled_cases.json`):

| Metric | Score |
|---|---|
| Escalation precision | 1.00 |
| Escalation recall | 1.00 |
| Allergen-conflict recall (safety-critical subset) | 1.00 |

**Why allergen-conflict recall is reported separately:** a missed allergen
escalation and a missed menu-count mismatch are not the same failure, and
averaging them into one recall number would let a safety-critical miss hide
behind an otherwise decent score. This eval set is intentionally small and
honest about it — see Limitations.

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

## Design decisions

- **Deterministic checks first, LLM last.** The model is never the first or
  only line of defense on a safety-relevant finding. It's used exactly
  where rule-based logic genuinely can't resolve ambiguity, and nowhere
  else.
- **Hidden-carrier allergen matching.** The allergen reference maps common
  dishes/sauces (satay, Caesar dressing, hoisin, romesco) to the allergens
  they typically carry, not just literal ingredient-name matches. This is
  what catches the cases a keyword scan misses.
- **Auto-clear is conservative.** Only low-severity, non-allergen findings
  are ever auto-cleared. Everything allergen-related is either escalated or
  routed to a human — never silently cleared.
- **No API key = safe degraded mode, not a broken demo.** Ambiguous cases
  default to human review rather than the system guessing.

## Limitations

- **Allergen reference is transcribed from the kitchen's real cards for 8
  of 9 categories** (milk/dairy is still the original seed list — that
  card hasn't been provided yet, see `allergen_reference.py`'s docstring
  for exactly what's verified). This is real operational data, not a
  synthetic placeholder, but it's still one kitchen's working reference,
  not a certified regulatory database — worth an occasional review against
  a source like FDA FALCPA categories, and a few transcription judgment
  calls (peanut/tree-nut split, a couple of terms not on the physical
  cards) are flagged in that same docstring for anyone maintaining it.
- **Substring matching has known false-positive traps for short terms.**
  The scan is plain substring matching, not word-boundary-aware — this is
  why the egg category deliberately uses "eggs" rather than a bare "egg"
  (which would false-positive on "eggplant" and "veggies"). Any new short
  term added to the reference should be checked against this same trap
  before being added.
- **Extraction prompts are tuned to real document formats but not yet
  validated against a live model.** `extract.py`'s prompts were written
  and refined against real photographed contracts and kitchen-board pull
  sheets (handwritten annotations, circled corrections, multi-event pages,
  and all), but running them end-to-end requires a `GEMINI_API_KEY` this
  environment doesn't have — treat the first real run as a validation
  pass, not an assumption, the same as any new prompt.
- **Extraction confidence is self-reported by the model, not independently
  verified.** The app surfaces every extracted field for human review
  before it's trusted, and that review step is load-bearing, not optional
  — this matters more, not less, on real documents with overlapping
  handwriting, sticky notes, and magnets partially covering the text.
- **Scanned/image PDFs are handled via Gemini's native PDF input**, not
  rasterized locally — this is simpler than the earlier text-extraction-
  first approach, but it means a scanned PDF's read quality depends
  entirely on Gemini's own PDF handling, which hasn't been validated
  against a real scanned banquet order in this environment (see above).
- **Pull-sheet coverage matching is fuzzy by design, not exact.** A
  contract's dish names and a pull sheet's "Secure X" phrasing rarely
  match word-for-word, so `pull_sheet_check.py` uses a loose word-overlap
  threshold and reports every finding at medium severity — it's a
  starting point for a human's eyes, not a precise diff.
- **The recall check does substring/keyword matching against openFDA's
  free-text fields**, not a structured product/UPC match. It's a
  reasonable real-time signal, not a guarantee of catching every relevant
  recall — a real deployment would want to match against a supplier's
  actual product/lot numbers where available.
- **Small eval set.** 3 of the 4 sample events are hand-labeled by one
  reviewer, and the eval harness doesn't yet cover the extraction,
  contract-diff, pull-sheet-coverage, or standalone allergen-scanner paths
  — it scores the original deterministic-checks-plus-decision pipeline
  only. A production version needs a larger, multi-reviewer-labeled set
  drawn from real (anonymized) past events.
- **This is a decision-support tool.** It never approves a menu or clears
  an allergen conflict on its own authority. Every escalation and every
  "needs review" item is a recommendation for a human to act on, not an
  autonomous action taken on their behalf.
