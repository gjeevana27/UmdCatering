"""
Storage, lookup, and comparison for the contract-version tracker.

This is deliberately a minimal module: it does one thing -- extract a
contract, compare it against whatever version is already stored for that
event, and report every difference. No allergen logic, no
production-sheet checks, no recall feed.

Storage is Firestore, one database, two COMPLETELY SEPARATE collections --
Good Tidings and Goodies To Go never read or write each other's collection,
enforced here at the query level, not just as a UI convention.

Document ID = (event_id, event_date) TOGETHER, within each division's
collection -- not event_id alone, since the same event_id can legitimately
recur on a different date (e.g. a different booking reusing an old event
number), and those must never be silently compared against or overwrite
each other. Match logic:
  - exact (event_id, event_date) match on file -> this is an update to a
    known booking, diff every field, then overwrite that same record.
  - event_id matches something on file, but under a DIFFERENT date ->
    ambiguous: could be a genuine reschedule (same event, date changed --
    the chef can link it, which diffs it against the prior date's record
    and then retires that old record) or a different event that happens to
    reuse the same event number (the chef keeps both, tracked
    independently forever after). This is surfaced to the chef to decide
    -- never silently merged either way. See find_other_dates().
  - event_id not found under any date -> brand new event, store as the
    baseline, nothing to compare yet.
"""

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from dateutil import parser as dateutil_parser
from google.cloud import firestore
from google.oauth2 import service_account

import contract_agent
import extract


DIVISIONS = ["Good Tidings", "Goodies To Go"]

_COLLECTION_NAMES = {
    "Good Tidings": "good_tidings_contracts",
    "Goodies To Go": "goodies_to_go_contracts",
}


def dates_match(date_a: str, date_b: str) -> bool:
    """
    Compares two event-date strings by actual calendar date, not exact
    text. Two extraction calls on the same document can come back with
    different formatting for the identical date ("Tuesday, May 5, 2026"
    vs "Tuesday, 5/5/2026") -- a plain string comparison treats those as
    a mismatch and wrongly flags a stable event as changed. Parses both
    with dateutil (fuzzy, so it tolerates a leading weekday name it
    doesn't need) and compares the resulting calendar date only.

    Falls back to exact string comparison if either string fails to
    parse as a date at all -- safer than silently treating two unparsable
    strings as equal.
    """
    if date_a == date_b:
        return True
    try:
        parsed_a = dateutil_parser.parse(date_a, fuzzy=True).date()
        parsed_b = dateutil_parser.parse(date_b, fuzzy=True).date()
        return parsed_a == parsed_b
    except (ValueError, OverflowError):
        return False


def _normalize_text(s: str) -> str:
    """Collapses all whitespace (spaces, tabs, newlines) to single spaces
    and strips the ends, so two extraction calls on the SAME identical
    printed text that differ only in line-break placement -- a known
    Gemini non-determinism, not a real edit -- compare as equal instead
    of producing a false 'changed' finding. Text-field comparisons in
    diff_records() use this; dates_match() above is its own, separate
    normalization for the date field specifically."""
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class MenuLineItem:
    qty_unit: str          # e.g. "275.00-EACH"
    recipe_name: str       # the bold/capitalized item name, e.g. "MIXED GREEN SALAD"
    description: str = ""  # the description/notes line underneath it


@dataclass
class ContractRecord:
    event_id: str
    event_date: str
    event_time: str
    location: str
    event_type: str
    guest_count: int
    time_desc_notes: str
    menu_items: list       # list[MenuLineItem]
    division: str = ""
    source_filename: str = ""
    source_file_hash: str = ""  # sha256 of the uploaded file's bytes -- lets
    # a re-upload of the exact same file be recognized before trusting a
    # fresh extraction's diff against it. Two Gemini vision calls on the
    # identical image aren't guaranteed to read every field identically
    # (see app.py's re-upload short-circuit), so a byte-for-byte match is
    # the one comparison that's actually reliable here. Empty string for
    # records saved before this field existed -- see _record_from_dict.
    print_datetime: str = ""  # the document's own "Print Date/Time:"
    # footer stamp, verbatim -- when the DOCUMENT was generated, not when
    # the event happens (that's event_date). Used to order two uploads
    # that land in the same batch under the same (event_id, event_date):
    # upload order alone can't be trusted to reflect which one was
    # actually printed first, but this can. See parse_print_datetime().


