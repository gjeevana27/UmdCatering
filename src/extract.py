"""
Converts a real-world document -- a photo of a recipe card, a scanned
banquet event order, or a contract PDF -- into the structured
Contract / ProductionSheet / PullSheet JSON schema the rest of the
pipeline expects.

Uses Google's Gemini API (free tier — see README for setup). Extraction is
intentionally kept separate from agent.py and discrepancy_engine.py: a bad
extraction (a misread ingredient, a garbled item name) should be visibly
flagged as low-confidence, not silently fed into the compliance checks as
if it were ground truth. That's why every extracted field comes back with
a confidence flag the caller can inspect before trusting it.

Requires GEMINI_API_KEY (free, no credit card -- see
https://aistudio.google.com/apikey). Every PDF and image is sent to Gemini
as native file input (not pre-extracted to text first) -- a naive text
extraction (previously via pdfplumber) proved unreliable on real
table/form-heavy documents like a Menu Packing List, silently returning
only a fragment (e.g. just the letterhead) with no reliable way to detect
that failure before it was too late. Letting Gemini read the actual page
directly avoids that failure mode entirely.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional, Union

import rate_guard

try:
    from google import genai
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

MODEL = "gemini-3.5-flash-lite"  # gemini-3.5-flash was tried first, but its
# free tier is capped at just 20 requests/day/project (confirmed via a live
# 429 quota error, not documented anywhere), which exhausts in a single
# pipeline run. gemini-2.5-flash-lite (higher documented free-tier RPD) was
# tried next but is no longer available to new users -- Google's own 404
# for it points here instead. Check
# https://ai.google.dev/gemini-api/docs/rate-limits for current numbers if
# this has been superseded.


class ExtractionError(Exception):
    pass


def _validate_extracted_dict(data: dict, *, int_fields: tuple = (),
                              list_fields: tuple = ()) -> dict:
    """
    Sanity-checks structural things Gemini's JSON response should always
    satisfy but isn't guaranteed to -- a wrong type, a negative count, a
    field that's just missing. Not a full schema validator; just a
    guardrail against the kind of malformed output that would otherwise
    silently corrupt a downstream comparison or decision (a negative
    guest_count feeding straight into a percent-change calculation, a
    menu_items that's a string instead of a list crashing every caller
    differently and confusingly).

    Auto-corrects what's safely recoverable (a negative count reset to 0)
    and force-downgrades _confidence + appends a note when it does, so
    the record still flows through the app's existing
    low-confidence-needs-human-review path instead of either crashing on
    something fixable or silently trusting a suspicious value. Raises
    ExtractionError only for a structural problem that can't be safely
    auto-corrected (a required field is entirely the wrong type).
    """
    notes = []

    for field in int_fields:
        if field not in data:
            continue
        val = data[field]
        try:
            val = int(val)
        except (TypeError, ValueError):
            raise ExtractionError(
                f"Extracted field '{field}' is not a valid number: {val!r}"
            )
        if val < 0:
            notes.append(f"'{field}' was negative ({val}), reset to 0")
            val = 0
        data[field] = val

    for field in list_fields:
        if field in data and not isinstance(data[field], list):
            raise ExtractionError(
                f"Extracted field '{field}' should be a list of items, got "
                f"{type(data[field]).__name__}: {data[field]!r}"
            )

    if data.get("_confidence") not in ("high", "medium", "low"):
        notes.append("'_confidence' was missing or not one of high/medium/low")

    if notes:
        data["_confidence"] = "low"
        existing = data.get("_confidence_notes", "") or ""
        data["_confidence_notes"] = (existing + " " if existing else "") + "; ".join(notes)

    return data


CONTRACT_SCHEMA_PROMPT = """You are extracting structured data from an event
CONTRACT document. This is typically a catering "Menu Packing List" style
document with an Event Information block (Event ID, Event Date, Customer,
# of Guests, Event Type, Salesperson, EventTime) followed by a table of
recipe line items. IMPORTANT: many recipe lines have an indented description
underneath naming what's included -- e.g. a buffet entree might say
"includes Side Salad for 12, 12 Rolls, 12 Butters & 12 Cookies" directly
below the dish name. Treat each of those included components as its OWN
entry in contracted_menu_items (e.g. "Side Salad (12)", "Rolls (12)"), not
just text you skip -- a component that's contracted but never makes it onto
the kitchen board is exactly the kind of gap this tool exists to catch.

Read the attached document carefully and return ONLY a JSON object with
this exact shape, no other text:

{
  "event_id": "<the Event ID printed on the document, e.g. from 'Event ID: 104113' or a handwritten '#113' -- prefer the printed Event ID over a handwritten shorthand if both appear>",
  "client_name": "<customer/client name>",
  "guest_count": <integer, from '# of Guests'>,
  "contracted_menu_items": ["<item name>", ...],
  "guaranteed_allergen_free": ["<category>", ...],
  "price_per_head": <number, or 0 if not shown on this document>,
  "special_instructions": "<Special Inst / Menu Notes field verbatim, including any handwritten notes visible on the page>",
  "event_date": "<the Event Date field, as printed, e.g. 'Saturday, December 13, 2025'>",
  "venue": "<read ONLY the text printed immediately beside/after the 'Location:' label itself, under DELIVERY INFORMATION -- verbatim, whatever it says (e.g. 'Chapel', 'Stamp', 'Delivery Instructions'). Do NOT read the separate 'Event Address:' field instead, even if Location looks short, informal, or not like a real address -- they are two different labeled fields on this document and only the text next to 'Location:' belongs here.>",
  "event_time": "<EventTime field, e.g. '3:15 PM - 4:15 PM'>",
  "status": "<'cancelled' if the document is marked cancelled/voided anywhere (including a handwritten 'CANCELLED' stamp or note), otherwise 'confirmed'>",
  "division": "<the catering division/brand this document is from, if shown in a header or logo -- e.g. 'Good Tidings' or 'Goodies To Go'. Leave empty string if not identifiable.>",
  "_confidence": "high" | "medium" | "low",
  "_confidence_notes": "<one sentence: what, if anything, was illegible, handwritten-over, or ambiguous -- e.g. overlapping handwritten annotations, a crossed-out quantity>"
}

allergen categories must be chosen ONLY from this exact set, translating
whatever language the document uses (including things like "Kosher Meals",
"Vegan and Gluten Free" call-outs -- note these as guaranteed_allergen_free
categories ONLY if the document guarantees the WHOLE event is free of them;
a dietary accommodation for a SUBSET of guests, e.g. "4 Kosher Meals" out of
125 guests, belongs in special_instructions instead, not
guaranteed_allergen_free, since it doesn't guarantee the whole event/dish
is free of that thing):
["milk", "egg", "fish", "shellfish", "peanut", "tree_nuts", "sesame", "wheat_gluten", "soy"]

If a field genuinely isn't present in the document, use a reasonable
default (0 for numbers, [] for lists, "" for strings) and say so in
_confidence_notes. Do not guess at numbers you cannot actually see, and do
not let a handwritten circle or annotation overwrite a clearly legible
printed value unless the handwriting is clearly a correction to it."""

PRODUCTION_SHEET_SCHEMA_PROMPT = """You are extracting structured data from
an event PRODUCTION SHEET or recipe card -- a dish-by-dish document listing
each menu item with its ingredients (as opposed to a flat prep checklist;
if the document you're looking at is a flat "Secure [item]: quantity" style
checklist grouped by event number instead, that's a different document
type -- do not use this schema for that, a pull-sheet schema exists
separately for it).

Read the attached document carefully and return ONLY a JSON object with
this exact shape, no other text:

{
  "event_id": "<any identifier visible on the document>",
  "guest_count": <integer>,
  "notes": "<any handwritten or printed notes verbatim>",
  "menu_items": [
    {
      "name": "<dish name>",
      "ingredients": ["<ingredient>", ...],
      "declared_allergens": ["<category>", ...]
    }
  ],
  "_confidence": "high" | "medium" | "low",
  "_confidence_notes": "<one sentence: what was unclear, illegible, or inferred>"
}

List every ingredient you can actually read, including ones inside a named
sauce or component (e.g. if "satay sauce" is listed as a sub-component and
its own ingredients are visible, list those too -- do not just write
"satay sauce" and stop, since hidden-allergen detection depends on this).

allergen categories must be chosen ONLY from this exact set:
["milk", "egg", "fish", "shellfish", "peanut", "tree_nuts", "sesame", "wheat_gluten", "soy"]

declared_allergens should only include what the document ITSELF explicitly
labels as an allergen -- do not infer allergens from ingredients here, that
is a separate downstream step. If nothing is declared, use []."""

PACKING_LIST_ALLERGEN_SCHEMA_PROMPT = """You are reading an event catering
PACKING LIST or MENU document for a quick allergen-screening tool. Unlike a
real recipe card, these documents typically list each dish by name with at
most one short description line (e.g. "ZAATAR GRILLED CHICKEN served over
pearled cous cous and seasonal vegetables") -- there usually is NO actual
ingredients list printed anywhere on the page.

For each dish: first note anything literally printed in its name or
description. Then, since that's rarely a complete ingredient breakdown,
use your general culinary knowledge of the dish to infer a REASONABLE,
FULL list of likely ingredients -- e.g. for "Zaatar Grilled Chicken,"
infer chicken, olive oil, and the zaatar spice blend's typical components
(thyme, sumac, sesame seeds), even though only the word "zaatar" is
printed. This is a deliberate best-effort inference for allergen-safety
screening, NOT a verified read of a real recipe -- a human always reviews
and confirms every finding before trusting it, so err on the side of
including a plausible ingredient rather than silently omitting one.

Read the attached document carefully and return ONLY a JSON object with
this exact shape, no other text:

{
  "event_id": "<any identifier visible on the document>",
  "event_date": "<the Event Date field, exactly as printed, if visible -- empty string if not shown>",
  "guest_count": <integer, or 0 if not shown>,
  "menu_items": [
    {
      "name": "<dish name, exactly as printed>",
      "ingredients": ["<ingredient>", ...],
      "declared_allergens": ["<category>", ...]
    }
  ],
  "_confidence": "high" | "medium" | "low",
  "_confidence_notes": "<one sentence: what was unclear, illegible, or purely inferred with no textual basis at all>"
}

allergen categories must be chosen ONLY from this exact set:
["milk", "egg", "fish", "shellfish", "peanut", "tree_nuts", "sesame", "wheat_gluten", "soy"]

declared_allergens should only include what the document ITSELF explicitly
labels as an allergen (e.g. an "Allergies:" field or a marked dietary
callout) -- not your own inference. If nothing is declared, use []."""

PULL_SHEET_SCHEMA_PROMPT = """You are extracting structured data from a
kitchen-board PULL SHEET / prep checklist -- an Excel-exported document
structured as a flat list of "Secure [item]: quantity" line items, usually
grouped under headers like "EVENT # 939". This is NOT a dish-by-dish recipe
card -- items here are prep/sourcing tasks (e.g. "Secure Gluten Free
Bread", "6 Bahn Mi Roll", "35 Secure Apples"), sometimes with a quantity
written before the item name, sometimes highlighted to flag a special-diet
accommodation (gluten-free, vegan) that needs separate sourcing.

A single photo often shows MULTIPLE events stacked on one page (look for
repeated "EVENT # NNN" header rows). Extract EVERY event block visible,
even partial ones cut off at the edge of the photo -- return one object
per event block found. Return ONLY a JSON array (even if there's only one
event) with this exact shape, no other text:

[
  {
    "event_id": "<the number after 'EVENT #'>",
    "guest_count": <integer, or 0 if not shown on this specific sheet -- pull sheets often don't repeat the guest count>,
    "secure_items": [
      {
        "name": "<the item/component name, e.g. 'Bahn Mi Roll', 'Secure Gluten Free Bread', 'Chix Salad, Lett, Tomato'>",
        "quantity": "<the number as printed/handwritten, as a STRING since these mix plain counts like '6' with annotated ones like '2 (1 per)' -- keep it exactly as written>",
        "notes": "<any sub-line description directly under the item, e.g. ingredient breakdown or a handwritten circle/note nearby>"
      }
    ],
    "_confidence": "high" | "medium" | "low",
    "_confidence_notes": "<one sentence: what was unclear -- these sheets are often photographed with circled numbers, overlapping sticky-notes, or magnets/tape covering part of the text, so be specific about what was obscured>"
  }
]

Do not skip an item because its quantity is ambiguous or handwritten over
-- include it with your best reading and flag it in _confidence_notes
rather than omitting it, since a silently dropped line is worse than a
flagged uncertain one for this kind of document."""

CONTRACT_RECORD_SCHEMA_PROMPT = """You are extracting data from a catering
"Menu Packing List" contract document for a contract-version tracking tool.
Extract ONLY the fields listed below -- this is a narrower extraction than
a full compliance review, used purely to detect what changed between two
versions of the same event's contract.

Read the attached document carefully and return ONLY a JSON object with
this exact shape, no other text:

{
  "event_id": "<the Event ID field, e.g. '95161' -- the printed system ID, not a handwritten shorthand like '#161'>",
  "event_date": "<the Event Date field exactly as printed, e.g. 'Tuesday, February 24, 2026'>",
  "event_time": "<the EventTime field, e.g. '11:30 AM - 1:00 PM'>",
  "location": "<read ONLY the text printed immediately beside/after the 'Location:' label itself, under DELIVERY INFORMATION -- verbatim, whatever it says (e.g. 'Chapel', 'Stamp', 'Delivery Instructions'). Do NOT read the separate 'Event Address:' field instead, even if Location looks short, informal, or not like a real address -- they are two different labeled fields on this document and only the text next to 'Location:' belongs here.>",
  "event_type": "<the Event Type field, e.g. 'EVENT - LUNCH BUFFET (OP)'>",
  "guest_count": <integer, from '# of Guests'>,
  "time_desc_notes": "<everything in the 'Time & Desc' section, verbatim>",
  "menu_items": [
    {
      "qty_unit": "<the Qty-Unit column value for this line, e.g. '275.00-EACH'>",
      "recipe_name": "<the bold/capitalized recipe name, e.g. 'MIXED GREEN SALAD'>",
      "description": "<the description/notes line(s) directly underneath that recipe name -- ingredients, additions, production notes. Empty string if there is none.>"
    }
  ],
  "division": "<'Good Tidings' or 'Goodies To Go', read from the header logo or the footer file-tag -- the footer ends in 'BatchGT' for Good Tidings or 'BatchGTG' for Goodies To Go>",
  "print_datetime": "<the 'Print Date/Time:' value from the page footer, exactly as printed, e.g. '4/29/2026 1:56:36 PM' -- this is when the DOCUMENT ITSELF was generated/printed, a completely different thing from the Event Date field above (which is when the event happens). Empty string if no such footer stamp is visible on this document.>",
  "_confidence": "high" | "medium" | "low",
  "_confidence_notes": "<one sentence: what was unclear, illegible, or handwritten-over>"
}

CRITICAL: extract EVERY menu line item on the page, not a sample or the
first few -- a missed item is exactly the kind of gap this tool exists to
catch. Section header rows (like "~ On the Buffet ~" or "* 4 Kosher Meals
*") are not menu items themselves; skip them, but don't skip the items
listed under them. Preserve exact capitalization on recipe_name as printed.

Do not let a handwritten circle, arrow, or correction overwrite a clearly
legible printed value unless the handwriting is obviously correcting it."""

INGREDIENT_LIST_SCHEMA_PROMPT = """You are reading a SINGLE dish, recipe
card, or ingredient label -- not a full contract or multi-item sheet. Read
every ingredient you can see, including ones named inside a sub-component
or sauce (e.g. if "satay sauce" is listed, and its own ingredients are
visible, list those too rather than just writing "satay sauce").

Return ONLY a JSON object with this exact shape, no other text:

{
  "dish_name": "<the dish or item name, or a short description if no name is given>",
  "ingredients": ["<ingredient>", ...],
  "_confidence": "high" | "medium" | "low",
  "_confidence_notes": "<one sentence: what was unclear or illegible>"
}"""


def _require_genai():
    if not _GENAI_AVAILABLE:
        raise ExtractionError("The 'google-genai' package is not installed.")
    if not os.environ.get("GEMINI_API_KEY"):
        raise ExtractionError(
            "GEMINI_API_KEY is not set. Document/photo extraction requires "
            "it -- there is no rule-based fallback for reading unstructured "
            "documents, unlike the ambiguous-case judgment calls in "
            "llm_client.py. Get a free key (no credit card) at "
            "https://aistudio.google.com/apikey"
        )


def _media_type_for(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
    }.get(ext, "image/png")


def _call_gemini_extract(schema_prompt: str, *, text: Optional[str] = None,
                          file_path: Optional[Path] = None) -> Union[dict, list]:
    """
    Exactly one of `text` or `file_path` should be given. `file_path` may
    be an image OR a PDF -- Gemini accepts both as native file input, so a
    scanned/image PDF doesn't need to be rasterized first (unlike the
    earlier Claude-based version of this module).

    Retries up to three times, with increasing pauses (3s / 10s / 25s), on
    a transient server-side error (HTTP 503 "high demand" is common on
    Gemini's free tier) before giving up -- at real-world volume (a chef
    uploading dozens of documents a day), a single retry left too much
    surface area for a routine blip to become a hard failure she'd have to
    notice and manually redo. A persistent failure is surfaced as
    ExtractionError -- a normal, expected, catchable error -- rather than
    an unhandled exception that crashes the whole app.
    """
    _require_genai()
    from google import genai  # local import: guaranteed bound here since
    # _require_genai() already confirmed the package is installed -- this
    # also gives static type checkers a direct, unconditional import to
    # trace, instead of the module-level conditional one used only for
    # the availability check above.
    from google.genai import errors as genai_errors
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    if file_path is not None:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        from google.genai import types
        contents = [
            types.Part.from_bytes(data=file_bytes,
                                   mime_type=_media_type_for(file_path)),
            schema_prompt,
        ]
    else:
        contents = f"{schema_prompt}\n\nDOCUMENT TEXT:\n{text}"

    retry_backoff_seconds = [3, 10, 25]  # 3 retries after the first attempt

    response = None
    last_error = None
    for attempt in range(len(retry_backoff_seconds) + 1):
        try:
            rate_guard.check_and_increment()
            response = client.models.generate_content(model=MODEL, contents=contents)
            break
        except rate_guard.RateLimitExceeded as e:
            raise ExtractionError(str(e)) from e
        except genai_errors.ServerError as e:
            last_error = e
            if attempt < len(retry_backoff_seconds):
                time.sleep(retry_backoff_seconds[attempt])
                continue
            raise ExtractionError(
                f"Gemini's servers are temporarily overloaded (503, high "
                f"demand) and didn't recover after {len(retry_backoff_seconds)} "
                f"retries. This is not a bug in this project -- it's "
                f"Google's free tier under load. Wait a minute and try "
                f"again: {e}"
            ) from e
        except genai_errors.ClientError as e:
            raise ExtractionError(
                f"Gemini rejected the request (not a transient issue -- "
                f"check GEMINI_API_KEY is valid and the file isn't too "
                f"large): {e}"
            ) from e

    if response is None:
        # Defensive only -- every real code path above either sets
        # response and breaks, or raises. This should be unreachable.
        raise ExtractionError(f"Gemini call failed with no response: {last_error}")

    if response.text is None:
        raise ExtractionError(
            "Gemini returned no text content -- this can happen if its "
            "safety filters blocked the response, or it returned something "
            "other than plain text. Try again, or with a different photo/"
            "crop if it keeps happening."
        )
    raw = response.text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ExtractionError(
            f"Model response wasn't valid JSON: {e}\nRaw response: {raw[:500]}"
        ) from e


def extract_contract(file_path) -> dict:
    """
    file_path: a PDF, PNG, JPG, or WEBP of a contract.
    Returns a dict matching parser.Contract's fields, plus _confidence and
    _confidence_notes. Caller should surface low-confidence extractions to
    a human rather than feeding them straight into the compliance pipeline.

    Always sends Gemini the actual page (native PDF/image input), rather
    than pre-extracting text via pdfplumber first -- pdfplumber's plain
    text extraction is unreliable on table/form-heavy layouts like a real
    Menu Packing List (it can silently grab only a header and miss the
    entire event/menu table), and there's no reliable way to detect that
    failure from the text alone. Gemini reads the page directly instead.
    """
    path = Path(file_path)
    result = _call_gemini_extract(CONTRACT_SCHEMA_PROMPT, file_path=path)
    assert isinstance(result, dict), f"Expected a JSON object, got {type(result).__name__}"
    return _validate_extracted_dict(
        result, int_fields=("guest_count",),
        list_fields=("contracted_menu_items", "guaranteed_allergen_free"),
    )


def extract_production_sheet(file_path) -> dict:
    """Same contract as extract_contract(), for production sheets."""
    path = Path(file_path)
    result = _call_gemini_extract(PRODUCTION_SHEET_SCHEMA_PROMPT, file_path=path)
    assert isinstance(result, dict), f"Expected a JSON object, got {type(result).__name__}"
    return _validate_extracted_dict(result, int_fields=("guest_count",),
                                     list_fields=("menu_items",))


def extract_packing_list_for_allergens(file_path) -> dict:
    """
    file_path: a photo or PDF of an event packing list/menu -- dishes
    listed by name with little to no real ingredient detail. Deliberately
    a SEPARATE prompt/function from extract_production_sheet(): that one
    is used by the full compliance pipeline's stricter, literal-reading
    checks (app_compliance_agent_full.py), and changing its behavior to
    infer ingredients more aggressively would silently affect those
    higher-stakes findings too. This function is only for the standalone
    allergen-screening tool, where every dish's ingredients are already
    understood to be Gemini's best-guess inference, clearly labeled as
    such by the caller, not a verified document read.
    """
    path = Path(file_path)
    result = _call_gemini_extract(PACKING_LIST_ALLERGEN_SCHEMA_PROMPT, file_path=path)
    assert isinstance(result, dict), f"Expected a JSON object, got {type(result).__name__}"
    return _validate_extracted_dict(result, int_fields=("guest_count",),
                                     list_fields=("menu_items",))


def extract_pull_sheet(file_path) -> list:
    """
    file_path: a photo or PDF of a kitchen-board pull sheet (the
    "Secure [item]: qty" checklist format, grouped by EVENT # -- see
    PULL_SHEET_SCHEMA_PROMPT for why this is a separate schema from
    extract_production_sheet()).

    Returns a LIST of dicts (one per event block found on the page), since
    a single board photo commonly shows several events stacked together.
    Each dict matches parser.PullSheet's fields, plus _confidence/
    _confidence_notes.
    """
    path = Path(file_path)
    result = _call_gemini_extract(PULL_SHEET_SCHEMA_PROMPT, file_path=path)

    if isinstance(result, dict):
        blocks = [result]
    elif isinstance(result, list):
        blocks = result
    else:
        raise ExtractionError(
            f"Expected a JSON array of event blocks, got {type(result).__name__}"
        )
    return [_validate_extracted_dict(b, int_fields=("guest_count",),
                                      list_fields=("secure_items",))
            for b in blocks]


def extract_contract_record(file_path) -> dict:
    """
    file_path: a photo or PDF of a Menu Packing List contract.
    Returns a dict matching contract_store.ContractRecord's fields
    (plus _confidence / _confidence_notes) -- the narrow extraction used
    by the contract-version tracker, as opposed to extract_contract()'s
    fuller schema used by the compliance agent.
    """
    path = Path(file_path)
    result = _call_gemini_extract(CONTRACT_RECORD_SCHEMA_PROMPT, file_path=path)
    assert isinstance(result, dict), f"Expected a JSON object, got {type(result).__name__}"
    return _validate_extracted_dict(result, int_fields=("guest_count",),
                                     list_fields=("menu_items",))


def extract_ingredient_list(file_path) -> dict:
    """
    file_path: a photo or PDF of a SINGLE dish/recipe card -- the input
    for the standalone allergen scanner, as opposed to a full contract or
    production sheet. Returns {dish_name, ingredients, _confidence,
    _confidence_notes}.
    """
    path = Path(file_path)
    result = _call_gemini_extract(INGREDIENT_LIST_SCHEMA_PROMPT, file_path=path)
    assert isinstance(result, dict), f"Expected a JSON object, got {type(result).__name__}"
    return _validate_extracted_dict(result, list_fields=("ingredients",))
