"""
Real-time recall exposure check.

Queries the public openFDA Food Enforcement API (api.fda.gov) for currently
active recalls and cross-references them against the ingredients used in an
event's recipes. This is the one genuinely real-time data source in the
project -- everything else (contract, production sheet) is static per-event
data, but recalls change day to day, so this check is only as good as the
moment it's run, which is exactly why it belongs in the agent's pipeline
rather than a one-time manual lookup.

No API key required -- openFDA's public endpoints are open access, rate
limited to 1,000 requests/day without a key (240/min with a free key, see
https://open.fda.gov/apis/authentication/).

This module makes a live network call. It has no effect on the deterministic
agent.py / discrepancy_engine.py pipeline unless you explicitly call
check_recall_exposure() -- see app.py for how it's wired into the demo.
"""

import requests

OPENFDA_ENFORCEMENT_URL = "https://api.fda.gov/food/enforcement.json"

# Only Class I (reasonable probability of serious health consequences or
# death) and Class II (temporary or reversible health consequences) recalls
# are treated as exposure-worthy for this check. Class III (unlikely to
# cause health consequences) is logged but not escalated.
ESCALATE_CLASSIFICATIONS = {"Class I", "Class II"}


class RecallCheckError(Exception):
    """Raised when the openFDA API can't be reached or returns an error."""
    pass


def search_active_recalls(ingredient_term: str, limit: int = 5) -> list:
    """
    Search openFDA for recalls whose product description or reason for
    recall mentions the given ingredient term, ongoing recalls only
    (status:Ongoing).

    Returns a list of dicts: [{classification, product_description,
    reason_for_recall, recalling_firm, report_date, status}, ...]

    Raises RecallCheckError on network failure or a malformed API response
    -- callers should decide whether that's fatal or just skip the check,
    since a recall-feed outage shouldn't block the rest of the review.
    """
    query = f'(product_description:"{ingredient_term}" OR reason_for_recall:"{ingredient_term}") AND status:"Ongoing"'
    params = {"search": query, "limit": limit}

    try:
        response = requests.get(OPENFDA_ENFORCEMENT_URL, params=params, timeout=10)
    except requests.RequestException as e:
        raise RecallCheckError(f"Could not reach openFDA: {e}") from e

    if response.status_code == 404:
        # openFDA returns 404 (not 200-with-empty-results) when nothing matches
        return []
    if response.status_code != 200:
        raise RecallCheckError(
            f"openFDA returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        data = response.json()
        results = data.get("results", [])
    except ValueError as e:
        raise RecallCheckError(f"Could not parse openFDA response: {e}") from e

    return [
        {
            "classification": r.get("classification", "Unknown"),
            "product_description": r.get("product_description", ""),
            "reason_for_recall": r.get("reason_for_recall", ""),
            "recalling_firm": r.get("recalling_firm", ""),
            "report_date": r.get("report_date", ""),
            "status": r.get("status", ""),
        }
        for r in results
    ]


def check_recall_exposure(sheet) -> list:
    """
    Cross-reference every ingredient on the production sheet's menu items
    against active openFDA recalls.

    Returns a list of dicts: {menu_item, ingredient, recall} for every
    ingredient that matches an active Class I/II recall. This is
    intentionally NOT wired into discrepancy_engine.Finding directly --
    see agent.py's check_live_recalls() for how it's converted into the
    same Finding/Decision shape so it goes through the identical
    escalate/review/clear logic as everything else.

    A network failure here raises RecallCheckError rather than silently
    returning an empty list -- a recall check that silently fails open
    (reports "nothing found" when it actually couldn't check) is worse
    than one that visibly fails, given what's at stake.
    """
    exposures = []
    # De-duplicate ingredient terms across all menu items before querying,
    # so a "chicken" ingredient used in 4 dishes is one API call, not 4.
    seen_terms = set()

    for item in sheet.menu_items:
        for ingredient in item.ingredients:
            # Use a simplified search term: openFDA free-text search works
            # best on short, generic terms rather than full ingredient
            # phrases like "romaine lettuce, chopped".
            term = ingredient.split(",")[0].strip()
            if term.lower() in seen_terms or len(term) < 3:
                continue
            seen_terms.add(term.lower())

            recalls = search_active_recalls(term)
            for recall in recalls:
                if recall["classification"] in ESCALATE_CLASSIFICATIONS:
                    exposures.append({
                        "menu_item": item.name,
                        "ingredient": ingredient,
                        "recall": recall,
                    })

    return exposures