def get_client(credentials_path: str = None, credentials_info: dict = None) -> firestore.Client:
    """
    Exactly one of credentials_path (a service-account JSON file on disk --
    local dev) or credentials_info (the parsed JSON as a dict -- pasted
    into Streamlit secrets for cloud deployment) should be given.
    """
    if credentials_info is not None:
        creds = service_account.Credentials.from_service_account_info(credentials_info)
        return firestore.Client(credentials=creds, project=creds.project_id)
    if credentials_path is not None:
        return firestore.Client.from_service_account_json(credentials_path)
    raise ValueError("Provide either credentials_path or credentials_info.")


def _collection(client: firestore.Client, division: str):
    if division not in _COLLECTION_NAMES:
        raise ValueError(f"Unknown division '{division}' -- must be one of {DIVISIONS}.")
    return client.collection(_COLLECTION_NAMES[division])


def _record_from_dict(data: dict) -> ContractRecord:
    items = [MenuLineItem(**i) for i in data.get("menu_items", [])]
    return ContractRecord(
        event_id=data["event_id"], event_date=data["event_date"],
        event_time=data.get("event_time", ""), location=data.get("location", ""),
        event_type=data.get("event_type", ""), guest_count=data.get("guest_count", 0),
        time_desc_notes=data.get("time_desc_notes", ""), menu_items=items,
        division=data.get("division", ""), source_filename=data.get("source_filename", ""),
        source_file_hash=data.get("source_file_hash", ""),
        print_datetime=data.get("print_datetime", ""),
    )


def parse_print_datetime(value: str):
    """Parses a document's printed 'Print Date/Time' footer stamp into a
    real datetime for chronological sorting. Returns None if empty or
    unparseable, so callers can fall back to upload order rather than
    crash on a document that doesn't have this footer at all."""
    if not value or not value.strip():
        return None
    try:
        return dateutil_parser.parse(value, fuzzy=True)
    except (ValueError, OverflowError):
        return None


