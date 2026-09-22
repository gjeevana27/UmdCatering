"""
Standalone allergen scanner: check ONE dish's ingredients for allergens,
without needing a full contract + production sheet pair.

Two input modes:
  - Text (a pasted or typed ingredient list): fully local, zero API calls,
    zero cost -- this is just allergen_reference.scan_recipe() directly.
  - Photo/PDF of a dish or recipe card: uses extract.extract_ingredient_list()
    (Gemini) to read the ingredients first, then the same local scan.

This is deliberately separate from the full agent.py pipeline: it doesn't
need a contract, doesn't check guaranteed-allergen-free categories against
anything, and doesn't escalate/auto-clear -- it just answers "what
allergens are in this dish," directly and immediately. Useful for a quick
check on a single recipe card without running the full compliance review.
"""

from allergen_reference import scan_recipe


def scan_text_ingredients(ingredients: list) -> dict:
    """
    ingredients: list[str], e.g. ["chicken thigh", "satay sauce", "lime"]
    Returns {category: [{match_type, matched_term, source_ingredient}, ...]}
    -- same shape as allergen_reference.scan_recipe(), just documented here
    as the entry point for this module.
    """
    raw = scan_recipe(ingredients)
    return {
        category: [
            {"match_type": m[0], "matched_term": m[1], "source_ingredient": m[2]}
            for m in matches
        ]
        for category, matches in raw.items()
    }


def parse_ingredient_text(raw_text: str) -> list:
    """Splits a pasted ingredient list on commas or newlines, whichever the
    user used, and strips empty entries."""
    if "\n" in raw_text:
        parts = raw_text.split("\n")
    else:
        parts = raw_text.split(",")
    return [p.strip() for p in parts if p.strip()]


def format_scan_report(dish_name: str, ingredients: list, results: dict) -> str:
    lines = [f"ALLERGEN SCAN -- {dish_name}", ""]
    if not results:
        lines.append("No allergens from the tracked category set detected "
                      "in the listed ingredients.")
    else:
        for category, matches in results.items():
            hidden = [m for m in matches if m["match_type"] == "hidden"]
            direct = [m for m in matches if m["match_type"] == "direct"]
            lines.append(f"[{category.replace('_', ' ').upper()}]")
            for m in direct:
                lines.append(f"  direct: '{m['source_ingredient']}'")
            for m in hidden:
                lines.append(f"  HIDDEN: '{m['source_ingredient']}' "
                              f"(carries {category.replace('_', ' ')} via "
                              f"'{m['matched_term']}')")
            lines.append("")
    lines.append(f"Ingredients scanned ({len(ingredients)}): "
                  + ", ".join(ingredients))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python allergen_scan.py \"ingredient one, ingredient two, ...\"")
        print("   or: python allergen_scan.py --photo path/to/dish.jpg")
        sys.exit(1)

    if sys.argv[1] == "--photo":
        import extract
        data = extract.extract_ingredient_list(sys.argv[2])
        dish_name = data.get("dish_name", "unknown dish")
        ingredients = data.get("ingredients", [])
        if data.get("_confidence") != "high":
            print(f"[confidence: {data.get('_confidence')}] "
                  f"{data.get('_confidence_notes', '')}\n")
    else:
        dish_name = "pasted ingredient list"
        ingredients = parse_ingredient_text(sys.argv[1])

    results = scan_text_ingredients(ingredients)
    print(format_scan_report(dish_name, ingredients, results))
