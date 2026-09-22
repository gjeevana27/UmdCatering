"""
The agent. This is deliberately NOT a system that auto-approves anything
touching food safety. Its only autonomous actions are:
  1. deciding what to escalate to a human, and how urgently
  2. drafting the escalation note so the reviewer doesn't start from scratch
  3. auto-clearing findings that are unambiguously non-issues (e.g. a menu
     item renamed with an identical ingredient list), so a human's limited
     review time goes to what actually needs it

It never removes an allergen flag on its own authority, and every decision
carries the evidence it was based on -- see EventReview.audit_trail.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import contract_diff
import discrepancy_engine as de
import llm_client
import recall_checker
from parser import load_event, load_contract


DECISION_ESCALATE = "escalate"
DECISION_AUTO_CLEAR = "auto_clear"
DECISION_REVIEW = "flag_for_review"

# Findings of this type/severity are NEVER auto-cleared, LLM or no LLM.
# This is the one hard rule in the whole system.
NEVER_AUTO_CLEAR = {("allergen_conflict", "high"), ("recall_exposure", "high"),
                     ("contract_allergen_added", "high"),
                     ("contract_event_cancelled", "high")}


@dataclass
class Decision:
    finding: "de.Finding"
    decision: str
    reasoning: str
    made_by: str  # "rule" | "llm"


@dataclass
class EventReview:
    event_id: str
    decisions: list = field(default_factory=list)
    audit_trail: list = field(default_factory=list)
    generated_at: str = ""

    def escalations(self):
        return [d for d in self.decisions if d.decision == DECISION_ESCALATE]

    def needs_review(self):
        return [d for d in self.decisions if d.decision == DECISION_REVIEW]

    def auto_cleared(self):
        return [d for d in self.decisions if d.decision == DECISION_AUTO_CLEAR]


def _log(trail, message):
    trail.append(message)


def decide(finding: "de.Finding", trail: list) -> Decision:
    key = (finding.finding_type, finding.severity)

    # Hard rule: any high-severity allergen conflict is ALWAYS escalated.
    # No rule-based override, no LLM override. This is the one place the
    # agent has zero discretion, by design.
    if key in NEVER_AUTO_CLEAR:
        _log(trail, f"[RULE] '{finding.item}' ({finding.finding_type}) is a "
                     f"hard-escalation case -> escalated, no further "
                     f"judgment applied.")
        return Decision(finding, DECISION_ESCALATE,
                         "Safety-critical finding type: always escalated, "
                         "no rule or model override exists for this case.",
                         made_by="rule")

    # Unambiguous, clearly real problems -> escalate by rule, no LLM needed.
    if finding.severity == "high":
        _log(trail, f"[RULE] '{finding.item}' is high severity -> escalated.")
        return Decision(finding, DECISION_ESCALATE,
                         "High-severity finding, escalated by rule.",
                         made_by="rule")

    # Low-severity, non-allergen findings -> auto-clear by rule.
    if finding.severity == "low" and finding.finding_type != "allergen_conflict":
        _log(trail, f"[RULE] '{finding.item}' is low severity, non-allergen "
                     f"-> auto-cleared.")
        return Decision(finding, DECISION_AUTO_CLEAR,
                         "Low-severity, non-safety finding, cleared by rule.",
                         made_by="rule")

    # Everything else (medium severity) is genuinely ambiguous. Try the LLM;
    # if it's not available, default to human review rather than guessing.
    if llm_client.is_llm_available():
        _log(trail, f"[LLM] '{finding.item}' is ambiguous (medium severity) "
                     f"-> asking model to judge.")
        try:
            result = llm_client.judge_ambiguous_finding(
                finding.detail, context=f"finding_type={finding.finding_type}"
            )
            decision_map = {
                "escalate": DECISION_ESCALATE,
                "auto_pass": DECISION_AUTO_CLEAR,
                "flag_low_confidence": DECISION_REVIEW,
            }
            mapped = decision_map.get(result["decision"], DECISION_REVIEW)
            _log(trail, f"[LLM] decision={mapped} reasoning={result['reasoning']!r}")
            return Decision(finding, mapped, result["reasoning"], made_by="llm")
        except Exception as e:
            _log(trail, f"[LLM] call failed ({e}) -> falling back to human review.")
            return Decision(finding, DECISION_REVIEW,
                             "LLM call failed; defaulted to human review rather "
                             "than guessing.", made_by="rule")

    _log(trail, f"[RULE] '{finding.item}' is ambiguous and no LLM is "
                 f"configured -> flagged for human review (safe default).")
    return Decision(finding, DECISION_REVIEW,
                     "Ambiguous finding, no LLM configured: routed to human "
                     "review rather than auto-decided.", made_by="rule")


def _recall_findings(sheet, trail) -> list:
    """
    Runs the live openFDA recall check and converts any exposures into
    Finding objects, so they flow through the exact same decide() logic
    as every other finding type -- a recall exposure is always high
    severity, which means it's always escalated by the existing rule,
    with no separate code path needed.

    A recall-feed failure (network down, API error) is logged to the
    audit trail and treated as zero findings from this check, NOT as a
    silent "no recalls" result -- see the trail entry for how to tell
    the difference between "checked, found nothing" and "couldn't check".
    """
    try:
        exposures = recall_checker.check_recall_exposure(sheet)
    except recall_checker.RecallCheckError as e:
        _log(trail, f"[RECALL CHECK] Live openFDA lookup failed ({e}) -- "
                     f"recall exposure was NOT checked for this review. "
                     f"This is not the same as 'no active recalls found'.")
        return []

    if not exposures:
        _log(trail, "[RECALL CHECK] Live openFDA lookup completed: no "
                     "active Class I/II recalls matched this event's "
                     "ingredients at review time.")
        return []

    findings = []
    for exp in exposures:
        recall = exp["recall"]
        findings.append(de.Finding(
            finding_type="recall_exposure",
            severity="high",
            item=exp["menu_item"],
            detail=(
                f"'{exp['menu_item']}' uses '{exp['ingredient']}', which "
                f"matches an ACTIVE {recall['classification']} recall by "
                f"{recall['recalling_firm']} (reported {recall['report_date']}): "
                f"{recall['reason_for_recall']}"
            ),
        ))
    _log(trail, f"[RECALL CHECK] Live openFDA lookup found "
                 f"{len(findings)} active recall exposure(s).")
    return findings


def review_contract_sheet(contract, sheet, check_live_recalls: bool = False) -> EventReview:
    """
    Same as review_event() but takes already-loaded Contract/ProductionSheet
    objects directly -- used by app.py for uploaded files and extracted
    documents, where there's no event_dir on disk to read from.
    """
    findings = de.run_all_checks(contract, sheet)

    trail = []
    _log(trail, f"Loaded contract and production sheet for event "
                 f"{contract.event_id}. {len(findings)} finding(s) from "
                 f"deterministic checks.")

    if check_live_recalls:
        findings += _recall_findings(sheet, trail)

    decisions = [decide(f, trail) for f in findings]

    return EventReview(
        event_id=contract.event_id,
        decisions=decisions,
        audit_trail=trail,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def review_contract_update(old_contract_path, event_dir,
                            check_live_recalls: bool = False) -> EventReview:
    """
    The change-detection entry point. Compares a saved-off old contract
    (the version the current production sheet was built against) with the
    current contract.json + production_sheet.json in event_dir, and
    returns ONE review combining:
      1. What changed in the contract itself (contract_diff.py)
      2. Whether the CURRENT production sheet is now out of compliance
         with the NEW contract as a result (the normal discrepancy checks,
         run against the updated contract)

    This is deliberately one combined review, not two separate reports --
    the point isn't just "here's what changed," it's "here's what changed,
    AND here's the concrete consequence for the sheet that's already on
    the kitchen board."
    """
    old_contract = load_contract(old_contract_path)
    new_contract, sheet = load_event(event_dir)

    trail = []
    diff_findings = contract_diff.diff_contracts(old_contract, new_contract)
    _log(trail, f"Compared contract versions for event {new_contract.event_id}: "
                 f"{len(diff_findings)} change(s) since the production sheet "
                 f"was built.")

    consistency_findings = de.run_all_checks(new_contract, sheet)
    _log(trail, f"Re-checked current production sheet against the UPDATED "
                 f"contract: {len(consistency_findings)} finding(s).")

    findings = diff_findings + consistency_findings
    if check_live_recalls:
        findings += _recall_findings(sheet, trail)

    decisions = [decide(f, trail) for f in findings]

    return EventReview(
        event_id=new_contract.event_id,
        decisions=decisions,
        audit_trail=trail,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def review_event(event_dir, check_live_recalls: bool = False) -> EventReview:
    contract, sheet = load_event(event_dir)
    return review_contract_sheet(contract, sheet, check_live_recalls=check_live_recalls)


def format_report(review: EventReview) -> str:
    lines = [f"EVENT COMPLIANCE REVIEW -- {review.event_id}",
             f"Generated: {review.generated_at}", ""]

    esc = review.escalations()
    rev = review.needs_review()
    cleared = review.auto_cleared()

    lines.append(f"ESCALATED ({len(esc)}) -- act before the event")
    for d in esc:
        lines.append(f"  [{d.finding.finding_type}] {d.finding.item}")
        lines.append(f"    {d.finding.detail}")
        lines.append(f"    -> {d.reasoning}")
    lines.append("")

    lines.append(f"NEEDS HUMAN REVIEW ({len(rev)}) -- unclear, not urgent")
    for d in rev:
        lines.append(f"  [{d.finding.finding_type}] {d.finding.item}")
        lines.append(f"    {d.finding.detail}")
        lines.append(f"    -> {d.reasoning}")
    lines.append("")

    lines.append(f"AUTO-CLEARED ({len(cleared)}) -- reviewed, no action needed")
    for d in cleared:
        lines.append(f"  [{d.finding.finding_type}] {d.finding.item}: {d.reasoning}")
    lines.append("")

    lines.append("AUDIT TRAIL")
    for entry in review.audit_trail:
        lines.append(f"  {entry}")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    event_dir = sys.argv[1] if len(sys.argv) > 1 else "data/sample_events/event_995"
    live = "--live-recalls" in sys.argv

    if "--compare-contract" in sys.argv:
        idx = sys.argv.index("--compare-contract")
        old_contract_path = sys.argv[idx + 1]
        review = review_contract_update(old_contract_path, event_dir,
                                         check_live_recalls=live)
    else:
        review = review_event(event_dir, check_live_recalls=live)

    print(format_report(review))
