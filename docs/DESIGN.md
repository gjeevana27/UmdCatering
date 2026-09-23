# Design, architecture, and limitations

Full rationale behind this project. The [README](../README.md) covers
what to run and how; this is the why and the fine print.

## Good-to-Go (`app.py`)

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
compliance agent and extended for real packing lists:

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

## Intake Agent (`src/intake_agent.py`)

A separate, standalone script (not part of the Streamlit app — runs in
its own terminal/process) that watches a local folder for contract
files and auto-routes them, so getting a contract into the system
becomes "save the file here" instead of "open the app and click
Upload." Built specifically to stay on the right side of a line worth
being explicit about: this agent does open-ended reasoning about *what
kind of file something is* and *whether it's ready to route* — the part
of this project genuinely worth calling agentic — but it never touches
extraction values or `contract_agent.py`'s decisions. Widening that
scope (letting it re-read a field, resolve an ambiguous case, or
override an escalation) would trade away exactly the auditability this
project has been built around, for no real benefit.

- **One shared inbox, not one per division.** A document's own
  header/footer already declares which division it belongs to — the
  same field the manual upload flow already reads — so a whole day's
  mixed batch of Good Tidings and Goodies To Go contracts can be
  dropped into one folder together, and each file still gets routed to
  its own division's Firestore collection independently.
- **Classification runs BEFORE the full extraction, deliberately.**
  `extract.classify_document()` is a new, cheap Gemini call that answers
  one question — contract, pull sheet, or other — before
  `extract_contract_record()`'s full schema ever runs. Forcing an
  unrelated file through a prompt built for a Menu Packing List would
  either produce garbage or waste a more expensive call; classifying
  first means a stray file costs one small call, not a wasted large one.
- **Shares the exact same pipeline as the manual upload flow, not a
  second copy of it.** `contract_store.extract_and_classify()` and
  `contract_store.diff_and_store()` used to live inside `app.py` as
  `_extract_and_classify()`/`_diff_and_store()`, coupled to Streamlit's
  `st.session_state` for the two cases that need a human decision
  (an unreadable division, a possible reschedule). Moved into
  `contract_store.py` as plain functions that return a structured
  `{"status": "ready" | "needs_review", ...}` result instead of writing
  UI state directly — `app.py` now wraps that result into
  `st.session_state` for its pending-confirmation UI, and
  `intake_agent.py` wraps the same result into a file move. One
  implementation of what actually happens to a contract; two thin
  callers, not two versions of the decision logic that could quietly
  drift apart from each other over time.
- **Never resolves an ambiguous case on its own.** A "needs_review"
  result (unreadable division, reschedule ambiguity, no event ID,
  extraction failure) moves the file to `intake/needs_review/` with a
  `.txt` note explaining why, and stops there — the same file would sit
  in `app.py`'s pending-confirmation queue waiting for a human either
  way; this agent just has nowhere to ask a human *in the moment*, since
  there's no browser session watching it. Building a second resolution
  path for a headless script wasn't worth the risk of it drifting from
  the Streamlit one.
- **Debounced against partial writes.** Waits for a file's size to stop
  changing across two checks, ~2 seconds apart, before reading it — a
  still-downloading or still-copying file should never be read
  half-written.
- **Rate-limit-aware, distinctly from other failures.** Checks
  `rate_guard.calls_made_today()` before attempting a file; if the daily
  cap is already hit, the file is left untouched in the inbox to retry
  once quota resets, rather than moved to `needs_review/` — that
  distinction matters, since a rate-limited file isn't the file's fault
  the way a genuine extraction failure is.
- **Firestore writes outlive the source file.** The moment a file is
  successfully routed, the Firestore write is permanent — nothing about
  moving a processed file to `processed/`, or deleting it there months
  later, touches what's already stored.

## Guardrails

`src/extract.py` and `src/llm_client.py` are the common call points for
everything below:

- **No local PII accumulation.** Every uploaded photo/PDF is a real
  customer document. It used to get written to the OS temp directory
  with `delete=False` (needed so the file survived long enough for
  Gemini's SDK to read it by path) and never cleaned up afterward —
  silently growing an unbounded folder of real customer documents on
  whatever machine runs the app. `_temp_upload_file()` now guarantees
  deletion in a `finally` block, success or failure, at both upload call
  sites in `app.py`. Deleting the file doesn't affect what gets stored: by the time the
  file is removed, Gemini has already returned a parsed JSON response,
  which lives on as a plain Python dict independent of the file --
  everything downstream (building a record, diffing it, writing to
  Firestore) uses that dict, never the file path again.
- **A daily call-count circuit breaker** (`src/rate_guard.py`) — an
  in-process, thread-safe counter shared by every Gemini call site.
  Checked immediately before each actual API attempt (including
  retries); once today's count hits the configured limit (300 by
  default, `GEMINI_DAILY_CALL_LIMIT` to override), further calls fail
  clearly instead of a bug (an infinite retry loop, a batch triggered
  twice) silently running up real cost. Deliberately not a replacement
  for Google Cloud's own billing alerts -- it's a same-process,
  same-day safety net that resets on app restart, not an authoritative
  spend tracker.
