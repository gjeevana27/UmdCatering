"""
Allergen reference -- transcribed from the kitchen's real reference cards
(the "Big 9" FDA-recognized major allergens: milk, egg, fish, shellfish,
tree nuts, peanuts, wheat, soybeans, sesame).

Source and status per category:
  - EGG, FISH, SHELLFISH, PEANUT, TREE_NUTS, SESAME, WHEAT_GLUTEN, SOY:
    transcribed directly from the kitchen's physical reference cards
    (including handwritten additions -- sesame/tahini under the nuts card,
    panko under the wheat card). VERIFIED against real kitchen material.
  - MILK: the kitchen's milk/dairy card was not provided yet. This
    category still uses the original seed list and is UNVERIFIED --
    replace it the same way as the others once that card is available.

A few judgment calls made during transcription, flagged for review:
  - "Peanut" and "Tree Nuts" are kept as two SEPARATE categories, even
    though the physical card combines them under one "NUTS/TREE NUTS"
    header -- this matches the kitchen's own typed category list, and is
    medically more correct (peanut and tree nut allergies don't always
    co-occur). "mandalona nuts" and "artificial nuts" are peanut-based
    products that imitate other nuts, so they're filed under PEANUT
    despite the word "nuts."
  - "mixed nuts" and "goober nuts" are filed under BOTH peanut and
    tree_nuts, since a real mixed-nuts product plausibly contains both.
  - "egg rolls" appears on the kitchen's NUTS/TREE NUTS card as written
    -- kept as-is (filed under peanut, since egg-roll wrappers/fillings
    in some cuisines use peanut oil or paste), but this is worth
    double-checking with whoever maintains the card.
  - Soy card items marked with "*" on the physical card are explicitly
    labeled by the card itself as "may not contain soy, but source is
    seldom listed" -- i.e. suspect, not certain. Included as hidden
    carriers per the card's own instruction to kitchen staff.
  - "soy sauce" and "barbecue sauce" appear on more than one card in real
    life (soy sauce usually contains wheat too) and are filed under every
    category their card lists them on.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AllergenRule:
    category: str
    # Direct terms: the ingredient name itself signals the allergen.
    direct_terms: tuple
    # Hidden carriers: common ingredients that contain the allergen but don't
    # say so in the name. These are the ones a naive keyword scan misses.
    hidden_carriers: tuple = field(default_factory=tuple)


ALLERGEN_RULES = [
    AllergenRule(
        category="milk",
        direct_terms=("milk", "cream", "butter", "cheese", "yogurt", "ghee",
                       "casein", "whey"),
        hidden_carriers=("au gratin", "bechamel", "alfredo", "ranch dressing",
                          "caesar dressing", "nougat", "roux"),
        # UNVERIFIED -- kitchen's milk/dairy card not yet provided, see
        # module docstring.
    ),
    AllergenRule(
        category="egg",
        direct_terms=("egg powder", "egg protein", "egg white", "egg yolk",
                       "pasteurized egg", "eggnog", "eggs"),
        # "eggs" (plural) added beyond the card -- recipes commonly just
        # list a plain ingredient like "eggs" rather than a product name.
        # Deliberately NOT adding a bare "egg" term: substring matching
        # means "egg" alone would false-positive on "eggplant" (no relation
        # to actual eggs) and "veggies" -- "eggs" (with the trailing s)
        # doesn't collide with either of those, so it's the safer minimal
        # addition. Revisit with word-boundary matching if this needs to
        # be more thorough.
        hidden_carriers=("albumin", "mayonnaise", "baking powder",
                          "ovalbumin", "ovoglobulin", "ovomucin", "globulin",
                          "ovomucoid", "livetin", "ovovitellin",
                          "simlesse", "vitellin", "meringue"),
    ),
    AllergenRule(
        category="fish",
        direct_terms=("anchovies", "anchovy", "bass", "catfish", "cod",
                       "flounder", "grouper", "haddock", "hake", "halibut",
                       "herring", "mahi mahi", "perch", "pike", "pollock",
                       "polluck", "salmon", "sole", "snapper", "swordfish",
                       "tilapia", "trout", "tuna", "fish oil", "fish gelatin",
                       "fish sticks"),
        hidden_carriers=("barbecue sauce", "bouillabaisse", "caesar dressing",
                          "caponata", "worcestershire sauce"),
    ),
    AllergenRule(
        category="shellfish",
        direct_terms=("barnacle", "crab", "crawfish", "krill", "lobster",
                       "prawns", "shrimp", "abalone", "clams", "cockle",
                       "mussels", "octopus", "oysters", "sea urchin",
                       "scallops", "snails", "squid", "whelk"),
        hidden_carriers=("bouillabaisse", "glucosamine", "fish stock",
                          "seafood flavoring", "surimi"),
    ),
    AllergenRule(
        category="peanut",
        direct_terms=("peanut protein", "hydrolyzed peanut protein",
                       "peanut oil", "peanut butter", "peanut flour",
                       "goober peas"),
        hidden_carriers=("mandalona nuts", "artificial nuts", "goober nuts",
                          "goobver nuts", "mixed nuts", "egg rolls",
                          "satay"),  # not on the kitchen's card -- "satay"
        # kept because satay sauce is traditionally peanut-based and is a
        # well-documented hidden-allergen source; add/remove per your
        # judgment if this doesn't match how your kitchen sources it.
    ),
    AllergenRule(
        category="tree_nuts",
        direct_terms=("almonds", "hickory nuts", "beechnuts",
                       "macadamia nuts", "brazil nuts", "pecans", "cashews",
                       "pine nuts", "pignoli nuts", "pinion", "chestnuts",
                       "pistachios", "filberts", "walnuts", "hazelnuts"),
        hidden_carriers=("marzipan", "pesto", "mixed nuts", "goober nuts"),
    ),
    AllergenRule(
        category="sesame",
        direct_terms=("sesame seeds", "sesame oil", "sesame"),
        hidden_carriers=("tahini",),
    ),
    AllergenRule(
        category="wheat_gluten",
        direct_terms=("wheat starch", "wheat gluten", "wheat bran",
                       "whole wheat flour", "wheat germ", "flour",
                       "high gluten flour", "graham flour"),
        hidden_carriers=("bran", "cracker", "gelatinized starch", "durum",
                          "bread crumbs", "barley", "farina", "semolina",
                          "modified starch", "high protein", "vegetable gum",
                          "vegetable starch", "vital gluten", "couscous",
                          "enriched flour", "rye", "malt", "soy sauce",
                          "yeast extract", "oats", "bulgur", "orzo", "beer",
                          "lager", "matzo", "pasta", "seitan",
                          "sprouted wheat", "spelt", "panko"),
    ),
    AllergenRule(
        category="soy",
        direct_terms=("tofu", "soy beans", "soy flour", "soy milk",
                       "soy nuts", "soy protein", "soy protein isolate",
                       "soy sauce", "soy sprouts", "edamame"),
        hidden_carriers=("emulsifiers", "stabilizers", "lecithin", "tempeh",
                          "shoyu", "soy albumin", "soy lecithin",
                          "barbecue sauce", "tamari",
                          "textured vegetable protein", "miso",
                          "unspecified sprouts", "vegetable broth",
                          "vegetable gum", "vegetable oil", "vegetable paste",
                          "vegetable protein", "vegetable shortening",
                          "vegetable starch"),
    ),
]

_LOOKUP = {rule.category: rule for rule in ALLERGEN_RULES}


def scan_ingredient(ingredient_name: str):
    """
    Scan a single ingredient string and return a list of
    (category, match_type, matched_term) tuples.
    match_type is 'direct' or 'hidden'.
    """
    name = ingredient_name.lower()
    hits = []
    for rule in ALLERGEN_RULES:
        for term in rule.direct_terms:
            if term in name:
                hits.append((rule.category, "direct", term))
        for term in rule.hidden_carriers:
            if term in name:
                hits.append((rule.category, "hidden", term))
    return hits


def scan_recipe(ingredient_list):
    """
    Scan a full ingredient list for a recipe.
    Returns {category: [(match_type, matched_term, source_ingredient), ...]}
    """
    findings = {}
    for ingredient in ingredient_list:
        for category, match_type, term in scan_ingredient(ingredient):
            findings.setdefault(category, []).append(
                (match_type, term, ingredient)
            )
    return findings
