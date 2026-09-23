"""
The agentic layer of the contract tracker. contract_store.diff_records()
only establishes facts ("guest_count went from 60 to 68"); this module is
the one place that decides what each fact means and what should happen
about it. That split is what keeps every decision auditable back to
either a rule or a specific model call, instead of one opaque "AI found
some changes" step.

Two possible decisions per change -- deliberately no silent third tier.
Every change stays visible; nothing gets auto-logged away as "too minor
to matter." A small guest-count bump can still mean real prep and cost
changes for a small catering operation, so this system never decides on
your behalf that something's too small to see.
  - ESCALATE: needs the chef's attention now, before the event.
  - REVIEW: worth a glance -- less urgent than ESCALATE, but still shown,
    not hidden.

Hard rules come first and are never overridden by the model. Genuinely
ambiguous cases (does a reworded special-instructions line actually change
anything operationally?) go to Gemini for a one-line judgment call.
Everything else falls back to a safe default (REVIEW) when no LLM is
configured -- this tool never guesses at a judgment call it can't
actually make.

The behavior that makes this genuinely agentic rather than just a rules
engine: the agent doesn't fully trust its own upstream extraction. When
the document's extraction confidence was low, every REVIEW gets promoted
to ESCALATE -- a "probably not urgent" conclusion drawn from a shaky
reading isn't one this system is willing to sit on quietly.
"""

from dataclasses import dataclass

import allergen_reference
import llm_client

DECISION_ESCALATE = "escalate"
DECISION_REVIEW = "review"

# A guest-count or menu-quantity change bigger than this fraction is
# treated as ESCALATE rather than REVIEW -- both are always shown, this
# threshold only decides how urgently, never whether.
SIGNIFICANT_QUANTITY_CHANGE = 0.15

# Field changes that always matter regardless of magnitude -- logistics
# facts where even a "small" change has real operational consequences.
_ALWAYS_ESCALATE_FIELDS = {"location", "event_time"}


@dataclass
class ChangeDecision:
    change: object          # the original FieldChange or MenuChange
    decision: str
    reasoning: str
    made_by: str            # "rule" | "llm"


def _pct_change(old_val, new_val) -> float:
    try:
        old_num = float(old_val)
        new_num = float(new_val)
    except (TypeError, ValueError):
        return float("inf")  # non-numeric -- can't assess magnitude, treat as significant
    if old_num == 0:
        return float("inf") if new_num != 0 else 0.0
    return abs(new_num - old_num) / old_num


def _decide_field_change(change) -> ChangeDecision:
    f = change.field

    if getattr(change, "likely_extraction_miss", False):
        # Takes priority over every other rule and over the LLM judge --
        # a field that had real content and now reads blank is far more
        # likely a bad extraction on this pass than a genuine edit (a
        # contract almost never gets amended down to "nothing"), and this
        # needs to escalate reliably every time, not depend on the LLM
        # judge happening to notice or a human remembering to be
        # suspicious of it.
        return ChangeDecision(
            change, DECISION_ESCALATE,
            f"{f.replace('_', ' ').title()} had content before and now "
            f"reads blank -- more likely a missed read on this extraction "
            f"than a real edit. Verify against the source document before "
            f"trusting this as an actual change.",
            made_by="rule",
        )

    if f in _ALWAYS_ESCALATE_FIELDS:
        return ChangeDecision(
            change, DECISION_ESCALATE,
            f"{f.replace('_', ' ').title()} changes always need the chef's "
            f"attention -- delivery/logistics depend on it directly.",
            made_by="rule",
        )

    if f == "guest_count":
        pct = _pct_change(change.old_value, change.new_value)
        if pct > SIGNIFICANT_QUANTITY_CHANGE:
            return ChangeDecision(
                change, DECISION_ESCALATE,
                f"Guest count changed by {pct:.0%} -- likely enough to "
                f"affect prep volume, escalated by rule.",
                made_by="rule",
            )
        return ChangeDecision(
            change, DECISION_REVIEW,
            f"Guest count changed by {pct:.0%} -- still worth a glance "
            f"even at this size, since headcount changes affect cost and "
            f"prep quantities directly.",
            made_by="rule",
        )

    # event_type, time_desc_notes, and anything else scalar: genuinely
    # ambiguous free text. A reworded field and a substantively different
    # one can look identical to a plain diff -- this is exactly the kind
    # of case that needs judgment, not a rule.
    if llm_client.is_llm_available():
        try:
            result = llm_client.judge_contract_change(
                field_label=f.replace('_', ' '),
                old_value=change.old_value,
                new_value=change.new_value,
                context="event contract field change",
            )
            decision = (DECISION_ESCALATE if result.get("decision") == "escalate"
                        else DECISION_REVIEW)
            return ChangeDecision(change, decision, result.get("reasoning", ""),
                                   made_by="llm")
        except Exception as e:
            return ChangeDecision(
                change, DECISION_REVIEW,
                f"LLM judgment failed ({e}); defaulted to review rather "
                f"than guessing.", made_by="rule",
            )

    return ChangeDecision(
        change, DECISION_REVIEW,
        "Ambiguous text change, no LLM configured: routed to review "
        "rather than auto-decided.", made_by="rule",
    )


