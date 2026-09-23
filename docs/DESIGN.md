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
- **A menu description edit is grounded against `allergen_reference.py`
  before it ever reaches the LLM.** If scanning the old and new
  description text finds a different set of allergen categories (a term
  added, removed, or both), that's decided by rule -- a direct match
  against the kitchen's own reference data, not a judgment call, and
  more reliable than trusting Gemini's general reasoning to always catch
  it. Only a description edit with no detectable allergen-term change
  still goes to the LLM. Deliberately NOT exposed to Gemini as a
  callable tool it might choose to use -- a plain check that runs
  unconditionally on every edit is more reliable than hoping a model
  reliably decides to call it.
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

### Investigation agent (`src/investigation_agent.py`)

An "Investigate" button appears next to every escalated change (review
items don't get one — investigation is for things you're about to act
on, not things you're glancing at). Opt-in and per-item, deliberately
not automatic on every escalation: it's a real API cost, and the chef
deciding when more context is worth it is more consistent with this
project's guardrail philosophy than running it unconditionally on every
upload.

- **Runs a bounded, manually-controlled Gemini tool-calling loop** (see
  `llm_client.run_tool_loop()`) with four read-only tools:
  `get_dish_allergen_note`, `find_other_dates`,
  `recent_notifications_for_event`, and `check_known_allergens` (a
  direct wrapper around `allergen_reference.scan_ingredient()`). No
  write-capable tool exists at all — that's the actual enforcement of
  "never takes an action," not just a prompt instruction.
- **Manual control, not the SDK's automatic function calling.**
  `rate_guard.check_and_increment()` is checked before every single
  Gemini attempt everywhere else in this project. Automatic function
  calling would execute the whole tool-call loop inside one
  `generate_content()` call, with no hook to check rate_guard between
  the model's internal round-trips. Manual control means every turn is
  this project's own code calling `generate_content()` directly, so
  that guarantee holds here too, and a max-turns cap (4) bounds a single
  investigation's cost independently of the daily limit.
- **Cached, not re-run.** The result is written back onto that specific
  change in Firestore (`contract_store.add_investigation_note()`, same
  read-whole-array-write-it-back pattern as `mark_change_reviewed()`),
  so investigating something once doesn't mean spending another API
  call every time that notification re-renders.
- Verified end to end against real Firestore and a real Gemini call
  before being wired into the UI (both the isolated tool-calling
  mechanics and the full `investigate()` path).

### Ask chatbot (`src/ask_agent.py`)

A read-only chat interface over stored events and change history, for
open-ended questions ("what's on the menu for event X," "what's
happening on this date," "has this event had any changes logged") that
don't map to a specific button anywhere else in the app. Surfaced as a
**floating button** (bottom-right, present on every tab), not its own
tab — opens as a popup (`st.dialog`) so it's reachable from wherever you
already are instead of needing to navigate away first.

- **Conversation history is actually sent to the model, not just
  displayed.** `llm_client.run_tool_loop()` takes an optional `history`
  param -- without it, every question started a brand-new conversation
  with zero memory of anything said earlier, even though `app.py`'s UI
  displayed the full chat history the whole time. Displaying history
  and sending it to the model turned out to be two different things,
  and only one of them happened before this existed: a follow-up like
  "now tell me the allergens" or a dish referenced loosely ("the fruit
  tray") couldn't be resolved against something shown two messages
  earlier in the SAME visible conversation. Capped to the last 10
  entries to keep cost bounded as a conversation gets long. Gemini's own
  role name for an assistant turn is `"model"`, not `"assistant"` --
  converted at the `llm_client` boundary so `app.py`'s chat history can
  keep using the same role strings `st.chat_message`-style code already
  used.
- **The floating button is a real `st.button()`, not an injected HTML
  element.** Positioned via CSS targeting the `st-key-<key>` class
  Streamlit adds to a container given a `key` (a stable, documented
  mechanism), rather than the confetti effect's approach of injecting a
  plain DOM element outside Streamlit's component tree via
  `components.html()`. The two look similar (both are "just CSS
  positioning" on the surface) but solve different problems: a plain
  injected element can't trigger a Python rerun or open a dialog without
  a full custom bidirectional component; a real `st.button()` inside a
  styled container gets that for free.
- **Chat bubbles are custom HTML, not `st.chat_message()`.** Streamlit's
  built-in chat message container has no `key` parameter, so there's no
  reliable way to CSS-target a user message differently from an
  assistant message by role -- the same gap the floating button worked
  around, solved the same way: real markup this project controls
  directly (`_render_chat_bubble()`) instead of fighting an opaque
  built-in component's internal DOM. Right-aligned/amber for the chef's
  own messages, left-aligned/neutral for the assistant's. A minimal
  markdown-to-HTML pass (`_chat_bubble_html()`) handles just `**bold**`
  and `- ` bullet lists -- not a full renderer, since the model's
  answers are short facts/lists, never tables or code -- and escapes
  the raw text BEFORE inserting any of that markup, so nothing in a
  question or answer can inject real HTML.
- **Tool output is formatted for a chat bubble, not a log file.**
  `notifications_for_event()`'s raw `created_at` is a full ISO 8601
  timestamp with microseconds and a UTC offset
  (`2026-09-23T22:37:06.575606+00:00`) -- readable as data, not as
  something a chef wants to read in a conversation. Reformatted to the
  same "always EST, real Eastern clock time" convention the
  Notifications tab already uses (`Sep 23, 06:37 PM EST`) before it ever
  reaches the model, so the model's answer reads like a message, not a
  data dump it's relaying verbatim.
- **Notifications now carry `event_date`** (`contract_store.save_notification()`
  gained an `event_date` parameter). Without it, `notifications_for_event()`
  could only filter by `event_id` -- and the same `event_id` can
  legitimately be a completely different booking that reused the number
  on a different date, which this project never treats as the same
  event anywhere else. When that tool finds history under more than one
  date for an `event_id`, it returns an `AMBIGUOUS` result instead of
  silently merging both bookings' history together, and the system
  prompt instructs the model to ask the chef which date before
  answering rather than guessing. A notification saved before this
  field existed has no `event_date` on file -- treated as "assume
  relevant" (included) rather than excluded when a specific date is
  requested, since silently hiding real history is worse than
  occasionally including an old notification that turns out to belong
  to a different date's booking.
- **`st.rerun()` inside the dialog must be `scope="fragment"`, never
  bare.** `st.dialog` inherits `st.fragment` behavior -- Streamlit's own
  docs are explicit that a bare (full-app-scoped) rerun from inside a
  dialog closes it, since the dialog-opening function is never called
  again during a full-app rerun. A bare `st.rerun()` after answering a
  question, or after clearing the conversation, was exactly why the
  chat closed itself after every message.
- **Same shared tool-loop as the investigation agent**
  (`llm_client.run_tool_loop()`) — no separate loop implementation, same
  manual-control-for-rate_guard reasoning documented above.
- **Six read-only tools**, all bound to one `(client, division)` pair via
  closure: `lookup_event`, `find_other_dates`, `events_on_date` (new --
  see `contract_store.find_by_date()`), `notifications_for_event`,
  `get_dish_allergen_note`, `check_known_allergens`. No write-capable
  tool exists.
- **Scoped to one division per conversation**, with a division selector
  inside the popup — switching divisions starts a fresh conversation
  rather than letting one chat span both, preserving the hard boundary
  enforced everywhere else in this app.
- `contract_store.find_by_date()` is a full collection scan filtered by
  fuzzy `dates_match()`, not an indexed query — `event_date` isn't
  stored in a normalized form, so there's no field to index on directly.
  Fine at the scale a single catering operation's Firestore collection
  actually reaches; flagged in its own docstring as not a pattern to
  reuse at higher volume without normalizing the stored field first.

**A real production bug was found (and fixed) while building this.**
Testing the chatbot's `lookup_event` tool against real data surfaced
that every one of the 6 real contract records on file had a legacy
Firestore document ID (bare `event_id`, e.g. `"95521"`) left over from
before the current `event_id + event_date` composite-ID scheme existed.
`contract_store.lookup()` only ever checked the new-style ID, so it
silently failed to find any of these real records — meaning a revised
upload for any of these 6 real events would have been treated as a
brand-new baseline instead of being diffed, with no notification
generated, exactly the failure mode this entire tool exists to prevent.
Migrated all 6 to the correct ID. **The migration itself caused real
data loss for 2 records** (`104086`, `104087` in Goodies To Go): the
migration script wrote to each record's correct-ID location without
first checking whether something already existed there, and two of
those locations already held more recent, correct data (created a day
earlier) that got silently overwritten before the older legacy data was
written in its place. Both issues are now closed — every document has a
unique, correct ID (verified, zero mismatches) — but the pre-overwrite
content of those 2 specific records was not recoverable from within
this session (no Point-in-Time Recovery access available). Recorded
here in full rather than summarized away, since this is exactly the
kind of incident a project claiming to be careful with real production
data shouldn't gloss over.

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
  `evaluate_contract_agent.py` (63 cases) and `evaluate_llm_judgment.py`
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
