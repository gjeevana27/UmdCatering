"""
Detects what changed between two versions of the same event's contract, and
converts each real change into a discrepancy_engine.Finding so it goes
through the exact same escalate/review/auto-clear decision logic as every
other finding in the system.

This exists for one specific failure mode: a production sheet gets printed
and hand-annotated against contract version 1, then the client changes
something (guest count, a menu swap, a new allergy requirement) before the
event, and nobody re-checks the printed sheet against the update. The
sheet itself doesn't know it's stale. This module is what would catch that.

Two contract states are compared: `old` (the version the current production
sheet was built against) and `new` (the current/live version). Nothing here
assumes which one is "correct" -- that's not a fact this module can know.
It only reports what's different and how urgent that difference is.
"""

from discrepancy_engine import Finding


# A guest-count change of more than this fraction is treated as high
# severity (quantities almost certainly need to be redone), below it as
# medium (still needs updating, but less likely to blow out prep volume).
GUEST_COUNT_HIGH_SEVERITY_THRESHOLD = 0.15


def _pct_change(old_val, new_val) -> float:
    if old_val == 0:
        return float("inf") if new_val != 0 else 0.0
    return abs(new_val - old_val) / old_val


def diff_contracts(old, new) -> list:
    """
    old, new: parser.Contract objects for the same event_id at two points
    in time.
    Returns a list[Finding] -- one per real change detected. An unchanged
    contract returns an empty list.
    """
    if old.event_id != new.event_id:
        raise ValueError(
            f"Refusing to diff contracts from different events "
            f"({old.event_id} vs {new.event_id}) -- pass two versions of "
            f"the same event."
        )

    findings = []

    # --- Guest count ---
    if old.guest_count != new.guest_count:
        diff = new.guest_count - old.guest_count
        pct = _pct_change(old.guest_count, new.guest_count)
        severity = "high" if pct > GUEST_COUNT_HIGH_SEVERITY_THRESHOLD else "medium"
        findings.append(Finding(
            finding_type="contract_guest_count_changed",
            severity=severity,
            item="guest_count",
            detail=(
                f"Guest count changed from {old.guest_count} to "
                f"{new.guest_count} ({'+' if diff > 0 else ''}{diff}, "
                f"{pct:.0%}) since the production sheet was last built. "
                f"Prep quantities on the current sheet reflect the old count."
            ),
        ))

    # --- Menu items: added / removed ---
    old_items = {i.strip().lower(): i for i in old.contracted_menu_items}
    new_items = {i.strip().lower(): i for i in new.contracted_menu_items}

    for key, name in new_items.items():
        if key not in old_items:
            findings.append(Finding(
                finding_type="contract_item_added",
                severity="high",
                item=name,
                detail=(
                    f"'{name}' was added to the contract since the "
                    f"production sheet was built. It will not be on the "
                    f"current sheet at all."
                ),
            ))
    for key, name in old_items.items():
        if key not in new_items:
            findings.append(Finding(
                finding_type="contract_item_removed",
                severity="high",
                item=name,
                detail=(
                    f"'{name}' was removed from the contract since the "
                    f"production sheet was built. The current sheet may "
                    f"still be planning to prep it -- check for wasted "
                    f"ingredients, and if it was removed for an allergy "
                    f"reason, confirm the kitchen knows."
                ),
            ))

    # --- Allergen guarantees: the single most safety-critical change type.
    # Adding a new guarantee is hard-escalated in agent.py's NEVER_AUTO_CLEAR
    # the same way a live allergen_conflict is -- if the client adds "must
    # be shellfish-free" after the sheet was printed, that cannot be missed.
    old_free = set(old.guaranteed_allergen_free)
    new_free = set(new.guaranteed_allergen_free)

    for category in new_free - old_free:
        findings.append(Finding(
            finding_type="contract_allergen_added",
            severity="high",
            item=category,
            detail=(
                f"The contract now guarantees the event is "
                f"{category.replace('_', ' ')}-free -- this guarantee "
                f"did NOT exist when the current production sheet was "
                f"built. Every dish on the current sheet needs to be "
                f"re-scanned against this category before the event."
            ),
        ))
    for category in old_free - new_free:
        findings.append(Finding(
            finding_type="contract_allergen_removed",
            severity="medium",
            item=category,
            detail=(
                f"The contract no longer guarantees "
                f"{category.replace('_', ' ')}-free (it did when the "
                f"current production sheet was built). Confirm this is "
                f"an intentional relaxation, not a dropped requirement."
            ),
        ))

    # --- Event date (reschedule) ---
    if old.event_date and new.event_date and old.event_date != new.event_date:
        findings.append(Finding(
            finding_type="contract_date_changed",
            severity="high",
            item="event_date",
            detail=(
                f"Event date changed from {old.event_date} to "
                f"{new.event_date} since the production sheet was built -- "
                f"this is a reschedule, not a minor edit. Confirm delivery/"
                f"prep timing is redone for the new date, not just quantities."
            ),
        ))

    # --- Event time ---
    if old.event_time and new.event_time and old.event_time != new.event_time:
        findings.append(Finding(
            finding_type="contract_time_changed",
            severity="medium",
            item="event_time",
            detail=(
                f"Event time changed from '{old.event_time}' to "
                f"'{new.event_time}' since the production sheet was built."
            ),
        ))

    # --- Venue ---
    if old.venue and new.venue and old.venue != new.venue:
        findings.append(Finding(
            finding_type="contract_venue_changed",
            severity="high",
            item="venue",
            detail=(
                f"Venue changed from '{old.venue}' to '{new.venue}' since "
                f"the production sheet was built -- confirm delivery "
                f"address, equipment/kitchen-access instructions, and "
                f"drive time are all updated, not just the food quantities."
            ),
        ))

    # --- Cancellation: the other hard-escalate case alongside a newly
    # added allergen guarantee. If status flips to cancelled, every other
    # finding on this event is moot -- but the agent still reports them
    # (it doesn't suppress checks based on status), it just makes sure
    # this one can never be missed or auto-cleared.
    if old.status != new.status and new.status.lower() == "cancelled":
        findings.append(Finding(
            finding_type="contract_event_cancelled",
            severity="high",
            item="status",
            detail=(
                f"Event {new.event_id} status changed to CANCELLED (was "
                f"'{old.status}') since the production sheet was built. "
                f"Stop all prep for this event and confirm with the kitchen."
            ),
        ))
    elif old.status != new.status:
        findings.append(Finding(
            finding_type="contract_status_changed",
            severity="medium",
            item="status",
            detail=(
                f"Event status changed from '{old.status}' to "
                f"'{new.status}' since the production sheet was built."
            ),
        ))

    # --- Price ---
    if old.price_per_head != new.price_per_head:
        findings.append(Finding(
            finding_type="contract_price_changed",
            severity="low",
            item="price_per_head",
            detail=(
                f"Price per head changed from ${old.price_per_head:.2f} to "
                f"${new.price_per_head:.2f}. No effect on kitchen prep; "
                f"flagged for billing awareness only."
            ),
        ))

    # --- Special instructions: free text, so flagged as medium (ambiguous)
    # rather than diffed word-by-word -- a rewording and a genuinely new
    # instruction look the same to a naive text diff, and this is exactly
    # the kind of case worth routing to the LLM judgment step when
    # available, or a human when it isn't.
    if old.special_instructions.strip() != new.special_instructions.strip():
        findings.append(Finding(
            finding_type="contract_instructions_changed",
            severity="medium",
            item="special_instructions",
            detail=(
                f"Special instructions changed.\n"
                f"    Previously: {old.special_instructions!r}\n"
                f"    Now: {new.special_instructions!r}"
            ),
        ))

    return findings
