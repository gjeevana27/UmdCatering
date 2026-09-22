"""
Streamlit web app for the Event Compliance Agent.

Run locally:
    streamlit run app.py

Deploy (free):
    Push this repo to GitHub, then either:
      - streamlit.io/cloud -> "New app" -> point at this repo/app.py, or
      - Hugging Face Spaces -> new Space -> SDK: Streamlit -> push this repo
    Set GEMINI_API_KEY as a secret on whichever platform you use if you
    want document/photo extraction and LLM-assisted ambiguous-case review;
    the app runs correctly without it (rule-based mode, see README). Get a
    free key (no credit card) at https://aistudio.google.com/apikey
"""

import json
import sys
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
load_dotenv()  # loads .env from the project root, if present.

import agent  # noqa: E402
import allergen_scan  # noqa: E402
import contract_diff  # noqa: E402
import extract  # noqa: E402
import llm_client  # noqa: E402
import pull_sheet_check  # noqa: E402
from parser import (Contract, ProductionSheet, load_event, load_contract,  # noqa: E402
                     contract_from_extracted, production_sheet_from_extracted,
                     pull_sheet_from_extracted)

st.set_page_config(page_title="Event Compliance Agent", page_icon="🍽️", layout="wide")

SAMPLE_EVENTS_DIR = Path(__file__).resolve().parent / "data" / "sample_events"


def render_report(review: agent.EventReview):
    esc = review.escalations()
    rev = review.needs_review()
    cleared = review.auto_cleared()

    c1, c2, c3 = st.columns(3)
    c1.metric("Escalated", len(esc))
    c2.metric("Needs review", len(rev))
    c3.metric("Auto-cleared", len(cleared))

    if esc:
        st.subheader("🔴 Escalated — act before the event")
        for d in esc:
            with st.container(border=True):
                st.markdown(f"**{d.finding.finding_type.replace('_', ' ')} — {d.finding.item}**")
                st.write(d.finding.detail)
                st.caption(f"→ {d.reasoning}")

    if rev:
        st.subheader("🟡 Needs human review — unclear, not urgent")
        for d in rev:
            with st.container(border=True):
                st.markdown(f"**{d.finding.finding_type.replace('_', ' ')} — {d.finding.item}**")
                st.write(d.finding.detail)
                st.caption(f"→ {d.reasoning}")

    if cleared:
        with st.expander(f"🟢 Auto-cleared ({len(cleared)}) — reviewed, no action needed"):
            for d in cleared:
                st.write(f"**{d.finding.item}**: {d.reasoning}")

    with st.expander("Audit trail"):
        for line in review.audit_trail:
            st.text(line)


def render_allergen_results(dish_name: str, ingredients: list, results: dict):
    st.subheader(f"🔎 {dish_name}")
    if not results:
        st.success("No allergens from the tracked category set detected.")
    else:
        for category, matches in results.items():
            hidden = [m for m in matches if m["match_type"] == "hidden"]
            direct = [m for m in matches if m["match_type"] == "direct"]
            with st.container(border=True):
                st.markdown(f"**{category.replace('_', ' ').upper()}**")
                for m in direct:
                    st.write(f"direct: '{m['source_ingredient']}'")
                for m in hidden:
                    st.warning(f"HIDDEN: '{m['source_ingredient']}' carries "
                               f"{category.replace('_', ' ')} via '{m['matched_term']}'")
    st.caption("Ingredients scanned (" + str(len(ingredients)) + "): " + ", ".join(ingredients))


def confidence_banner(confidence: str, notes: str, label: str):
    if confidence == "high":
        return
    icon = "🟡" if confidence == "medium" else "🔴"
    st.warning(f"{icon} **{label} extraction confidence: {confidence}.** {notes or ''}\n\n"
               f"Review the extracted fields below before running compliance checks — "
               f"a low-confidence extraction feeding into the agent is a garbage-in/"
               f"garbage-out risk, same as any other data-entry error.")


