"""
Checks whether a kitchen-board pull sheet (parser.PullSheet -- the
"Secure [item]: qty" checklist format) actually covers what the contract
requires.

This is deliberately a fuzzy, lower-confidence check compared to
discrepancy_engine.py's menu alignment: a pull sheet's line items are
prep-task phrasing ("Secure Gluten Free Bread", "Bahn Mi Roll") that rarely
matches a contract's dish names ("Assorted Sandwich Platter") word for
word. A missed match here is a genuine finding worth a human's eyes, but
it's inherently noisier than an exact-set comparison, which is why every
finding from this module comes out at "medium" severity (ambiguous) rather
than "high" -- it goes through the same LLM-assisted review step as any
other ambiguous finding when one's configured, and to a human otherwise.
"""

import re

from discrepancy_engine import Finding

_STOPWORDS = {"the", "a", "an", "with", "and", "for", "of", "on", "in"}


def _normalize(text: str) -> set:
    words = re.findall(r"[a-z]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _best_overlap(contract_item: str, secure_items: list) -> float:
    """Returns the best word-overlap ratio (0-1) between the contract item
    name and any secure_items entry, checked against both the item's name
    and its notes field (since a component is often named in the notes,
    e.g. a sandwich's bread type)."""
    target = _normalize(contract_item)
    if not target:
        return 0.0
    best = 0.0
    for item in secure_items:
        candidate = _normalize(item.name + " " + item.notes)
        if not candidate:
            continue
        overlap = len(target & candidate) / len(target)
        best = max(best, overlap)
    return best


def check_pull_sheet_coverage(contract, pull_sheet, threshold: float = 0.5) -> list:
    """
    contract: parser.Contract
    pull_sheet: parser.PullSheet
    threshold: minimum word-overlap ratio to count a contract item as
    "covered" by some line on the pull sheet. 0.5 means at least half the
    meaningful words in the contract item name show up somewhere in a
    secure_items entry -- deliberately loose, because these two documents
    are never going to use identical phrasing.

    Returns list[Finding], one per contracted item with no matching pull-
    sheet coverage above threshold.
    """
    findings = []
    for item_name in contract.contracted_menu_items:
        overlap = _best_overlap(item_name, pull_sheet.secure_items)
        if overlap < threshold:
            findings.append(Finding(
                finding_type="pull_sheet_gap",
                severity="medium",
                item=item_name,
                detail=(
                    f"'{item_name}' is in the contract, but no line on the "
                    f"pull sheet for event {pull_sheet.event_id} appears to "
                    f"cover it (best match: {overlap:.0%} word overlap). "
                    f"This is a fuzzy text match, not an exact one -- "
                    f"confirm by eye before assuming it's actually missing."
                ),
            ))
    return findings
