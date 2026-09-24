"""
Good-to-Go — Good Tidings / Goodies To Go

Core purpose: upload a contract (photo/PDF), extract the event fields,
compare against whatever is already stored for that event, and tell the
chef what changed. Also includes a standalone allergen scanner (a quick
one-dish ingredient check, no contract needed).

Storage is Firestore. Two completely separate collections, one per
division — a division's uploads only ever read/write its own collection.

Run locally:
    streamlit run app.py

Requires:
    GEMINI_API_KEY               — extraction (see README)
    Firestore credentials, either:
      - FIRESTORE_CREDENTIALS_PATH env var pointing at the service-account
        JSON file (local dev), or
      - st.secrets["FIRESTORE_SERVICE_ACCOUNT_JSON"] — the JSON file's
        full contents pasted as a secret (Streamlit Cloud deployment)
    APP_PASSCODE                 — a shared passcode gating the app, since
        it will run on a public URL against your own API/Firestore quota.
        Set via env var locally or st.secrets on Streamlit Cloud.
"""

import contextlib
import hashlib
import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
load_dotenv()  # loads .env from the project root, if it exists -- see
# .env.example. Silently does nothing if no .env file is present, so this
# is safe to leave in even on a deployment that uses Streamlit secrets
# instead (Streamlit Cloud never has a .env file).

import allergen_scan  # noqa: E402
import ask_agent  # noqa: E402
import contract_agent  # noqa: E402
import contract_store as store  # noqa: E402
import extract  # noqa: E402
import investigation_agent  # noqa: E402
import llm_client  # noqa: E402

st.set_page_config(page_title="Good-to-Go", page_icon="🍽️", layout="wide")