st.title("🍽️ Event Compliance Agent")
st.caption("Cross-checks a production sheet against its contract — menu alignment, "
           "hidden-allergen detection, undeclared allergens, guest count, and (optionally) "
           "live recall exposure. Escalates safety-critical findings, never auto-clears them.")

with st.sidebar:
    st.header("Options")
    check_live_recalls = st.toggle(
        "Check live FDA recall feed", value=False,
        help="Queries api.fda.gov in real time for active recalls matching this "
             "event's ingredients. Requires internet access from wherever this app "
             "is deployed.",
    )
    llm_status = "✅ configured" if llm_client.is_llm_available() else "⚠️ not configured (rule-based mode)"
    st.caption(f"LLM-assisted ambiguous review: {llm_status}")

tab_sample, tab_upload, tab_extract, tab_diff, tab_allergen = st.tabs(
    ["Sample events", "Upload JSON", "Upload photo / PDF", "Compare contract versions",
     "Quick allergen scan"]
)

review = None

with tab_sample:
    # Group by division -- the allergen reference and compliance logic are
    # shared across divisions, this is purely an organizing filter.
    all_ids = sorted(p.name for p in SAMPLE_EVENTS_DIR.iterdir() if p.is_dir())
    divisions = {}
    for eid in all_ids:
        contract_path = SAMPLE_EVENTS_DIR / eid / "contract.json"
        if contract_path.exists():
            div = json.loads(contract_path.read_text()).get("division", "") or "(unassigned)"
        else:
            div = "(unassigned)"
        divisions.setdefault(div, []).append(eid)

    division_choice = st.selectbox("Division", ["All"] + sorted(divisions.keys()))
    sample_ids = all_ids if division_choice == "All" else divisions[division_choice]

    chosen = st.selectbox("Pick a sample event", sample_ids)
    contract_preview_path = SAMPLE_EVENTS_DIR / chosen / "contract.json"
    if contract_preview_path.exists():
        c = json.loads(contract_preview_path.read_text())
        cols = st.columns(4)
        cols[0].caption(f"**Division:** {c.get('division', '—')}")
        cols[1].caption(f"**Date:** {c.get('event_date', '—')}")
        cols[2].caption(f"**Venue:** {c.get('venue', '—')}")
        cols[3].caption(f"**Status:** {c.get('status', '—')}")
    if st.button("Run review", key="run_sample"):
        with st.spinner("Running deterministic checks and decision logic..."):
            review = agent.review_event(SAMPLE_EVENTS_DIR / chosen,
                                         check_live_recalls=check_live_recalls)

with tab_upload:
    st.write("Upload your own `contract.json` and `production_sheet.json` "
             "(same schema as `data/sample_events/`).")
    contract_file = st.file_uploader("contract.json", type="json", key="c_json")
    sheet_file = st.file_uploader("production_sheet.json", type="json", key="s_json")
    if st.button("Run review", key="run_upload") and contract_file and sheet_file:
        with st.spinner("Running deterministic checks and decision logic..."):
            contract_data = json.load(contract_file)
            sheet_data = json.load(sheet_file)
            contract = Contract(**contract_data)
            items = [__import__("parser").MenuItem(**i) for i in sheet_data["menu_items"]]
            sheet = ProductionSheet(event_id=sheet_data["event_id"], menu_items=items,
                                     guest_count=sheet_data["guest_count"],
                                     notes=sheet_data.get("notes", ""))
            review = agent.review_contract_sheet(contract, sheet,
                                                  check_live_recalls=check_live_recalls)