def _normalize_date_key(event_date: str) -> str:
    """A calendar-date string suitable for a Firestore document ID --
    collapses formatting differences ('Tuesday, May 5, 2026' vs '5/5/2026')
    to the same key, same tolerance as dates_match(). Falls back to a
    slugified version of the raw string if it doesn't parse as a date at
    all, rather than raising -- an unparsable date still needs a stable,
    if imperfect, key."""
    try:
        return dateutil_parser.parse(event_date, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return "".join(c if c.isalnum() else "_" for c in event_date.strip().lower())


def _doc_id(event_id: str, event_date: str) -> str:
    """Storage key = (event_id, event_date) together, not event_id alone --
    the same event_id can legitimately recur on a different date (a
    different booking reusing an old event number), and those must never
    be silently compared against or overwrite each other. See lookup() /
    find_other_dates() for how a genuine reschedule (same event_id, date
    changes) is still detected and offered as a linked update rather than
    just becoming an orphaned second record."""
    return f"{event_id}__{_normalize_date_key(event_date)}"


def lookup(client: firestore.Client, division: str, event_id: str,
           event_date: str) -> "ContractRecord | None":
    """Returns the stored ContractRecord for this EXACT (event_id,
    event_date) pair in this division's collection, or None if nothing's
    on file for that specific date. A different date under the same
    event_id is deliberately NOT returned here -- see find_other_dates()
    for that case."""
    doc = _collection(client, division).document(_doc_id(event_id, event_date)).get()
    if not doc.exists:
        return None
    return _record_from_dict(doc.to_dict())


def find_other_dates(client: firestore.Client, division: str, event_id: str,
                      exclude_event_date: str = None) -> list:
    """All stored records sharing this event_id under a DIFFERENT date than
    exclude_event_date (if given) -- used when lookup() finds no exact
    match, to tell "brand new event_id" apart from "this event_id has
    history under another date," which might be a reschedule. Doesn't
    guess which -- the caller surfaces this to the chef to decide."""
    docs = _collection(client, division).where("event_id", "==", event_id).stream()
    records = [_record_from_dict(d.to_dict()) for d in docs]
    if exclude_event_date is not None:
        records = [r for r in records if not dates_match(r.event_date, exclude_event_date)]
    records.sort(key=lambda r: r.event_date, reverse=True)
    return records


def save(client: firestore.Client, record: ContractRecord):
    """Overwrites whatever was stored for this EXACT (event_id, event_date)
    pair (per the 'overwrite, don't keep history' decision) -- document ID
    = _doc_id(event_id, event_date), scoped to the record's own division's
    collection. A different date under the same event_id is a different
    document and is untouched by this call."""
    data = asdict(record)
    data["stored_at"] = datetime.now(timezone.utc).isoformat()
    _collection(client, record.division).document(_doc_id(record.event_id, record.event_date)).set(data)


def delete_record(client: firestore.Client, division: str, event_id: str, event_date: str) -> None:
    """Removes the stored record for this exact (event_id, event_date) --
    used only when the chef confirms a new upload is the SAME event,
    rescheduled: the old date's record is migrated away (deleted) once its
    contents have been folded into the new date's record via save(),
    keeping one active record per event lineage rather than leaving a
    stale orphan behind."""
    _collection(client, division).document(_doc_id(event_id, event_date)).delete()


_NOTIFICATION_COLLECTION_NAMES = {
    "Good Tidings": "good_tidings_notifications",
    "Goodies To Go": "goodies_to_go_notifications",
}


def _notification_collection(client: firestore.Client, division: str):
    if division not in _NOTIFICATION_COLLECTION_NAMES:
        raise ValueError(f"Unknown division '{division}' -- must be one of {DIVISIONS}.")
    return client.collection(_NOTIFICATION_COLLECTION_NAMES[division])


def save_notification(client: firestore.Client, division: str, event_id: str,
                       source_filename: str, changes: list) -> None:
    """
    Logs one notification: a new upload was compared against the stored
    version for `event_id` and came back with at least one change. Kept as
    its own append-only collection per division (never overwritten, unlike
    `save()`) so the notifications tab has a running history rather than
    just the latest diff.

    changes: list of dicts, each {"label": str, "decision":
    "escalate"|"review", "reasoning": str, "made_by": "rule"|"llm"} -- the
    flattened, display-ready form of contract_agent's ChangeDecision list,
    stored as plain data since the original FieldChange/MenuChange objects
    aren't relevant once already summarized into a label.
    """
    _notification_collection(client, division).add({
        "event_id": event_id,
        "division": division,
        "source_filename": source_filename,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
    })


def mark_notification_reviewed(client: firestore.Client, division: str,
                                notification_id: str, reviewed: bool = True) -> None:
    """Marks a whole notification reviewed/dismissed -- app.py filters
    these out of the notifications feed once True, regardless of whether
    every individual change was checked off or the chef just dismissed it
    outright."""
    _notification_collection(client, division).document(notification_id).update(
        {"reviewed": reviewed}
    )


def mark_change_reviewed(client: firestore.Client, division: str, notification_id: str,
                          change_index: int, reviewed: bool = True) -> list:
    """Marks ONE change within a notification's `changes` list reviewed.
    Firestore has no per-element array update -- this reads the whole
    array, flips the one entry, and writes the array back. Returns the
    updated list so the caller can check whether every change is now
    reviewed (and if so, also call mark_notification_reviewed) without a
    second read.
    """
    doc_ref = _notification_collection(client, division).document(notification_id)
    changes = doc_ref.get().to_dict().get("changes", [])
    if 0 <= change_index < len(changes):
        changes[change_index]["reviewed"] = reviewed
    doc_ref.update({"changes": changes})
    return changes


def list_notifications(client: firestore.Client, division: str, limit: int = 50) -> list:
    """Most recent notifications first, for this division only. Each dict
    includes the Firestore document id under "_id" -- callers need a
    stable per-notification key (e.g. to track expand/collapse state in a
    UI) that survives a rerun, and re-deriving one from the content alone
    isn't reliable when two notifications could share an event_id."""
    docs = (_notification_collection(client, division)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit).stream())
    result = []
    for d in docs:
        data = d.to_dict()
        data["_id"] = d.id
        result.append(data)
    return result