- **Output validation on every extraction** (`extract._validate_extracted_dict()`,
  applied at all 6 extraction entry points) — Gemini's JSON response is
  parsed but not otherwise guaranteed to satisfy anything about its
  *content*. A count field is checked as a real non-negative number: a
  negative value is auto-corrected to 0 with `_confidence` force-downgraded
  to `low` and a note explaining why, so it still flows into the app's
  existing human-review path instead of silently feeding a bad number
  into a percent-change calculation. A field that's supposed to be a
  list but comes back as some other type raises `ExtractionError`
  outright, since that's not something safe to guess a fix for.

## Design decisions

- **Deterministic checks first, LLM last.** The model is never the first or
  only line of defense on a safety-relevant finding. It's used exactly
  where rule-based logic genuinely can't resolve ambiguity, and nowhere
  else.
- **Hidden-carrier allergen matching.** The allergen reference maps common
  dishes/sauces (satay, Caesar dressing, hoisin, romesco) to the allergens
  they typically carry, not just literal ingredient-name matches. This is
  what catches the cases a keyword scan misses.
- **No silent third tier.** Every change contract_agent.py sees resolves
  to escalate or review — never a quiet auto-clear, for any field,
  allergen-related or not. A small guest-count bump or a qty/unit tweak
  still surfaces, because deciding something's "too minor to matter" is
  itself a judgment call this system doesn't make on the chef's behalf.
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
- **`classify_document()`'s accuracy hasn't been validated against a
  real document either**, same limitation as extraction above and for
  the same reason (no `GEMINI_API_KEY`-backed live run possible in this
  environment). The routing logic around it (file moves, needs_review
  notes, the rate-limit check) is verified with fabricated inputs — see
  `intake_agent.py`'s test coverage from its build — but whether the
  model itself reliably tells a contract apart from a pull sheet or a
  stray unrelated file is unproven until it runs against real intake
  traffic. No automated eval exists for it yet, same bottleneck as
  `evaluate_extraction.py`: it needs real, hand-labeled document images.
- **The rate-limit circuit breaker is per-process, not persisted.**
  `rate_guard.py`'s daily counter lives in memory — it resets on every app
  restart and isn't shared across instances if this were ever deployed as
  more than one replica. That's a deliberate scoping choice, not an
  oversight: this is a single-instance Streamlit deployment, and the
  actual spend ceiling is Google Cloud's own billing alerts, not this
  counter — persisting it to Firestore would mean a database round-trip
  on every single Gemini call just to track a number that already has an
  authoritative backstop elsewhere.
- **Eval sets are hand-labeled by one reviewer.**
  `evaluate_contract_agent.py` (60 cases) and `evaluate_llm_judgment.py`
  (36 cases, live) were both deliberately scaled up from an original 8 to
  include boundary and adversarial cases specifically — an
  exact-15%-threshold guest count, a non-numeric OCR-garbled number,
  cosmetic-only quantity reformatting, a same-place location written two
  different ways — rather than only the obvious example of each rule,
  since a perfect score on a handful of easy cases doesn't mean much.
  That scaling is what surfaced a real, disclosed disagreement in the
  live eval (35/36, not 1.00 — see README's Evaluation section for the
  specific case); it's left in and reported honestly rather than dropped
  or relabeled to make the number cleaner.
- **`evaluate_extraction.py` closes the biggest remaining gap** — the
  standalone allergen scanner still has no automated eval, but until now
  neither did extraction accuracy itself: the vision-to-JSON step every
  other layer's eval silently assumes is already correct. The harness is
  built and scores field-level accuracy (weighted toward the fields that
  actually drive `contract_agent.py`'s decisions) plus whether the
  model's own self-reported `_confidence` actually tracks its error
  rate, but it has nothing to score yet: it needs real, hand-labeled
  contract photos, which can't ship in this repo (real client PII) and
  don't exist in this environment either. See
  `data/real_examples/README.md` and
  `evaluation/label_extraction_example.py` for how to build examples as
  real photos come in from actual use. A production version needs a
  larger, multi-reviewer-labeled set across every harness, drawn from
  real (anonymized) past events.
- **This is a decision-support tool.** It never approves a menu or clears
  an allergen conflict on its own authority. Every escalation and every
  "needs review" item is a recommendation for a human to act on, not an
  autonomous action taken on their behalf.