with tab_extract:
    st.write("Upload a **photo or PDF** of a real contract and production sheet. "
             "Uses Gemini's vision to read the document and extract structured data "
             "before running the compliance pipeline.")
    if not llm_client.is_llm_available():
        st.info("Set GEMINI_API_KEY to use this tab — there's no rule-based "
                 "fallback for reading unstructured documents. Free key, no "
                 "credit card: https://aistudio.google.com/apikey")
    else:
        sheet_kind = st.radio(
            "What kind of second document is this?",
            ["Recipe-card production sheet (dish → ingredients)",
             "Kitchen board / pull sheet (\"Secure [item]: qty\" checklist)"],
            help="These need different extraction schemas — a pull sheet is a "
                 "flat prep checklist grouped by event #, not a dish-by-dish list.",
        )
        contract_upload = st.file_uploader("Contract (photo or PDF)",
                                            type=["png", "jpg", "jpeg", "pdf"], key="c_img")
        sheet_upload = st.file_uploader("Second document (photo or PDF)",
                                         type=["png", "jpg", "jpeg", "pdf"], key="s_img")
        if st.button("Extract and run review", key="run_extract") and contract_upload and sheet_upload:
            with st.spinner("Reading documents with Gemini..."):
                contract = sheet = pull_sheet = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(contract_upload.name).suffix) as tf:
                        tf.write(contract_upload.getvalue())
                        c_path = tf.name
                    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(sheet_upload.name).suffix) as tf:
                        tf.write(sheet_upload.getvalue())
                        s_path = tf.name

                    c_data = extract.extract_contract(c_path)
                    contract, c_conf, c_notes = contract_from_extracted(c_data)
                    confidence_banner(c_conf, c_notes, "Contract")
                    with st.expander("Extracted contract (review before trusting)"):
                        st.json(c_data)

                    if sheet_kind.startswith("Recipe-card"):
                        s_data = extract.extract_production_sheet(s_path)
                        sheet, s_conf, s_notes = production_sheet_from_extracted(s_data)
                        confidence_banner(s_conf, s_notes, "Production sheet")
                        with st.expander("Extracted production sheet (review before trusting)"):
                            st.json(s_data)
                    else:
                        # Pull sheets can contain multiple events per photo;
                        # match against the extracted contract's event_id if
                        # more than one block came back.
                        blocks = extract.extract_pull_sheet(s_path)
                        chosen = blocks[0]
                        if len(blocks) > 1:
                            matches = [b for b in blocks if str(b.get("event_id")) == str(contract.event_id)]
                            if matches:
                                chosen = matches[0]
                            st.caption(f"Found {len(blocks)} event blocks on this page — "
                                       f"using event #{chosen.get('event_id')}"
                                       f"{' (matched to contract)' if matches else ' (first found — did not match contract event_id, check manually)'}")
                        pull_sheet, ps_conf, ps_notes = pull_sheet_from_extracted(chosen)
                        confidence_banner(ps_conf, ps_notes, "Pull sheet")
                        with st.expander("Extracted pull sheet (review before trusting)"):
                            st.json(chosen)

                except extract.ExtractionError as e:
                    st.error(f"Extraction failed: {e}")

                if contract and sheet:
                    with st.spinner("Running deterministic checks and decision logic..."):
                        review = agent.review_contract_sheet(
                            contract, sheet, check_live_recalls=check_live_recalls
                        )
                elif contract and pull_sheet:
                    st.subheader("Pull sheet coverage check")
                    st.caption("Fuzzy text match — every gap needs a human glance before "
                               "assuming it's really missing.")
                    gaps = pull_sheet_check.check_pull_sheet_coverage(contract, pull_sheet)
                    if not gaps:
                        st.success("Every contracted item has some matching line on the pull sheet.")
                    for g in gaps:
                        with st.container(border=True):
                            st.markdown(f"**{g.item}**")
                            st.write(g.detail)