def pull_unresolved_changes_for_event(client: firestore.Client, division: str,
                                       event_id: str) -> list:
    """
    Finds every notification for this event_id that isn't fully reviewed,
    collects whichever of their individual changes are still unchecked,
    and marks those OLDER notifications reviewed -- their unresolved
    content is being folded into a new notification instead, so a chef
    checking "the latest notification" for an event sees everything still
    outstanding, not just what changed in the most recent diff, with an
    older still-open item stuck in a separate notification that's easy to
    never look at again.

    Returns the carried-forward change dicts (same shape save_notification
    expects), for the caller to merge into whatever notification it's
    about to save.
    """
    carried = []
    for n in list_notifications(client, division, limit=50):
        if n.get("event_id") != event_id or n.get("reviewed", False):
            continue
        unresolved = [c for c in n.get("changes", []) if not c.get("reviewed", False)]
        if unresolved:
            carried.extend(unresolved)
        _notification_collection(client, division).document(n["_id"]).update({"reviewed": True})
    return carried


_DISH_ALLERGEN_COLLECTION = "dish_allergen_notes"
# Global, not per-division -- a dish and the allergens a chef knows about
# it aren't specific to Good Tidings vs. Goodies To Go (same shared recipe
# knowledge either way, matching how allergen_reference.py itself is
# shared across divisions).


def _dish_key(dish_name: str) -> str:
    """Same case/whitespace folding diff_records() already uses for
    menu-item matching, so a note saved on 'Zaatar Grilled Chicken' is
    found again even if a later contract prints it differently
    capitalized."""
    return dish_name.strip().lower()


def get_dish_allergen_note(client: firestore.Client, dish_name: str) -> list:
    """The chef-provided allergen list for this dish name, or [] if
    nothing's been noted for it yet."""
    key = _dish_key(dish_name)
    if not key:
        return []
    doc = client.collection(_DISH_ALLERGEN_COLLECTION).document(key).get()
    if not doc.exists:
        return []
    return doc.to_dict().get("allergens", [])