def _decide_menu_change(change) -> ChangeDecision:
    if change.change_type in ("added", "removed"):
        return ChangeDecision(
            change, DECISION_ESCALATE,
            f"A menu item was {change.change_type} entirely -- the kitchen "
            f"needs to know regardless of magnitude, escalated by rule.",
            made_by="rule",
        )

    # "changed" -- qty/unit and/or description differ (see
    # contract_store.diff_records for how `detail` is built). A qty/unit
    # change is a rule-decidable prep-volume fact -- always escalate,
    # rather than trying to safely apply a magnitude threshold to
    # inconsistently-formatted quantity strings like "40.00-EACH". A
    # description-only wording change is genuinely ambiguous -- could be
    # a typo fix or a real ingredient change with allergen/prep
    # implications -- so that case goes to judgment instead.
    if "qty/unit" in change.detail:
        return ChangeDecision(
            change, DECISION_ESCALATE,
            "Quantity or unit changed on this item -- escalated by rule "
            "rather than assumed routine.", made_by="rule",
        )

    # Ground the description judgment against the kitchen's own allergen
    # reference BEFORE asking the model to reason about it freely -- this
    # runs on every single description change, unconditionally, unlike an
    # LLM tool call the model might or might not choose to make. If a
    # scan of the old vs. new text finds a different set of allergen
    # categories, that's a direct match against known terms, not a
    # judgment call, so it's decided by rule with no LLM involved at all.
    old_categories = {c for c, _, _ in allergen_reference.scan_ingredient(change.old_description)}
    new_categories = {c for c, _, _ in allergen_reference.scan_ingredient(change.new_description)}
    added = new_categories - old_categories
    removed = old_categories - new_categories
    if added or removed:
        parts = []
        if added:
            parts.append(f"adds {', '.join(sorted(added))}")
        if removed:
            parts.append(f"removes {', '.join(sorted(removed))}")
        return ChangeDecision(
            change, DECISION_ESCALATE,
            f"Description change {' and '.join(parts)}, per a direct match "
            f"against the kitchen's allergen reference -- escalated by "
            f"rule, not LLM judgment, since this is a literal term match, "
            f"not a matter of interpretation.",
            made_by="rule",
        )

    if llm_client.is_llm_available():
        try:
            result = llm_client.judge_contract_change(
                field_label="description",
                old_value=change.old_description,
                new_value=change.new_description,
                context="menu item description change",
                item_label=change.recipe_name,
            )
            decision = (DECISION_ESCALATE if result.get("decision") == "escalate"
                        else DECISION_REVIEW)
            return ChangeDecision(change, decision, result.get("reasoning", ""),
                                   made_by="llm")
        except Exception as e:
            return ChangeDecision(
                change, DECISION_REVIEW,
                f"LLM judgment failed ({e}); defaulted to review.",
                made_by="rule",
            )

    return ChangeDecision(
        change, DECISION_REVIEW,
        "Ambiguous description change, no LLM configured: routed to "
        "review rather than auto-decided.", made_by="rule",
    )


def evaluate_changes(field_changes, menu_changes, extraction_confidence: str = "high") -> dict:
    """
    Runs every field/menu change through the decision layer and returns
    {"escalate": [...], "review": [...]} of ChangeDecision. Every change
    lands in one of these two -- nothing is dropped or hidden.

    extraction_confidence: the _confidence value from the extraction that
    produced the NEW record being compared. When it's "low", every REVIEW
    is promoted to ESCALATE -- the agent doesn't sit quietly on a
    "probably fine" conclusion drawn from a reading it wasn't confident in.
    """
    decisions = [_decide_field_change(c) for c in field_changes]
    decisions += [_decide_menu_change(c) for c in menu_changes]

    if extraction_confidence == "low":
        for d in decisions:
            if d.decision == DECISION_REVIEW:
                d.decision = DECISION_ESCALATE
                d.reasoning = (
                    d.reasoning + " (Promoted to escalate: extraction "
                    "confidence on this upload was low, so a 'less urgent' "
                    "conclusion isn't trusted on its own.)"
                )
                d.made_by = "rule"

    return {
        "escalate": [d for d in decisions if d.decision == DECISION_ESCALATE],
        "review": [d for d in decisions if d.decision == DECISION_REVIEW],
    }