with tab_diff:
    st.write("Compares an **old contract snapshot** (the version the current "
             "production sheet was built against) with the **current contract**, "
             "then re-checks the current production sheet against what changed. "
             "This is the tool for the actual failure mode that causes problems: "
             "a client changes something after the sheet is already printed and "
             "hand-annotated, and nobody re-checks it.")

    diff_mode = st.radio("Source", ["Built-in demo (event 2201)", "Upload my own"],
                          horizontal=True)

    if diff_mode == "Built-in demo (event 2201)":
        st.caption("Simulates: guest count grows from 60→68, shrimp skewers "
                    "swapped for chicken after a shellfish allergy is disclosed, "
                    "and a new shellfish-free guarantee is added — but the "
                    "production sheet on the board still reflects the old contract.")
        if st.button("Run comparison", key="run_diff_demo"):
            with st.spinner("Diffing contract versions and re-checking the sheet..."):
                event_dir = SAMPLE_EVENTS_DIR / "event_2201"
                review = agent.review_contract_update(
                    event_dir / "contract_v1.json", event_dir,
                    check_live_recalls=check_live_recalls,
                )
    else:
        old_c = st.file_uploader("OLD contract.json (what the sheet was built from)",
                                  type="json", key="old_c")
        new_c = st.file_uploader("NEW/current contract.json", type="json", key="new_c")
        sheet_f = st.file_uploader("Current production_sheet.json", type="json", key="diff_sheet")
        if st.button("Run comparison", key="run_diff_upload") and old_c and new_c and sheet_f:
            with st.spinner("Diffing contract versions and re-checking the sheet..."):
                old_contract = Contract(**json.load(old_c))
                new_contract = Contract(**json.load(new_c))
                sheet_data = json.load(sheet_f)
                items = [__import__("parser").MenuItem(**i) for i in sheet_data["menu_items"]]
                sheet = ProductionSheet(event_id=sheet_data["event_id"], menu_items=items,
                                         guest_count=sheet_data["guest_count"],
                                         notes=sheet_data.get("notes", ""))
                diff_findings = contract_diff.diff_contracts(old_contract, new_contract)
                st.info(f"{len(diff_findings)} change(s) detected in the contract itself.")
                # Build the same combined review agent.review_contract_update produces,
                # without needing files on disk.
                import discrepancy_engine as de
                trail = []
                consistency_findings = de.run_all_checks(new_contract, sheet)
                findings = diff_findings + consistency_findings
                decisions = [agent.decide(f, trail) for f in findings]
                review = agent.EventReview(event_id=new_contract.event_id,
                                            decisions=decisions, audit_trail=trail,
                                            generated_at="")

with tab_allergen:
    st.write("Check ONE dish's ingredients for allergens directly — no contract or "
             "production sheet needed. Text is instant and free (fully local, no API "
             "call); photo/PDF uses Gemini to read the ingredients first.")

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
            render_allergen_results("pasted ingredient list", ingredients, results)
    else:
        if not llm_client.is_llm_available():
            st.info("Set GEMINI_API_KEY to scan a photo — there's no rule-based "
                     "fallback for reading an image. Free key, no credit card: "
                     "https://aistudio.google.com/apikey")
        else:
            photo = st.file_uploader("Photo or PDF of a dish / recipe card",
                                      type=["png", "jpg", "jpeg", "pdf"], key="allergen_img")
            if st.button("Scan", key="scan_photo") and photo:
                with st.spinner("Reading the dish with Gemini..."):
                    try:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(photo.name).suffix) as tf:
                            tf.write(photo.getvalue())
                            p_path = tf.name
                        data = extract.extract_ingredient_list(p_path)
                        confidence_banner(data.get("_confidence", "unknown"),
                                           data.get("_confidence_notes", ""), "Ingredient list")
                        with st.expander("Extracted ingredients (review before trusting)"):
                            st.json(data)
                        ingredients = data.get("ingredients", [])
                        results = allergen_scan.scan_text_ingredients(ingredients)
                        render_allergen_results(data.get("dish_name", "scanned dish"),
                                                 ingredients, results)
                    except extract.ExtractionError as e:
                        st.error(f"Extraction failed: {e}")

if review is not None:
    st.divider()
    render_report(review)

st.divider()
st.caption("This is a decision-support tool, not a food-safety system. Every finding "
           "here is a recommendation for a human reviewer — the agent never approves "
           "a menu or clears an allergen conflict on its own authority.")