def save_dish_allergen_note(client: firestore.Client, dish_name: str, allergens: list) -> None:
    """Stores/overwrites the chef-provided allergen list for this dish
    name -- flagged automatically every time this same dish name shows up
    in a future scan (see get_dish_allergen_note()). Saving an empty list
    clears the note rather than leaving a stale one on file.

    Deduplicates case-insensitively before writing -- a defense-in-depth
    guarantee independent of whatever the caller already did, since a
    duplicate here means the same allergen gets shown twice on every
    future scan of this dish forever."""
    key = _dish_key(dish_name)
    if not key:
        return
    deduped = []
    seen = set()
    for a in allergens:
        a = a.strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            deduped.append(a)
    client.collection(_DISH_ALLERGEN_COLLECTION).document(key).set({
        "dish_name": dish_name.strip(),
        "allergens": deduped,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


@dataclass
class FieldChange:
    field: str
    old_value: str
    new_value: str
    likely_extraction_miss: bool = False  # old_value had real content and
    # new_value reads blank -- a genuine contract edit essentially never
    # wipes a field down to nothing, so this pattern is far more likely
    # Gemini failing to read the field on this pass than an actual change
    # (see extract.py's docstring on why extraction isn't 100% consistent
    # call to call). contract_agent.py escalates this by rule, distinctly
    # from an ordinary field change, so it doesn't depend on manual review
    # or the LLM judge noticing on its own.


@dataclass
class MenuChange:
    change_type: str  # "added" | "removed" | "changed"
    recipe_name: str
    detail: str
    old_description: str = ""  # populated only for "changed" entries where
    new_description: str = ""  # the description itself differs -- kept
    # separate from `detail` (a pre-formatted "field: 'old' -> 'new'"
    # display string) so callers building an LLM prompt can hand it the
    # literal old/new text directly, instead of re-parsing `detail` or
    # risking conflating it with recipe_name (see contract_agent.py's
    # judge_contract_change call -- that conflation previously produced a
    # hallucinated reasoning that referenced the dish's NAME as if it were
    # part of the description that changed).
    qty_unit: str = ""           # populated for "added"/"removed" entries
    item_description: str = ""   # populated for "added"/"removed" entries
    # -- same idea as old_description/new_description above: kept as their
    # own fields so a UI can build a short title ("ADDED -- Dish (qty)")
    # with the description on its own line, instead of the single long
    # "New item: qty -- description" string in `detail` (still built, kept
    # for anything that just wants one plain-text line).


def diff_records(old: ContractRecord, new: ContractRecord) -> tuple:
    """
    Returns (field_changes: list[FieldChange], menu_changes: list[MenuChange]).
    Compares every scalar field plus the full menu list. Menu items are
    matched by recipe_name (case-insensitive) -- an item present in both
    with a different qty_unit or description is a "changed" entry, not a
    remove+add, so the chef sees "this item's quantity changed" rather than
    two unrelated-looking findings.
    """
    field_changes = []
    # time_desc_notes deliberately excluded -- it's a large concatenated
    # blob (Instructions + Equipment/Food delivery times, etc.), and in
    # practice it's been the single noisiest field in this diff: Gemini
    # reads it inconsistently between calls far more than any other field
    # (see the likely_extraction_miss rule below, added for exactly this
    # field originally), and even a "genuine" change here is usually the
    # same information reordered/reformatted rather than something new.
    scalar_fields = ["event_date", "event_time", "location", "event_type",
                      "guest_count"]
    for f in scalar_fields:
        old_val, new_val = getattr(old, f), getattr(new, f)
        if f == "event_date":
            changed = not dates_match(str(old_val), str(new_val))
        elif f == "guest_count":
            changed = str(old_val) != str(new_val)
        else:
            changed = _normalize_text(str(old_val)) != _normalize_text(str(new_val))
        if changed:
            likely_extraction_miss = bool(str(old_val).strip()) and not str(new_val).strip()
            field_changes.append(FieldChange(field=f, old_value=str(old_val),
                                               new_value=str(new_val),
                                               likely_extraction_miss=likely_extraction_miss))

    old_items = {i.recipe_name.strip().lower(): i for i in old.menu_items}
    new_items = {i.recipe_name.strip().lower(): i for i in new.menu_items}

    menu_changes = []
    for key, item in new_items.items():
        if key not in old_items:
            menu_changes.append(MenuChange(
                change_type="added", recipe_name=item.recipe_name,
                detail=f"New item: {item.qty_unit} — {item.description}".strip(" —"),
                qty_unit=item.qty_unit, item_description=item.description,
            ))
    for key, item in old_items.items():
        if key not in new_items:
            menu_changes.append(MenuChange(
                change_type="removed", recipe_name=item.recipe_name,
                detail=f"No longer on the contract (was: {item.qty_unit} — {item.description})".strip(),
                qty_unit=item.qty_unit, item_description=item.description,
            ))
    for key in set(old_items) & set(new_items):
        o, n = old_items[key], new_items[key]
        qty_changed = _normalize_text(o.qty_unit) != _normalize_text(n.qty_unit)
        desc_changed = _normalize_text(o.description) != _normalize_text(n.description)
        if qty_changed or desc_changed:
            parts = []
            if qty_changed:
                parts.append(f"qty/unit: '{o.qty_unit}' → '{n.qty_unit}'")
            if desc_changed:
                parts.append(f"description: '{o.description}' → '{n.description}'")
            menu_changes.append(MenuChange(
                change_type="changed", recipe_name=n.recipe_name,
                detail="; ".join(parts),
                old_description=o.description if desc_changed else "",
                new_description=n.description if desc_changed else "",
            ))
    return field_changes, menu_changes


# ---------------------------------------------------------------------------
# Upload orchestration -- extract, classify, diff, store, notify. Shared by
# app.py's manual upload flow and intake_agent.py's folder watcher, so both
# go through the exact same pipeline instead of two versions that could
# silently drift apart. Neither caller talks to Firestore, contract_agent,
# or extract directly for this path -- everything routes through here.
# ---------------------------------------------------------------------------

def change_label(c) -> tuple:
    """Shared by the on-screen diff and the notification log, so a stored
    notification always reads exactly like what was shown at upload time.

    Returns (title, subtext, css). `title` is kept short and scannable --
    what changed and, for a quantity, by how much -- so it reads at a
    glance. Anything longer (an item's description, the full before/after
    of a description edit) goes in `subtext` instead of being crammed
    onto the same line, for display as a smaller continuation line below
    the title rather than one long run-on string.
    """
    if hasattr(c, "field"):  # FieldChange
        title = f"{c.field.replace('_', ' ')}: {c.old_value} → {c.new_value}"
        return title, "", "field-changed"

    if c.change_type == "added":
        qty = f" ({c.qty_unit})" if c.qty_unit else ""
        return f"ADDED — {c.recipe_name}{qty}", c.item_description, "menu-added"

    if c.change_type == "removed":
        qty = f" (was {c.qty_unit})" if c.qty_unit else ""
        return f"REMOVED — {c.recipe_name}{qty}", c.item_description, "menu-removed"

    # "changed" -- qty/unit and/or description differ (see diff_records
    # above). Keep a qty/unit change in the title itself (short, always
    # worth seeing immediately); push a description change's full
    # before/after to the subtext line instead.
    qty_changed = "qty/unit:" in c.detail
    desc_changed = bool(c.old_description or c.new_description)
    if qty_changed and not desc_changed:
        title = f"CHANGED — {c.recipe_name}: {c.detail}"
        subtext = ""
    elif desc_changed and not qty_changed:
        title = f"CHANGED — {c.recipe_name}: description updated"
        subtext = f"'{c.old_description}' → '{c.new_description}'"
    else:
        title = f"CHANGED — {c.recipe_name}: qty/unit and description updated"
        subtext = c.detail
    return title, subtext, "menu-changed"


def serialize_decisions(result: dict) -> list:
    """Flattens a contract_agent.evaluate_changes() result into plain,
    Firestore-storable dicts for save_notification()."""
    out = []
    for bucket in ("escalate", "review"):
        for d in result[bucket]:
            title, subtext, _ = change_label(d.change)
            out.append({"label": title, "subtext": subtext, "decision": bucket,
                        "reasoning": d.reasoning, "made_by": d.made_by})
    return out


def evaluate_and_store(client: firestore.Client, division: str, data: dict,
                        prior, new_record: ContractRecord, *, migrate_from=None) -> dict:
    """Shared tail end: diff prior vs. new_record, save new_record, log a
    notification if anything changed. `prior` may be None (nothing to
    diff against -- shouldn't happen when this is called, but handled for
    safety). `migrate_from`, if given, is a ContractRecord whose OLD
    (event_id, event_date) document gets deleted after the new one is
    saved -- used for a confirmed reschedule, so linking two dates
    together still leaves exactly one active record for that event
    lineage rather than a stale orphan."""
    field_changes, menu_changes = diff_records(prior, new_record) if prior else ([], [])
    save(client, new_record)
    if migrate_from is not None:
        delete_record(client, division, migrate_from.event_id, migrate_from.event_date)

    reschedule_note = (f"Rescheduled from {prior.event_date} to {new_record.event_date} -- "
                        if migrate_from is not None else "")

    serialized = []
    if field_changes or menu_changes:
        result = contract_agent.evaluate_changes(field_changes, menu_changes,
                                                  extraction_confidence=data.get("_confidence", "high"))
        serialized = serialize_decisions(result)

    # Anything still unresolved from an EARLIER notification on this same
    # event gets folded into this one too, rather than staying stuck in a
    # separate, easy-to-miss older notification -- a chef checking "the
    # latest notification" for an event should see everything still
    # outstanding, not just what changed in this specific diff. Runs even
    # when this diff found nothing new, so an older unresolved item never
    # gets silently orphaned by a no-op re-upload.
    carried = pull_unresolved_changes_for_event(client, division, new_record.event_id)
    seen_labels = {c["label"] for c in serialized}
    for c in carried:
        if c["label"] not in seen_labels:
            serialized.append(c)
            seen_labels.add(c["label"])

    if not serialized:
        return {"status": "stored", "icon": "⚪", "name": new_record.source_filename,
                "message": f"{reschedule_note}No other changes for event {new_record.event_id}."}

    save_notification(client, division, new_record.event_id,
                       new_record.source_filename, serialized)
    escalate_n = sum(1 for c in serialized if c["decision"] == "escalate")
    review_n = len(serialized) - escalate_n
    icon = "🔴" if escalate_n else "🟡"
    return {"status": "stored", "icon": icon, "name": new_record.source_filename,
            "message": f"{reschedule_note}Updated event {new_record.event_id} — "
                       f"{escalate_n} to act on, {review_n} to review — "
                       f"see the Notifications tab."}


def diff_and_store(client: firestore.Client, division: str, data: dict,
                    new_record: ContractRecord, link_to_prior_date=None) -> dict:
    """Looks up whatever's on file for this EXACT (event_id, event_date)
    pair and either stores it as a new baseline, skips it as an exact-file
    repeat, reports back that it needs a reschedule-vs-separate-booking
    decision (never resolves that itself -- see the "needs_review" status
    below), or diffs + saves + logs a notification. Returns one
    {"status", "icon", "name", "message", ...} outcome.

    status is "stored" (something was written to Firestore, possibly with
    a notification) or "needs_review" (nothing was stored -- a human
    decision is required first). A caller with somewhere to surface that
    decision (app.py's pending-confirmation UI) reads `reason`/`pending`
    off a "needs_review" result; a caller with nowhere to ask a human in
    the moment (intake_agent.py) just leaves the source file alone for
    manual handling later.

    link_to_prior_date: a ContractRecord for this event_id under a
    DIFFERENT date, only passed once a human has confirmed this upload is
    the same event, rescheduled -- diffs against that prior record instead
    of doing a fresh exact-match lookup, and retires the prior record
    afterward.
    """
    if link_to_prior_date is not None:
        return evaluate_and_store(client, division, data, link_to_prior_date, new_record,
                                   migrate_from=link_to_prior_date)

    existing = lookup(client, division, new_record.event_id, new_record.event_date)

    if existing is not None:
        if existing.source_file_hash and existing.source_file_hash == new_record.source_file_hash:
            # Byte-for-byte the same file already on record -- nothing on
            # the actual document could have changed, so skip the diff
            # entirely rather than trust two independent Gemini reads of
            # the identical image to agree on every field.
            return {"status": "stored", "icon": "⚪", "name": new_record.source_filename,
                    "message": f"Exact same file already on record for event "
                               f"{new_record.event_id} — nothing to compare."}
        return evaluate_and_store(client, division, data, existing, new_record)

    # No record for this exact (event_id, event_date). Before treating it
    # as a brand-new event, check whether this event_id has history under
    # a DIFFERENT date -- that might be the same event, rescheduled.
    others = find_other_dates(client, division, new_record.event_id,
                               exclude_event_date=new_record.event_date)
    if not others:
        save(client, new_record)
        return {"status": "stored", "icon": "🟢", "name": new_record.source_filename,
                "message": f"New — stored as the baseline for event {new_record.event_id}."}

    prior = others[0]
    return {"status": "needs_review", "reason": "reschedule_ambiguous",
            "icon": "🟡", "name": new_record.source_filename,
            "message": f"Event {new_record.event_id} is on file under a different date "
                       f"({prior.event_date}), this upload says {new_record.event_date} "
                       f"— waiting for a decision.",
            "pending": {"reason": "reschedule", "data": data, "new_record": new_record,
                        "prior_record": prior}}


def extract_and_classify(file_path, file_name: str, division: str = None) -> dict:
    """Extracts and classifies ONE contract file. Shared by app.py's
    manual upload flow and intake_agent.py's folder watcher.

    `division` is the division the CALLER already expects this file to
    belong to (e.g. which Streamlit tab it was uploaded into) -- when
    given, the extracted division is checked against it and a mismatch is
    surfaced rather than trusted, and an unreadable division still gets a
    record built (using this expected value) so a human confirmation can
    resume from it. Pass None when there's no such external expectation
    (the intake agent has no per-division folder to check against): the
    document's own declared division is trusted directly instead, and a
    blank one can't be resolved automatically at all, since there's
    nothing to fall back to.

    Returns one of:
      {"status": "ready", "data": ..., "new_record": ...}
      {"status": "needs_review", "reason": "extraction_failed" |
       "division_mismatch" | "division_unknown" | "no_event_id",
       "icon": ..., "name": ..., "message": ...,
       "data"/"new_record": ... (only for "division_unknown" when an
       expected `division` was given, so a later confirmation can resume)}
    """
    with open(file_path, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()

    try:
        data = extract.extract_contract_record(file_path)
    except extract.ExtractionError as e:
        return {"status": "needs_review", "reason": "extraction_failed",
                "icon": "🔴", "name": file_name, "message": f"Extraction failed: {e}"}

    extracted_division = str(data.get("division", "")).strip()

    if division is not None:
        if extracted_division and extracted_division.lower() != division.lower():
            return {"status": "needs_review", "reason": "division_mismatch",
                    "icon": "🔴", "name": file_name,
                    "message": f"Division mismatch — looks like a {extracted_division} "
                               f"contract (read from its header/footer). Skipped, not stored."}
        record_division = division
    else:
        if not extracted_division:
            return {"status": "needs_review", "reason": "division_unknown",
                    "icon": "🟡", "name": file_name,
                    "message": "Couldn't read a division (Good Tidings / Goodies To Go) "
                               "from this document's header/footer — can't route it "
                               "automatically."}
        record_division = extracted_division

    items = [MenuLineItem(qty_unit=i.get("qty_unit", ""),
                           recipe_name=i.get("recipe_name", ""),
                           description=i.get("description", ""))
             for i in data.get("menu_items", [])]
    new_record = ContractRecord(
        event_id=str(data.get("event_id", "")).strip(),
        event_date=data.get("event_date", ""),
        event_time=data.get("event_time", ""),
        location=data.get("location", ""),
        event_type=data.get("event_type", ""),
        guest_count=data.get("guest_count", 0),
        time_desc_notes=data.get("time_desc_notes", ""),
        menu_items=items,
        division=record_division,
        source_filename=file_name,
        source_file_hash=file_hash,
        print_datetime=data.get("print_datetime", ""),
    )

    if not new_record.event_id:
        return {"status": "needs_review", "reason": "no_event_id",
                "icon": "🔴", "name": file_name,
                "message": "Could not read an Event ID from this document — "
                           "can't store it without one."}

    if division is not None and not extracted_division:
        return {"status": "needs_review", "reason": "division_unknown",
                "icon": "🟡", "name": file_name,
                "message": f"Couldn't confirm this is a {division} document from its "
                           f"header/footer — waiting for your confirmation below.",
                "data": data, "new_record": new_record}

    return {"status": "ready", "data": data, "new_record": new_record}