# ---------------------------------------------------------------------------
# Visual identity — color/font tokens.
# ---------------------------------------------------------------------------
st.markdown("""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --paper: #EEF0EC; --paper-raised: #F8F9F6; --ink: #1C2521; --ink-soft: #5B655F;
  --line: #C9CFC8; --amber: #B5720B; --amber-bg: #FBF0DC; --red: #A32B22;
  --red-bg: #FBE7E3; --green: #2E6E4E; --green-bg: #E5F1EA;
}
html, body, [class*="css"] { font-family: 'IBM Plex Mono', ui-monospace, monospace; }
h1, h2, h3 { font-family: 'Fraunces', Georgia, serif !important; font-weight: 600 !important; }
.stApp { background: var(--paper); color: var(--ink); }
.ticket {
  background: var(--paper-raised); border: 1px solid var(--line);
  padding: 18px 20px; margin-bottom: 14px;
}
.field-changed {
  border-left: 4px solid var(--amber); background: var(--amber-bg);
  padding: 10px 14px; margin-bottom: 8px; font-size: 14px;
}
.menu-added { border-left: 4px solid var(--green); background: var(--green-bg);
  padding: 10px 14px; margin-bottom: 8px; font-size: 14px; }
.menu-removed { border-left: 4px solid var(--red); background: var(--red-bg);
  padding: 10px 14px; margin-bottom: 8px; font-size: 14px; }
.menu-changed { border-left: 4px solid var(--amber); background: var(--amber-bg);
  padding: 10px 14px; margin-bottom: 8px; font-size: 14px; }
.stButton button { transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease; }
.stButton button:hover { transform: translateY(-1px); box-shadow: 0 3px 10px rgba(28, 37, 33, 0.14); border-color: var(--amber); }
.stButton button:active { transform: translateY(0); box-shadow: none; }
[data-testid="stVerticalBlockBorderWrapper"] { transition: box-shadow 0.15s ease, border-color 0.15s ease; }
[data-testid="stVerticalBlockBorderWrapper"]:hover { box-shadow: 0 2px 10px rgba(28, 37, 33, 0.10); border-color: var(--amber); }
.stTabs [data-baseweb="tab"] { transition: color 0.15s ease; }
.stTabs [data-baseweb="tab"]:hover { color: var(--amber); }
[data-testid="stFileUploaderDropzone"] { transition: border-color 0.15s ease, background-color 0.15s ease; }
[data-testid="stFileUploaderDropzone"]:hover { border-color: var(--amber); }
.ticket, .field-changed, .menu-added, .menu-removed, .menu-changed { transition: transform 0.15s ease, box-shadow 0.15s ease; }
.ticket:hover, .field-changed:hover, .menu-added:hover, .menu-removed:hover, .menu-changed:hover {
  transform: translateX(2px); box-shadow: 0 2px 8px rgba(28, 37, 33, 0.10);
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
.main .block-container { animation: fadeIn 0.35s ease-out; }
.chicken-lane { position: relative; height: 42px; margin: 0 0 -20px 0; overflow: visible; }
.chicken-walker { position: absolute; left: 0; top: 0; font-size: 36px; line-height: 1; display: inline-block; animation: chicken-walk 7s ease-in-out infinite; }
@keyframes chicken-walk {
  0% { transform: translate(0, 0) scaleX(-1); }
  12% { transform: translate(45px, -5px) scaleX(-1); }
  24% { transform: translate(90px, 0) scaleX(-1); }
  36% { transform: translate(135px, -5px) scaleX(-1); }
  48% { transform: translate(180px, 0) scaleX(-1); }
  50% { transform: translate(180px, 0) scaleX(1); }
  62% { transform: translate(135px, -5px) scaleX(1); }
  74% { transform: translate(90px, 0) scaleX(1); }
  86% { transform: translate(45px, -5px) scaleX(1); }
  98% { transform: translate(0, 0) scaleX(1); }
  100% { transform: translate(0, 0) scaleX(-1); }
}
@keyframes bell-ring {
  0% { transform: rotate(0deg); }
  2% { transform: rotate(14deg); }
  4% { transform: rotate(-10deg); }
  6% { transform: rotate(8deg); }
  8% { transform: rotate(-6deg); }
  10% { transform: rotate(4deg); }
  12% { transform: rotate(-2deg); }
  14% { transform: rotate(0deg); }
  100% { transform: rotate(0deg); }
}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Passcode gate — this runs on a public URL against your own API/DB quota.
# ---------------------------------------------------------------------------
def _get_secret(name):
    val = os.environ.get(name)
    if val:
        return val
    try:
        return st.secrets[name]
    except Exception:
        return None


def require_passcode():
    correct = _get_secret("APP_PASSCODE")
    if not correct:
        return  # no passcode configured -- open access (fine for local dev)
    if st.session_state.get("authed"):
        return
    st.title("🍽️ Good-to-Go")
    code = st.text_input("Passcode", type="password")
    if st.button("Enter"):
        if code == correct:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Incorrect passcode.")
    st.stop()


require_passcode()


# ---------------------------------------------------------------------------
# Firestore client
# ---------------------------------------------------------------------------
@st.cache_resource
def get_firestore_client():
    path = os.environ.get("FIRESTORE_CREDENTIALS_PATH")
    if path:
        return store.get_client(credentials_path=path)
    raw = _get_secret("FIRESTORE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return store.get_client(credentials_info=info)
    return None


client = get_firestore_client()


@contextlib.contextmanager
def _temp_upload_file(uploaded_file):
    """Writes an uploaded file to a temp path (extract.py needs a real
    file path, not bytes, to hand Gemini) and guarantees it's deleted
    afterward, success or failure. Every file processed here is a real
    photo/PDF of a real customer's contract -- leaving these to
    accumulate unbounded in the OS temp directory (the previous
    delete=False behavior, kept only so the file survived long enough
    for extract.py to open it by path) is a real PII exposure risk on
    whatever machine runs this app, not just a tidiness issue."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(uploaded_file.name).suffix) as tf:
        tf.write(uploaded_file.getvalue())
        tmp_path = tf.name
    try:
        yield tmp_path
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Shared rendering
# ---------------------------------------------------------------------------
def _change_label(c) -> tuple:
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

    # "changed" -- qty/unit and/or description differ (see
    # contract_store.diff_records). Keep a qty/unit change in the title
    # itself (short, always worth seeing immediately); push a description
    # change's full before/after to the subtext line instead.
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


def _render_menu_items_structured(menu_items: list):
    """Every menu item's recipe name/qty PLUS whatever's printed
    underneath it (the "Notes" column on a Goodies To Go packing list,
    or a description line on a Good Tidings one -- same `description`
    field either way, see contract_store.MenuLineItem) -- one block per
    item, notes on their own indented line, not folded into a single
    run-on paragraph. Used everywhere a full menu gets shown (the
    upload batch result, a resolved pending-confirmation, the Ask
    chatbot's event lookup), so a dish's real notes are never a click
    or a diff away from where the chef is already looking."""
    for item in menu_items:
        qty = f" ({item.qty_unit})" if item.qty_unit else ""
        st.markdown(f"**{item.recipe_name}**{qty}")
        if item.description:
            st.caption(item.description.replace("\n", "  \n"))


def _serialize_decisions(result: dict) -> list:
    """Flattens a contract_agent.evaluate_changes() result into plain,
    Firestore-storable dicts for save_notification()."""
    out = []
    for bucket in ("escalate", "review"):
        for d in result[bucket]:
            title, subtext, _ = _change_label(d.change)
            out.append({"label": title, "subtext": subtext, "decision": bucket,
                        "reasoning": d.reasoning, "made_by": d.made_by})
    return out


def _evaluate_and_store(division: str, data: dict, prior, new_record, *, migrate_from=None) -> dict:
    """Shared tail end: diff prior vs. new_record, save new_record, log a
    notification if anything changed. `prior` may be None (nothing to
    diff against -- shouldn't happen when this is called, but handled for
    safety). `migrate_from`, if given, is a ContractRecord whose OLD
    (event_id, event_date) document gets deleted after the new one is
    saved -- used for a confirmed reschedule, so linking two dates
    together still leaves exactly one active record for that event
    lineage rather than a stale orphan."""
    field_changes, menu_changes = store.diff_records(prior, new_record) if prior else ([], [])
    store.save(client, new_record)
    if migrate_from is not None:
        store.delete_record(client, division, migrate_from.event_id, migrate_from.event_date)

    reschedule_note = (f"Rescheduled from {prior.event_date} to {new_record.event_date} -- "
                        if migrate_from is not None else "")

    serialized = []
    if field_changes or menu_changes:
        result = contract_agent.evaluate_changes(field_changes, menu_changes,
                                                  extraction_confidence=data.get("_confidence", "high"))
        serialized = _serialize_decisions(result)

    # Anything still unresolved from an EARLIER notification on this same
    # event gets folded into this one too, rather than staying stuck in a
    # separate, easy-to-miss older notification -- a chef checking "the
    # latest notification" for an event should see everything still
    # outstanding, not just what changed in this specific diff. Runs even
    # when this diff found nothing new, so an older unresolved item never
    # gets silently orphaned by a no-op re-upload.
    carried = store.pull_unresolved_changes_for_event(client, division, new_record.event_id)
    seen_labels = {c["label"] for c in serialized}
    for c in carried:
        if c["label"] not in seen_labels:
            serialized.append(c)
            seen_labels.add(c["label"])

    if not serialized:
        return {"icon": "⚪", "name": new_record.source_filename,
                "message": f"{reschedule_note}No other changes for event {new_record.event_id}.",
                "menu_items": new_record.menu_items}

    store.save_notification(client, division, new_record.event_id,
                             new_record.source_filename, serialized,
                             event_date=new_record.event_date)
    escalate_n = sum(1 for c in serialized if c["decision"] == "escalate")
    review_n = len(serialized) - escalate_n
    icon = "🔴" if escalate_n else "🟡"
    return {"icon": icon, "name": new_record.source_filename,
            "message": f"{reschedule_note}Updated event {new_record.event_id} — "
                       f"{escalate_n} to act on, {review_n} to review — "
                       f"see the Notifications tab.",
            "menu_items": new_record.menu_items}


def _diff_and_store(division: str, data: dict, new_record, link_to_prior_date=None) -> dict:
    """Looks up whatever's on file for this EXACT (event_id, event_date)
    pair and either stores it as a new baseline, skips it as an exact-file
    repeat, defers it pending a reschedule-vs-separate-booking decision, or
    diffs + saves + logs a notification. Returns one {"icon", "name",
    "message"} outcome for the batch summary.

    link_to_prior_date: a ContractRecord for this event_id under a
    DIFFERENT date, only passed when the chef has just confirmed (via
    _render_pending_confirmations) that this upload is the same event,
    rescheduled -- diffs against that prior record instead of doing a
    fresh exact-match lookup, and retires the prior record afterward.
    """
    if link_to_prior_date is not None:
        return _evaluate_and_store(division, data, link_to_prior_date, new_record,
                                    migrate_from=link_to_prior_date)

    existing = store.lookup(client, division, new_record.event_id, new_record.event_date)

    if existing is not None:
        if existing.source_file_hash and existing.source_file_hash == new_record.source_file_hash:
            # Byte-for-byte the same file already on record -- nothing on
            # the actual document could have changed, so skip the diff
            # entirely rather than trust two independent Gemini reads of
            # the identical image to agree on every field.
            return {"icon": "⚪", "name": new_record.source_filename,
                    "message": f"Exact same file already on record for event "
                               f"{new_record.event_id} — nothing to compare.",
                    "menu_items": new_record.menu_items}
        return _evaluate_and_store(division, data, existing, new_record)

    # No record for this exact (event_id, event_date). Before treating it
    # as a brand-new event, check whether this event_id has history under
    # a DIFFERENT date -- that might be the same event, rescheduled.
    others = store.find_other_dates(client, division, new_record.event_id,
                                     exclude_event_date=new_record.event_date)
    if not others:
        store.save(client, new_record)
        return {"icon": "🟢", "name": new_record.source_filename,
                "message": f"New — stored as the baseline for event {new_record.event_id}.",
                "menu_items": new_record.menu_items}

    prior = others[0]
    pending_key = f"batch_pending_{division}"
    st.session_state.setdefault(pending_key, []).append({
        "reason": "reschedule", "data": data, "new_record": new_record,
        "prior_record": prior,
    })
    return {"icon": "🟡", "name": new_record.source_filename,
            "message": f"Event {new_record.event_id} is on file under a different date "
                       f"({prior.event_date}), this upload says {new_record.event_date} "
                       f"— waiting for your decision below.",
            "menu_items": new_record.menu_items}


def _extract_and_classify(division: str, uploaded):
    """Extracts and classifies ONE uploaded file, WITHOUT storing anything
    yet -- storing happens afterward in the batch loop below, once every
    file in the batch has been extracted and any same-(event_id,
    event_date) files within it have been reordered by print timestamp
    (see process_division). Returns either ("outcome", result_dict) for a
    terminal case (extraction failure, division mismatch/pending, missing
    event ID), or ("ready", data, new_record) for a file that's ready to
    be diffed and stored."""
    file_bytes = uploaded.getvalue()
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    try:
        with _temp_upload_file(uploaded) as tmp_path:
            data = extract.extract_contract_record(tmp_path)
    except extract.ExtractionError as e:
        return ("outcome", {"icon": "🔴", "name": uploaded.name,
                             "message": f"Extraction failed: {e}"})

    extracted_division = str(data.get("division", "")).strip()
    if extracted_division and extracted_division.lower() != division.lower():
        return ("outcome", {"icon": "🔴", "name": uploaded.name,
                             "message": f"Division mismatch — looks like a {extracted_division} "
                                        f"contract (read from its header/footer). Skipped, not stored."})

    items = [store.MenuLineItem(qty_unit=i.get("qty_unit", ""),
                                 recipe_name=i.get("recipe_name", ""),
                                 description=i.get("description", ""))
             for i in data.get("menu_items", [])]
    new_record = store.ContractRecord(
        event_id=str(data.get("event_id", "")).strip(),
        event_date=data.get("event_date", ""),
        event_time=data.get("event_time", ""),
        location=data.get("location", ""),
        event_type=data.get("event_type", ""),
        guest_count=data.get("guest_count", 0),
        time_desc_notes=data.get("time_desc_notes", ""),
        menu_items=items,
        division=division,
        source_filename=uploaded.name,
        source_file_hash=file_hash,
        print_datetime=data.get("print_datetime", ""),
    )

    if not new_record.event_id:
        return ("outcome", {"icon": "🔴", "name": uploaded.name,
                             "message": "Could not read an Event ID from this document — "
                                        "can't store it without one."})

    if not extracted_division:
        pending_key = f"batch_pending_{division}"
        st.session_state.setdefault(pending_key, []).append({
            "reason": "division", "data": data, "new_record": new_record,
        })
        return ("outcome", {"icon": "🟡", "name": uploaded.name,
                             "message": f"Couldn't confirm this is a {division} document from its "
                                        f"header/footer — waiting for your confirmation below."})

    return ("ready", data, new_record)


def _process_upload_batch(division: str, uploaded_files, on_progress=None) -> list:
    """Extracts every file first, then stores/diffs them -- two passes,
    not one, specifically so files that land in the SAME batch under the
    same (event_id, event_date) can be reordered before any of them touch
    the database. Upload order (whatever order the file picker returns
    them in) doesn't reflect which document was actually produced first;
    the document's own 'Print Date/Time' footer stamp does, so within
    each such group the earliest-printed one is stored first (becoming
    the baseline) and later ones are diffed against it in print order --
    otherwise whichever file happened to come first in the list would be
    treated as the baseline regardless of which one is actually older."""
    total = len(uploaded_files)
    ready = []      # [(data, new_record), ...] in original upload order
    results = []    # terminal outcomes, in original upload order (errors,
                     # pending confirmations)

    for i, uploaded in enumerate(uploaded_files, start=1):
        if on_progress:
            on_progress(i, total, uploaded.name)
        kind, *rest = _extract_and_classify(division, uploaded)
        if kind == "outcome":
            results.append(rest[0])
        else:
            data, new_record = rest
            ready.append((data, new_record))

    groups = {}
    for data, new_record in ready:
        key = store._doc_id(new_record.event_id, new_record.event_date)
        groups.setdefault(key, []).append((data, new_record))

    for group in groups.values():
        if len(group) > 1:
            group.sort(key=lambda dn: (
                store.parse_print_datetime(dn[1].print_datetime) is None,
                store.parse_print_datetime(dn[1].print_datetime) or datetime.min,
            ))
        for data, new_record in group:
            results.append(_diff_and_store(division, data, new_record))

    return results


def _render_pending_confirmations(division: str):
    """Files from a batch upload that couldn't be auto-resolved -- an
    unreadable division marker, or an event date that doesn't match
    what's on file. Persists in session_state across reruns so resolving
    one doesn't force re-extracting the rest of the batch."""
    pending_key = f"batch_pending_{division}"
    pending = st.session_state.get(pending_key, [])
    if not pending:
        return

    st.warning(f"⚠️ {len(pending)} file(s) from your last upload need a decision "
               f"before they're stored:")

    still_pending = []
    resolved = []
    for item in pending:
        new_record = item["new_record"]
        data = item["data"]
        item_key = f"{division}_{new_record.event_id}_{new_record.source_file_hash}_{item['reason']}"

        if item["reason"] == "division":
            prompt = (f"**{new_record.source_filename}** — couldn't confirm this is a "
                      f"**{division}** document from its header/footer. Store it under "
                      f"**{division}** anyway?")
            with st.container(border=True):
                st.markdown(prompt)
                if new_record.menu_items:
                    with st.expander("Menu & notes"):
                        _render_menu_items_structured(new_record.menu_items)
                col_yes, col_no = st.columns(2)
                confirm_clicked = col_yes.button("Yes — store it", key=f"batch_confirm_{item_key}",
                                                  use_container_width=True)
                discard_clicked = col_no.button("No — skip this file", key=f"batch_discard_{item_key}",
                                                 use_container_width=True)

            if confirm_clicked:
                resolved.append(_diff_and_store(division, data, new_record))
            elif discard_clicked:
                resolved.append({"icon": "⚪", "name": new_record.source_filename,
                                  "message": "Skipped — not stored."})
            else:
                still_pending.append(item)

        else:  # "reschedule" -- event_id matches, but a different date is on file
            prior = item["prior_record"]
            prompt = (f"**{new_record.source_filename}** — event {new_record.event_id} is "
                      f"already on file for **{prior.event_date}**, this document says "
                      f"**{new_record.event_date}**. Same event, rescheduled — or a "
                      f"different booking that happens to reuse this event number?")
            with st.container(border=True):
                st.markdown(prompt)
                if new_record.menu_items:
                    with st.expander("Menu & notes"):
                        _render_menu_items_structured(new_record.menu_items)
                col_same, col_diff, col_no = st.columns(3)
                same_clicked = col_same.button("Same event — link it", key=f"batch_link_{item_key}",
                                                use_container_width=True)
                diff_clicked = col_diff.button("Different event — keep both",
                                                key=f"batch_separate_{item_key}",
                                                use_container_width=True)
                discard_clicked = col_no.button("Skip this file", key=f"batch_discard_{item_key}",
                                                 use_container_width=True)

            if same_clicked:
                resolved.append(_diff_and_store(division, data, new_record, link_to_prior_date=prior))
            elif diff_clicked:
                store.save(client, new_record)
                resolved.append({"icon": "🟢", "name": new_record.source_filename,
                                  "message": f"Stored as a separate booking for event "
                                             f"{new_record.event_id} on {new_record.event_date} "
                                             f"(the {prior.event_date} booking is kept as-is).",
                                  "menu_items": new_record.menu_items})
            elif discard_clicked:
                resolved.append({"icon": "⚪", "name": new_record.source_filename,
                                  "message": "Skipped — not stored."})
            else:
                still_pending.append(item)

    st.session_state[pending_key] = still_pending
    if resolved:
        st.success("Resolved just now:")
        for r in resolved:
            st.markdown(f"{r['icon']} **{r['name']}** — {r['message']}")
            if r.get("menu_items"):
                with st.expander("Menu & notes"):
                    _render_menu_items_structured(r["menu_items"])


_DIVISION_BIRD = {"Good Tidings": "🐓", "Goodies To Go": "🐤"}


def process_division(division: str):
    bird = _DIVISION_BIRD.get(division)
    if bird:
        st.markdown(f'<div class="chicken-lane"><span class="chicken-walker">{bird}</span></div>',
                    unsafe_allow_html=True)
    st.header(division)

    if client is None:
        st.error("Firestore isn't configured — set FIRESTORE_CREDENTIALS_PATH "
                  "(local) or the FIRESTORE_SERVICE_ACCOUNT_JSON secret (cloud). "
                  "See README.")
        return

    if not llm_client.is_llm_available():
        st.info("Set GEMINI_API_KEY to extract contracts — no rule-based "
                 "fallback exists for reading a document. Free key, no "
                 "credit card: https://aistudio.google.com/apikey")
        return

    upload_key = f"upload_{division}"
    uploaded_files = st.file_uploader(
        "Contracts (photos or PDFs) — select as many as you like to upload "
        "a whole batch (e.g. a morning's worth) at once",
        type=["png", "jpg", "jpeg", "pdf"], key=upload_key,
        accept_multiple_files=True,
    )

    button_label = (f"Process {len(uploaded_files)} contract(s)" if uploaded_files
                     else "Process contract(s)")
    if st.button(button_label, key=f"process_{division}") and uploaded_files:
        total = len(uploaded_files)
        progress = st.progress(0.0)
        status = st.empty()

        def _on_progress(i, total, name):
            status.write(f"Reading {i} of {total} with Gemini: {name}")
            progress.progress(i / total)

        results = _process_upload_batch(division, uploaded_files, on_progress=_on_progress)
        status.empty()
        progress.empty()

        st.divider()
        st.subheader(f"Batch result — {total} file(s)")
        for r in results:
            st.markdown(f"{r['icon']} **{r['name']}** — {r['message']}")
            if r.get("menu_items"):
                with st.expander("Menu & notes"):
                    _render_menu_items_structured(r["menu_items"])

    _render_pending_confirmations(division)


EASTERN = ZoneInfo("America/New_York")


def _render_notification_row(n: dict, dt):
    """One notification: a badge saying whether it needs action, division +
    event id, the time right-aligned on the far side of the row, and a
    click-to-expand detail panel with every change. `dt` is the parsed
    created_at, already converted to Eastern time (or None if it didn't
    parse), by the caller -- also used there to place this row in the
    right day-group."""
    division = n.get("division", "")
    notif_id = n.get("_id") or f"{n.get('event_id', '')}_{n.get('created_at', '')}"
    changes = n.get("changes", [])
    unreviewed = [(i, c) for i, c in enumerate(changes) if not c.get("reviewed", False)]
    escalate_n = sum(1 for _, c in unreviewed if c.get("decision") == "escalate")
    review_n = sum(1 for _, c in unreviewed if c.get("decision") == "review")

    if escalate_n:
        badge = f"🔴 Act on this — {escalate_n}"
    elif review_n:
        badge = f"🟡 Worth a glance — {review_n}"
    else:
        # Shouldn't happen -- a notification is only ever saved when at
        # least one change was found, and it's filtered out of the feed
        # entirely once every change is reviewed -- but an honest label
        # beats hiding the gap if it somehow did.
        badge = "⚪ No changes logged"

    # Always labeled "EST" rather than switching to "EDT" during daylight
    # saving -- the clock time itself still correctly tracks real New York
    # local time (via the EASTERN zoneinfo conversion in the caller), only
    # the suffix is pinned, per explicit request.
    time_display = (dt.strftime("%I:%M %p EST") if dt is not None
                     else (n.get("created_at") or "unknown time"))

    open_key = f"notif_open_{notif_id}"
    if open_key not in st.session_state:
        st.session_state[open_key] = False

    with st.container(border=True):
        col_check, col_main, col_time = st.columns([0.5, 4.5, 1])
        with col_check:
            if st.checkbox("Mark notification reviewed", key=f"reviewed_{notif_id}",
                            label_visibility="collapsed"):
                store.mark_notification_reviewed(client, division, notif_id, True)
                st.rerun()
        with col_main:
            label = f"{badge}  ·  {division}  ·  Event {n.get('event_id', '')}"
            if st.button(label, key=f"toggle_{notif_id}", use_container_width=True):
                st.session_state[open_key] = not st.session_state[open_key]
        with col_time:
            st.markdown(
                f'<div style="text-align:right; padding-top:0.6em; '
                f'color:var(--ink-soft);">{time_display}</div>',
                unsafe_allow_html=True,
            )

        if st.session_state[open_key]:
            st.caption(f"From: {n.get('source_filename') or 'unknown file'}")
            if not unreviewed:
                st.success("All changes reviewed.")
            for i, c in unreviewed:
                col_c_check, col_c_text = st.columns([0.5, 5.5])
                with col_c_check:
                    if st.checkbox("Mark change reviewed", key=f"change_reviewed_{notif_id}_{i}",
                                    label_visibility="collapsed"):
                        updated = store.mark_change_reviewed(client, division, notif_id, i, True)
                        if all(uc.get("reviewed", False) for uc in updated):
                            store.mark_notification_reviewed(client, division, notif_id, True)
                        st.rerun()
                with col_c_text:
                    icon = "🔴" if c.get("decision") == "escalate" else "🟡"
                    tag_label = "[RULE]" if c.get("made_by") == "rule" else "[JUDGED]"
                    subtext = c.get("subtext", "")
                    subtext_html = (
                        f'<span style="font-size:0.85em; color:var(--ink-soft);">'
                        f'{subtext}</span><br>' if subtext else ""
                    )
                    st.markdown(
                        f'{icon} <span style="font-size:1.15em; font-weight:700;">'
                        f'{c.get("label", "")}</span><br>'
                        f'{subtext_html}'
                        f'<span style="font-size:0.9em; font-style:italic; '
                        f'color:var(--ink-soft);">{tag_label} {c.get("reasoning", "")}</span>',
                        unsafe_allow_html=True,
                    )
                    if c.get("decision") == "escalate":
                        investigation_note = c.get("investigation_note")
                        if investigation_note:
                            st.caption(f"Investigation: {investigation_note}")
                        elif llm_client.is_llm_available():
                            if st.button("Investigate", key=f"investigate_{notif_id}_{i}"):
                                with st.spinner("Investigating..."):
                                    try:
                                        note = investigation_agent.investigate(
                                            client, division, n.get("event_id", ""),
                                            c.get("label", ""), c.get("reasoning", ""),
                                        )
                                    except Exception as e:
                                        note = f"Investigation failed: {e}"
                                store.add_investigation_note(client, division, notif_id, i, note)
                                st.rerun()


def _fire_confetti():
    """One-shot confetti burst, via a Streamlit component iframe running
    canvas-confetti (loaded from a CDN) -- st.markdown() can't run <script>
    tags at all (it's inserted as inert HTML, not executed), so this needs
    components.html(), which renders a real document that does execute
    scripts. The canvas is appended to window.parent.document (the actual
    page, not the iframe) so it overlays the whole viewport instead of
    being clipped to the iframe's own box; it removes itself after the
    burst finishes."""
    components.html("""
<script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.3/dist/confetti.browser.min.js"></script>
<script>
(function() {
  var doc = window.parent.document;
  var canvas = doc.createElement('canvas');
  canvas.style.position = 'fixed';
  canvas.style.top = '0';
  canvas.style.left = '0';
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  canvas.style.pointerEvents = 'none';
  canvas.style.zIndex = '99999';
  doc.body.appendChild(canvas);
  var burst = confetti.create(canvas, { resize: true, useWorker: true });
  burst({ particleCount: 160, spread: 100, startVelocity: 45, origin: { y: 0.3 } });
  setTimeout(function() { canvas.remove(); }, 4000);
})();
</script>
""", height=0)


def _fetch_all_notifications() -> list:
    """Every notification across both divisions, reviewed or not -- shared
    by render_notifications() and the tab-label unread badge/ring in the
    layout section below, so both agree on the same fetch."""
    if client is None:
        return []
    all_notifications = []
    for division in store.DIVISIONS:
        all_notifications.extend(store.list_notifications(client, division, limit=50))
    return all_notifications


def _render_division_notification_feed(division: str, all_notifications: list):
    """One division's day-grouped notification feed, rendered inside its
    own column -- Good Tidings and Goodies To Go each get half the page,
    same side-by-side idea as the Allergen Scan tab's event boxes, except
    the split here is permanent (by division), not paired row by row."""
    st.subheader(division)

    division_all = [n for n in all_notifications if n.get("division") == division]
    notifications = [n for n in division_all if not n.get("reviewed", False)]
    notifications.sort(key=lambda n: n.get("created_at", ""), reverse=True)

    if not notifications:
        if division_all:
            st.success("All caught up.")
        else:
            st.caption("No notifications yet.")
        return

    # Group into (day_label, [(notification, parsed_datetime), ...])
    # buckets, in the same newest-first order as the flat list above --
    # every notification within a day stays sorted newest-first too, since
    # it's carried over from that already-sorted list. Timestamps are
    # stored in UTC (see contract_store.save_notification) but converted
    # to Eastern here BEFORE computing the day label -- a notification
    # logged at 11pm Eastern is after midnight UTC, so grouping on the raw
    # UTC date would put it under the wrong day for a chef reading this in
    # Eastern time.
    day_groups = []
    for n in notifications[:50]:
        try:
            dt = datetime.fromisoformat(n.get("created_at", "")).astimezone(EASTERN)
        except ValueError:
            dt = None
        day_label = dt.strftime("%A, %B %d, %Y") if dt is not None else "Unknown date"
        if day_groups and day_groups[-1][0] == day_label:
            day_groups[-1][1].append((n, dt))
        else:
            day_groups.append((day_label, [(n, dt)]))

    for day_label, day_notifications in day_groups:
        st.markdown(f"**{day_label}**")
        for n, dt in day_notifications:
            _render_notification_row(n, dt)
        st.divider()


def render_notifications():
    all_notifications = _fetch_all_notifications()
    # Fully-reviewed notifications (dismissed outright, or every individual
    # change checked off) don't show in the feed at all.
    unread = [n for n in all_notifications if not n.get("reviewed", False)]
    unread_count = len(unread)

    # Confetti only fires on a genuine had-unread -> now-zero TRANSITION
    # within this browser session, never just from landing on an
    # already-empty/caught-up state (e.g. opening the app fresh, or
    # switching tabs and back). `last_seen_unread_count` starts unset on a
    # brand-new session -- None specifically means "haven't observed a
    # count yet," so the very first render never counts as a transition
    # even if it happens to already be zero.
    prev_count = st.session_state.get("last_seen_unread_count")
    just_caught_up = prev_count is not None and prev_count > 0 and unread_count == 0
    st.session_state["last_seen_unread_count"] = unread_count

    header_text = f"🔔 Notifications ({unread_count})" if unread_count else "🔔 Notifications"
    st.header(header_text)
    st.caption("Every change detected when an uploaded contract was compared "
               "against what's already on file — grouped by day, newest "
               "first, Good Tidings and Goodies To Go side by side. Click a "
               "notification to see exactly what changed.")

    if client is None:
        st.error("Firestore isn't configured — set FIRESTORE_CREDENTIALS_PATH "
                  "(local) or the FIRESTORE_SERVICE_ACCOUNT_JSON secret (cloud). "
                  "See README.")
        return

    if not unread:
        if all_notifications:
            st.success("All caught up — every notification has been reviewed.")
            # Fires once per catch-up, not on every rerun of this same
            # empty state (e.g. switching tabs and back) -- resets below
            # as soon as a new notification shows up again.
            if just_caught_up and not st.session_state.get("confetti_shown", False):
                st.session_state["confetti_shown"] = True
                _fire_confetti()
        else:
            st.info("No notifications yet — one gets logged here the next time an "
                     "uploaded contract differs from what's already on file.")
        return

    st.session_state["confetti_shown"] = False

    col_gt, col_gtg = st.columns(2)
    with col_gt:
        _render_division_notification_feed("Good Tidings", all_notifications)
    with col_gtg:
        _render_division_notification_feed("Goodies To Go", all_notifications)


def _render_allergen_results(dish_name: str, ingredients: list, results: dict):
    """Reuses the app's existing card CSS (.field-changed/.menu-removed)
    rather than plain st.write/st.warning, so this reads consistently with
    every other finding in the app: amber for a direct, visible mention;
    red for a hidden carrier, since that's the one a kitchen is likelier
    to miss precisely because the ingredient name doesn't say it."""
    st.markdown(f'<div class="ticket"><b>{dish_name}</b></div>', unsafe_allow_html=True)
    if not results:
        st.success("No allergens from the tracked category set detected in these ingredients.")
    else:
        for category, matches in results.items():
            label = category.replace("_", " ").upper()
            for m in matches:
                if m["match_type"] == "direct":
                    st.markdown(
                        f'<div class="field-changed"><b>{label}</b> — direct: '
                        f'\'{m["source_ingredient"]}\'</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'<div class="menu-removed"><b>{label} — HIDDEN</b>: '
                        f'\'{m["source_ingredient"]}\' carries {category.replace("_", " ")} '
                        f'via \'{m["matched_term"]}\'</div>',
                        unsafe_allow_html=True,
                    )
    st.caption(f"Ingredients scanned ({len(ingredients)}): " + ", ".join(ingredients))


def _render_event_allergen_box(source_filename: str, data: dict):
    """ONE bordered box for a whole event's packing list -- every dish's
    allergen findings listed compactly inside it, rather than a separate
    big card per match. Every ingredient behind these findings is
    Gemini's inference (see extract_packing_list_for_allergens()), not a
    verified read of the document, so every finding gets its own
    checkbox for the chef to confirm it's actually in the dish -- nothing
    here is presented as settled fact."""
    event_id = data.get("event_id", "").strip() or "(no event ID read)"
    event_date = data.get("event_date", "").strip()
    header = f"Event {event_id}" + (f" — {event_date}" if event_date else "")

    with st.container(border=True):
        st.markdown(f"### {header}")
        st.caption(f"From: {source_filename}")

        conf = data.get("_confidence", "unknown")
        if conf != "high":
            icon = "🟡" if conf == "medium" else "🔴"
            st.warning(f"{icon} Extraction confidence: {conf}. "
                        f"{data.get('_confidence_notes', '')}")

        menu_items = data.get("menu_items", [])
        if not menu_items:
            st.warning("No dishes found on this document.")
            return

        any_findings = False
        # (dish_name, existing_noted, newly_confirmed_categories) collected
        # across EVERY dish in this box, saved and rerun ONCE at the very
        # end -- not per checkbox, not even per dish. Calling st.rerun()
        # any earlier (right after the first checked box, or even right
        # after finishing one dish) cuts the rest of this render off
        # mid-flight, so any checkbox clicked after that point in the same
        # click-batch never got read before the page reran out from under
        # it. That was the "some are saving some are not" bug -- this is
        # the fully robust version: render everything, THEN act once.
        pending_saves = []
        for item_idx, item in enumerate(menu_items):
            name = item.get("name", "Scanned dish")
            ingredients = item.get("ingredients", [])
            results = allergen_scan.scan_text_ingredients(ingredients)
            noted = store.get_dish_allergen_note(client, name) if client is not None else []
            noted_lower = {n.strip().lower() for n in noted}
            # "no allergens" is a deliberate chef override, not just
            # another category -- once on file, showing fresh checkbox
            # suggestions for this dish on any later scan would silently
            # contradict it ("Already known: no allergens" right next to
            # a still-unticked "MILK" checkbox looks like the note never
            # took effect). So it's treated as fully terminal: no more
            # checklist noise for this dish, ever, same as the save-note
            # input already is below. A dish with real confirmed
            # categories (not "no allergens") still gets checkboxes for
            # any NEW category a later scan turns up -- that's a
            # genuinely different, still-open fact, not noise.
            no_allergens_noted = "no allergens" in noted_lower

            st.markdown(f"**{name}**")

            if noted:
                st.markdown(f"📝 **Already known for this dish:** " + ", ".join(noted))

            # Categories already confirmed/noted for this dish are dropped
            # from the checkbox list entirely -- once stored, it shows up
            # as a plain flag above (via `noted`) instead of an
            # unconfirmed tick-mark to check again. Compared as the RAW
            # category string on both sides (e.g. "wheat_gluten") -- this
            # used to compare a space-converted display version against
            # the raw stored version, which never matched, so a confirmed
            # checkbox never actually left the pending list and kept
            # re-saving itself as a duplicate on every single rerun.
            pending_results = {} if no_allergens_noted else {
                c: m for c, m in results.items() if c.lower() not in noted_lower
            }
            if noted or pending_results:
                any_findings = True

            newly_confirmed = []
            for category, matches in pending_results.items():
                label = category.replace("_", " ").upper()
                # ONE checkbox per (dish, category), not one per matching
                # ingredient -- the same allergen showing up as 3 separate
                # rows because 3 different ingredients in the dish all
                # contain milk was noise, not information: confirming
                # "MILK" for a dish is a single fact regardless of how
                # many ingredients contributed to it, which is also
                # exactly what gets saved (save_dish_allergen_note stores
                # one category per dish, never one per ingredient) -- this
                # now matches the UI to the data model it's actually
                # writing to, instead of showing more granularity than
                # the save step ever used.
                icon = "🔴" if any(m["match_type"] == "hidden" for m in matches) else "🟡"
                seen_ingredients = []
                for m in matches:
                    if m["source_ingredient"] not in seen_ingredients:
                        seen_ingredients.append(m["source_ingredient"])
                key = f"allergen_confirm_{event_id}_{item_idx}_{name}_{category}"
                checked = st.checkbox(
                    f"{icon} **{label}** — ({', '.join(seen_ingredients)})",
                    key=key,
                )
                if checked and category.lower() not in noted_lower:
                    newly_confirmed.append(category)

            if newly_confirmed:
                pending_saves.append({"name": name, "base": noted, "values": newly_confirmed})

            if ingredients:
                st.caption("Inferred ingredients: " + ", ".join(ingredients))

            if client is not None and not noted:
                # Only shown while this dish is still UNRESOLVED -- no
                # note on file for it at all yet, from this scan or any
                # earlier one. The instant a dish has ANY saved note
                # (whether that's real allergens confirmed via the
                # checkboxes above, or an explicit "no allergens"), this
                # whole input+button disappears for it, on this scan and
                # every future one: "Already known for this dish" above
                # is the permanent record from then on, and there's
                # nothing left to manually add. Before this check, the
                # input kept showing even for a dish that was fully
                # resolved -- unused empty space at best, and a chance to
                # accidentally re-type something already on file at
                # worst. Always starts empty -- purely for typing NEW
                # allergens to add while still unresolved. Saving merges
                # onto whatever's already stored; it never replaces the
                # list, so there's no way to accidentally wipe out prior
                # entries by submitting an incomplete retype.
                note_key = f"allergen_note_input_{event_id}_{item_idx}_{name}"
                note_input = st.text_input(
                    "Add a NEW allergen you know about this dish (comma-separated, "
                    "added to what's already known above)",
                    value="", key=note_key,
                )
                if st.button("Save note", key=f"allergen_note_save_{event_id}_{item_idx}_{name}"):
                    new_allergens = [a.strip() for a in note_input.split(",")
                                     if a.strip() and a.strip().lower() not in noted_lower]
                    if new_allergens:
                        pending_saves.append({"name": name, "base": noted, "values": new_allergens})

            st.divider()

        if pending_saves and client is not None:
            # Merge every entry per dish before writing ONE final list --
            # both checkbox confirmations and typed notes are purely
            # additive now, so there's no ordering/precedence question if
            # both land in the same batch; they just combine.
            merged = {}
            for entry in pending_saves:
                name = entry["name"]
                combined = merged.get(name, list(entry["base"]))
                for v in entry["values"]:
                    if v.lower() not in {c.lower() for c in combined}:
                        combined.append(v)
                merged[name] = combined
            for name, allergens in merged.items():
                store.save_dish_allergen_note(client, name, allergens)
            # A message set here can't just be st.success()'d in place --
            # st.rerun() below reloads the page before anyone could ever
            # see it. Stashed in session_state instead, and shown once at
            # the top of render_allergen_scan() on the render that follows
            # this rerun, then cleared so it doesn't linger on every
            # later interaction.
            st.session_state["allergen_save_success"] = list(merged.keys())
            st.rerun()

        if not any_findings:
            st.success("No allergens from the tracked category set inferred or "
                       "previously noted for any dish.")


def render_allergen_scan():
    st.header("🔎 Allergen Scan")
    st.caption("Check one dish's ingredients for allergens directly — no contract "
               "or production sheet needed. Pasting text is instant and free "
               "(fully local, no API call); a photo or PDF uses Gemini to read "
               "the ingredients first.")

    scan_mode = st.radio("Input", ["Paste ingredient list", "Upload photo / PDF"],
                          horizontal=True, key="allergen_mode")

    if scan_mode == "Paste ingredient list":
        raw = st.text_area(
            "Ingredients (comma- or newline-separated)",
            placeholder="chicken thigh, satay sauce, lime, cilantro, caesar dressing, croutons",
        )
        if st.button("Scan", key="scan_text") and raw.strip():
            ingredients = allergen_scan.parse_ingredient_text(raw)
            results = allergen_scan.scan_text_ingredients(ingredients)
            _render_allergen_results("Pasted ingredient list", ingredients, results)
        return

    if not llm_client.is_llm_available():
        st.info("Set GEMINI_API_KEY to scan a photo — there's no rule-based "
                 "fallback for reading an image. Free key, no credit card: "
                 "https://aistudio.google.com/apikey")
        return

    photos = st.file_uploader(
        "Photo or PDF of one or more event packing lists — every dish on "
        "each document gets scanned, grouped into one box per event",
        type=["png", "jpg", "jpeg", "pdf"], key="allergen_img",
        accept_multiple_files=True,
    )
    if st.button("Scan", key="scan_photo") and photos:
        scanned = []
        for i, photo in enumerate(photos, start=1):
            with st.spinner(f"Reading {i} of {len(photos)} with Gemini: {photo.name}"):
                try:
                    # extract_packing_list_for_allergens(), not
                    # extract_production_sheet() -- a packing list has no
                    # real ingredients printed on it at all, just a dish
                    # name and maybe one description line, so this asks
                    # Gemini to INFER a plausible ingredient list from
                    # general culinary knowledge instead of only reading
                    # literal text. Kept as its own separate extraction
                    # path specifically so this inference behavior never
                    # bleeds into extract_production_sheet(), which the
                    # full compliance pipeline relies on for stricter,
                    # literal-reading checks.
                    with _temp_upload_file(photo) as p_path:
                        data = extract.extract_packing_list_for_allergens(p_path)
                except extract.ExtractionError as e:
                    st.error(f"{photo.name}: extraction failed: {e}")
                    continue
            scanned.append({"source_filename": photo.name, "data": data})
        # Stored in session_state, not just rendered here -- rendering
        # only happens inside this `if st.button(...)` block runs ONLY on
        # the exact rerun the button click caused. Any later interaction
        # (checking a checkbox, saving a note) triggers its OWN rerun on
        # which st.button() returns False again, so results rendered only
        # here would vanish the instant anyone touched a checkbox. This
        # is what "the page is vanishing" was.
        st.session_state["allergen_scan_results"] = scanned

    saved_dishes = st.session_state.pop("allergen_save_success", None)
    if saved_dishes:
        st.success("Saved allergen info for: " + ", ".join(saved_dishes))

    scans = st.session_state.get("allergen_scan_results", [])
    for row_start in range(0, len(scans), 2):
        row = scans[row_start:row_start + 2]
        cols = st.columns(2)
        for col, scan in zip(cols, row):
            with col:
                _render_event_allergen_box(scan["source_filename"], scan["data"])


def _chat_bubble_html(text: str) -> str:
    """Minimal markdown -> HTML for a chat bubble: **bold**, a run of
    '- '/'* ' lines becoming a real <ul>, an indented line right under a
    bullet nesting INSIDE that <li> instead of becoming its own
    disconnected paragraph (a menu item's "  Notes: ..." line stays
    visually tied to the item it describes), and a lone "---" line
    becoming a real <hr> (multiple events answered in one reply get a
    divider between them instead of running together into one
    undifferentiated block -- see the system prompt's instruction to
    emit "---" between events). Not a full markdown renderer -- the
    model's answers are short facts/lists, never tables or code, so
    this is deliberately just enough, not a new dependency for the
    rest. Escapes the raw text FIRST, then inserts real tags for the
    bits WE add, so nothing in the model's (or the chef's) own text is
    ever interpreted as markup."""
    lines = [html.escape(line) for line in text.strip("\n").split("\n")]
    out, bullet_buffer = [], []

    def _flush_bullets():
        if bullet_buffer:
            out.append("<ul style='margin:4px 0 4px 18px; padding:0;'>"
                       + "".join(f"<li>{b}</li>" for b in bullet_buffer) + "</ul>")
            bullet_buffer.clear()

    for line in lines:
        stripped = line.strip()
        is_indented_continuation = bool(line[:1].isspace() and stripped)
        if stripped in ("---", "***", "___"):
            _flush_bullets()
            out.append("<hr style='border:none; border-top:1px solid var(--line); "
                        "margin:10px 0;'>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            bullet_buffer.append(stripped[2:])
        elif is_indented_continuation and bullet_buffer:
            bullet_buffer[-1] += (f"<br><span style='font-size:0.9em; "
                                   f"color:var(--ink-soft);'>{stripped}</span>")
        else:
            _flush_bullets()
            if stripped:
                out.append(stripped)
    _flush_bullets()

    html_text = "<br>".join(out)
    # **bold** -> <b>bold</b>, applied after escaping so a literal "<" or
    # "&" typed by anyone never becomes real markup.
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html_text)


def _render_chat_bubble(role: str, content: str) -> None:
    """A single WhatsApp-style bubble -- right-aligned/amber for the
    chef's own messages, left-aligned/neutral for the assistant's. Built
    as plain HTML (st.markdown(..., unsafe_allow_html=True)) rather than
    st.chat_message(), which has no `key` parameter and therefore no
    reliable way to CSS-target one message differently from another by
    role -- same reasoning as the floating Ask button using a real,
    keyed st.container() instead of fighting an opaque built-in
    component's internal DOM."""
    is_user = role == "user"
    align = "flex-end" if is_user else "flex-start"
    bg = "var(--amber-bg)" if is_user else "var(--paper-raised)"
    border = "var(--amber)" if is_user else "var(--line)"
    st.markdown(
        f'<div style="display:flex; justify-content:{align}; margin:4px 0;">'
        f'<div style="max-width:78%; padding:6px 11px; border-radius:14px; '
        f'background:{bg}; border:1px solid {border}; font-size:0.8em; '
        f'line-height:1.35; color:var(--ink);">{_chat_bubble_html(content)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_ask_content():
    """Read-only chatbot over stored contract/notification data (see
    src/ask_agent.py). Scoped to ONE division per conversation, same hard
    boundary enforced everywhere else in this app -- switching the
    division selector starts a fresh conversation rather than letting one
    chat span both. Rendered inside the floating-button dialog below, not
    its own tab -- the dialog already shows "Ask Crumbly" as its title
    bar, so no redundant header here."""
    st.caption("Ask Crumbly about events, dates, and change history already on "
               "file — read-only, scoped to one division at a time. Never "
               "changes any decision or stored data.")

    if client is None:
        st.info("Firestore isn't configured — nothing to ask about yet.")
        return
    if not llm_client.is_llm_available():
        st.info("Set GEMINI_API_KEY to use this tab. Free key, no credit card: "
                 "https://aistudio.google.com/apikey")
        return

    division = st.radio("Division", store.DIVISIONS, horizontal=True, key="ask_division")
    history_key = f"ask_history_{division}"
    pending_delete_key = f"ask_pending_delete_{division}"
    if history_key not in st.session_state:
        st.session_state[history_key] = []

    if st.session_state[history_key] and st.button("Clear conversation", key="ask_clear"):
        st.session_state[history_key] = []
        st.session_state.pop(pending_delete_key, None)
        st.rerun(scope="fragment")  # same reasoning as below -- a bare rerun here closed the dialog too

    # History renders FIRST, in plain top-to-bottom document order --
    # then the input, so it's the last thing on the page. The earlier
    # bug wasn't stickiness, it was that a just-submitted question's
    # reply used to be rendered INLINE right after the st.chat_input()
    # call, landing physically below the input box in the DOM. Fixed by
    # never rendering the new exchange inline at all: it's appended to
    # session_state and answered with an immediate st.rerun(), so the
    # render that actually shows it goes through this SAME loop as
    # every older message, in its natural place at the end, above the
    # input -- not a special case that could end up in the wrong spot.
    for msg in st.session_state[history_key]:
        _render_chat_bubble(msg["role"], msg["content"])

    # Event cancellation: Crumbly can only ever PROPOSE a deletion (by
    # emitting ask_agent.CONFIRM_DELETE_RE's marker line once it's
    # confirmed one exact event via its own read-only tools) -- this is
    # the ONLY place a delete_record() call for this feature actually
    # happens, gated on a real button click, never on anything the model
    # says in chat. Rendered right after history (same "never inline
    # after chat_input" reasoning as the messages above), so it appears
    # as the natural next thing after Crumbly's proposal, still above
    # the input.
    pending = st.session_state.get(pending_delete_key)
    if pending:
        with st.container(border=True):
            st.warning(f"⚠️ Delete **Event {pending['event_id']}** — "
                       f"**{pending['event_date']}** from {division}? This permanently "
                       f"removes it from Firestore and cannot be undone.")
            col_yes, col_no = st.columns(2)
            confirm_clicked = col_yes.button("Yes, delete permanently",
                                              key=f"ask_delete_confirm_{division}",
                                              use_container_width=True)
            cancel_clicked = col_no.button("Cancel", key=f"ask_delete_cancel_{division}",
                                            use_container_width=True)
            if confirm_clicked:
                # Re-looked-up fresh right here, not trusting the
                # snapshot from when Crumbly proposed it -- defense in
                # depth against anything changing between the proposal
                # and this click (another upload landing in between,
                # the conversation sitting open a while, etc.).
                record = store.lookup(client, division, pending["event_id"], pending["event_date"])
                if record is None:
                    result_msg = (f"Event {pending['event_id']} on {pending['event_date']} "
                                   f"is no longer on file -- nothing to delete.")
                else:
                    store.log_deleted_event(client, record, source="ask_chatbot")
                    store.delete_record(client, division, pending["event_id"], pending["event_date"])
                    result_msg = (f"✅ Event {pending['event_id']} ({pending['event_date']}) "
                                   f"has been permanently deleted from {division}.")
                st.session_state[history_key].append({"role": "assistant", "content": result_msg})
                st.session_state.pop(pending_delete_key, None)
                st.rerun(scope="fragment")
            elif cancel_clicked:
                st.session_state[history_key].append(
                    {"role": "assistant", "content": "Okay, not deleted."})
                st.session_state.pop(pending_delete_key, None)
                st.rerun(scope="fragment")

    question = st.chat_input(f"Ask Crumbly about {division}...")
    if question:
        # A new question means the chef moved on without confirming or
        # cancelling a prior deletion proposal -- treat it as abandoned
        # rather than leaving it to linger and possibly get confirmed
        # later against a conversation that's no longer about it.
        st.session_state.pop(pending_delete_key, None)
        prior_turns = list(st.session_state[history_key])  # BEFORE appending this
        # question -- ask_agent.ask()'s history param is prior COMPLETED
        # turns only, not the current one (that's passed separately).
        st.session_state[history_key].append({"role": "user", "content": question})
        with st.spinner("Looking..."):
            try:
                answer = ask_agent.ask(client, division, question, history=prior_turns)
            except Exception as e:
                answer = f"Something went wrong: {e}"
        delete_match = ask_agent.CONFIRM_DELETE_RE.search(answer)
        if delete_match:
            # Strip the protocol line out of what's actually shown in
            # the chat bubble -- it's a signal for this code, not
            # something the chef should ever see verbatim.
            answer = ask_agent.CONFIRM_DELETE_RE.sub("", answer).strip()
            st.session_state[pending_delete_key] = {
                "event_id": delete_match.group("event_id").strip(),
                "event_date": delete_match.group("event_date").strip(),
            }
        st.session_state[history_key].append({"role": "assistant", "content": answer})
        # scope="fragment", NOT a bare st.rerun() -- st.dialog inherits
        # st.fragment behavior, and Streamlit's own docs are explicit that
        # a bare (full-app-scoped) rerun from inside a dialog closes it,
        # since the dialog-opening function never gets called again
        # during a full-app rerun. This was exactly the "chat closes
        # after every question" bug.
        st.rerun(scope="fragment")


@st.dialog("Ask Crumbly")
def _open_ask_dialog():
    _render_ask_content()


def _render_ask_fab():
    """Floating chat-bubble button, bottom-right corner, present on every
    tab -- opens the Ask chatbot as a popup dialog instead of taking up
    its own tab. Placed once, outside any `with tab_x:` block, since
    Streamlit re-executes the whole script on every rerun regardless of
    which tab is visually selected (see the dish-key crash fix above for
    why that matters) -- so this renders unconditionally every time,
    exactly like a real floating widget should.

    Positioned via CSS targeting the `st-key-<key>` class Streamlit adds
    to a keyed container (a stable, documented mechanism) -- NOT by
    injecting a plain HTML/JS element outside Streamlit's own component
    tree the way the confetti effect does. A plain injected button can't
    trigger a Python rerun/dialog on its own without a full custom
    bidirectional component; a real st.button() inside a styled
    container gets that for free, which is why this approach was chosen
    over the confetti-style one despite both being "just CSS
    positioning" on the surface.
    """
    st.markdown("""
<style>
div.st-key-ask_fab{position:fixed;bottom:24px;right:24px;z-index:9999;width:auto;}
div.st-key-ask_fab button{border-radius:50%;width:56px;height:56px;
font-size:1.5rem;line-height:1;box-shadow:0 2px 10px rgba(0,0,0,0.35);
padding:0;}
/* A fly orbiting the cookie -- purely decorative, pointer-events:none
   so it never intercepts a click meant for the real button underneath.
   This div is a SIBLING of div.st-key-ask_fab in the DOM (rendered by
   the st.markdown call right before that container), not a child of
   it, so it's positioned independently via the same fixed anchor
   (bottom/right) rather than a percentage centered on a parent it
   isn't actually inside. 96px box, fixed 4px from each edge, centers
   it exactly on the 56px button's own center (24px inset + 28px half-
   width = 52px = 4px inset + 48px half-width) -- the fly then sits at
   the box's top-center, 48px from that center, and rotating the WHOLE
   box traces it in a circle around the button (a plain top/left
   keyframe animation can't easily trace a circle, but rotating a
   centered parent can). */
.ask-fly-orbit{position:fixed; bottom:4px; right:4px; width:96px; height:96px;
animation:ask-fly-spin 5s linear infinite; pointer-events:none; z-index:9998;}
.ask-fly-orbit .ask-fly{position:absolute; top:0; left:50%;
transform:translateX(-50%); font-size:1.05rem;}
@keyframes ask-fly-spin{from{transform:rotate(0deg);}to{transform:rotate(360deg);}}
/* The "psst..." bubble stays put (doesn't orbit) so it's always
   readable, and just fades in and out on a slow cycle above the
   button -- like the fly occasionally whispering rather than a
   constant label crowding the corner. */
.ask-psst{position:fixed; bottom:88px; right:18px; z-index:9998;
background:var(--paper-raised); border:1px solid var(--line);
border-radius:12px; padding:4px 10px; font-size:0.78rem;
box-shadow:0 2px 8px rgba(0,0,0,0.2); pointer-events:none;
animation:ask-psst-fade 6s ease-in-out infinite;}
@keyframes ask-psst-fade{0%,60%{opacity:0;}70%,90%{opacity:1;}100%{opacity:0;}}
</style>
<div class="ask-fly-orbit"><span class="ask-fly">🪰</span></div>
<div class="ask-psst">psst&hellip;</div>
""", unsafe_allow_html=True)
    with st.container(key="ask_fab"):
        if st.button("🍪", key="ask_fab_button", help="Ask Crumbly any questions you've got"):
            _open_ask_dialog()


# ---------------------------------------------------------------------------
# Layout — one section per division, side by side, never mixed, plus a
# combined notifications feed.
# ---------------------------------------------------------------------------
st.title("🍽️ Good-to-Go")
st.caption("Upload a contract, and it's compared against whatever's already on "
           "file for that event — every field, every menu item. Good Tidings "
           "and Goodies To Go are kept in completely separate storage; a "
           "contract from one is never compared against the other.")

# The tab label text passed to st.tabs() is a FIXED string,
# "Notifications" -- never "Notifications (N)". st.tabs() re-derives which
# tab is selected from its own argument list on every rerun, and changing
# that list's contents (even just the count in one label) was resetting
# the selection back to the first tab on every click that changed the
# unread count. A count badge rendered directly ON the tab pill was tried
# four different ways (CSS ::before, inserting into the tab, a fixed
# overlay, an absolute overlay) and none rendered reliably or in the
# right place -- dropped. The count is fully reliable in the in-page
# "🔔 Notifications (N)" heading instead (see render_notifications()),
# one click away, with no risk of the DOM-hack failure modes above.
tab_gt, tab_gtg, tab_notifications, tab_allergen = st.tabs(
    store.DIVISIONS + ["Notifications", "🔎 Allergen Scan"], key="main_tabs")
with tab_gt:
    process_division("Good Tidings")
with tab_gtg:
    process_division("Goodies To Go")
with tab_notifications:
    render_notifications()
with tab_allergen:
    render_allergen_scan()

_render_ask_fab()
