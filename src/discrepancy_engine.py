"""
Deterministic checks the agent runs before it has to make any judgment
calls. Kept separate from agent.py on purpose: these are facts (a menu item
is on the sheet but not the contract; an ingredient matches a guaranteed-free
allergen category), not decisions (whether that fact is worth escalating).
Separating "what did we find" from "what should we do about it" is what
makes the agent's decisions auditable instead of a black box.
"""

from dataclasses import dataclass, field

from allergen_reference import scan_recipe


@dataclass
class Finding:
    finding_type: str          # "menu_mismatch" | "allergen_conflict" | "allergen_undeclared" | "guest_count_mismatch"
    severity: str              # "high" | "medium" | "low"
    item: str
    detail: str
    evidence: list = field(default_factory=list)


def check_menu_alignment(contract, sheet) -> list:
    """Items on the production sheet that were never contracted, and
    contracted items that never made it onto the sheet."""
    findings = []
    contracted = {name.strip().lower() for name in contract.contracted_menu_items}
    produced = {item.name.strip().lower(): item.name for item in sheet.menu_items}

    for produced_lower, original_name in produced.items():
        if produced_lower not in contracted:
            findings.append(Finding(
                finding_type="menu_mismatch",
                severity="medium",
                item=original_name,
                detail=f"'{original_name}' appears on the production sheet "
                       f"but was not in the signed contract's item list.",
            ))

    for contracted_name in contract.contracted_menu_items:
        if contracted_name.strip().lower() not in produced:
            findings.append(Finding(
                finding_type="menu_mismatch",
                severity="high",
                item=contracted_name,
                detail=f"'{contracted_name}' is in the signed contract but "
                       f"missing from the production sheet entirely.",
            ))
    return findings


def check_allergen_conflicts(contract, sheet) -> list:
    """The highest-severity check: does any dish contain something from a
    category the client was contractually guaranteed to be free of."""
    findings = []
    guaranteed_free = set(contract.guaranteed_allergen_free)
    if not guaranteed_free:
        return findings

    for item in sheet.menu_items:
        scan = scan_recipe(item.ingredients)
        for category, matches in scan.items():
            if category in guaranteed_free:
                for match_type, term, source_ingredient in matches:
                    findings.append(Finding(
                        finding_type="allergen_conflict",
                        severity="high",
                        item=item.name,
                        detail=(
                            f"'{item.name}' contains '{source_ingredient}', "
                            f"which is a {'hidden' if match_type == 'hidden' else 'direct'} "
                            f"source of {category.replace('_', ' ')} -- "
                            f"a category this event's contract guarantees is absent."
                        ),
                        evidence=[source_ingredient, term, category],
                    ))
    return findings


def check_undeclared_allergens(sheet) -> list:
    """Allergens present in the ingredient list that the recipe card itself
    never declared -- a kitchen-floor labeling gap, independent of what the
    client was promised."""
    findings = []
    for item in sheet.menu_items:
        scan = scan_recipe(item.ingredients)
        declared = {a.lower() for a in item.declared_allergens}
        for category, matches in scan.items():
            if category.lower() not in declared:
                hidden_matches = [m for m in matches if m[0] == "hidden"]
                severity = "medium" if hidden_matches else "low"
                sample = matches[0]
                findings.append(Finding(
                    finding_type="allergen_undeclared",
                    severity=severity,
                    item=item.name,
                    detail=(
                        f"'{item.name}' contains {category.replace('_', ' ')} "
                        f"(via '{sample[2]}') but the recipe card doesn't list "
                        f"{category.replace('_', ' ')} as a declared allergen."
                    ),
                    evidence=[sample[2], category],
                ))
    return findings


def check_guest_count(contract, sheet) -> list:
    findings = []
    if contract.guest_count != sheet.guest_count:
        diff = sheet.guest_count - contract.guest_count
        findings.append(Finding(
            finding_type="guest_count_mismatch",
            severity="medium" if abs(diff) > 5 else "low",
            item="guest_count",
            detail=(
                f"Contract guarantees {contract.guest_count} guests; "
                f"production sheet is built for {sheet.guest_count} "
                f"({'+' if diff > 0 else ''}{diff})."
            ),
        ))
    return findings


def run_all_checks(contract, sheet) -> list:
    findings = []
    findings += check_menu_alignment(contract, sheet)
    findings += check_allergen_conflicts(contract, sheet)
    findings += check_undeclared_allergens(sheet)
    findings += check_guest_count(contract, sheet)
    return findings
