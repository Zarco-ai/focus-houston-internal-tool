

CONDITION_NEW = "new"
CONDITION_USED = "used"
CONDITIONS = (CONDITION_NEW, CONDITION_USED)


PRICE_ENTRY_FIXED = "fixed"
PRICE_ENTRY_MANUAL = "manual"
PRICE_ENTRY_CHOICES = (PRICE_ENTRY_FIXED, PRICE_ENTRY_MANUAL)


STATUS_DRAFT = "draft"
STATUS_COMMITTED = "committed"
STATUSES = (STATUS_DRAFT, STATUS_COMMITTED)

STALE_DRAFT_DAYS = 1


COMMIT_MODE_PER_BATCH = "per_batch"
COMMIT_MODE_PER_MOTHER = "per_mother"
COMMIT_MODE_AUTO = "auto"
COMMIT_MODES = (COMMIT_MODE_PER_BATCH, COMMIT_MODE_PER_MOTHER, COMMIT_MODE_AUTO)

COMMIT_MODE_LABELS = {
    COMMIT_MODE_PER_BATCH: "Review and approve each batch",
    COMMIT_MODE_PER_MOTHER: "Approve, then start the next one right away",
    COMMIT_MODE_AUTO: "Save entries automatically, no approval step",
}

COMMIT_MODE_HELP = {
    COMMIT_MODE_PER_BATCH: (
        "You count everything, look it over on the review screen, then approve. "
        "Approving takes you back to the home screen."
    ),
    COMMIT_MODE_PER_MOTHER: (
        "Same as above, but approving opens a new empty batch straight away. "
        "Good if you want one batch per person."
    ),
    COMMIT_MODE_AUTO: (
        "Counts are saved as you enter them, with no review step. An item whose "
        "price you have not typed is saved too, and listed in the review queue "
        "so you can put a value on it later."
    ),
}


BUCKET_FORMULA = "formula"
BUCKET_FOOD = "food"
BUCKET_DIAPERS = "diapers"
BUCKET_CLOTHING = "clothing"
BUCKET_HYGIENE = "hygiene"
BUCKET_HOUSEHOLD = "household"
BUCKET_TRANSPORTATION = "transportation"

REPORT_BUCKETS = (
    BUCKET_FORMULA,
    BUCKET_FOOD,
    BUCKET_DIAPERS,
    BUCKET_CLOTHING,
    BUCKET_HYGIENE,
    BUCKET_HOUSEHOLD,
    BUCKET_TRANSPORTATION,
)

BUCKET_LABELS = {
    BUCKET_FORMULA: "Baby Formula",
    BUCKET_FOOD: "Food (GIK)",
    BUCKET_DIAPERS: "Individual Diapers",
    BUCKET_CLOTHING: "Clothing",
    BUCKET_HYGIENE: "Hygiene",
    BUCKET_HOUSEHOLD: "Household Goods",
    BUCKET_TRANSPORTATION: "Transportation Assistance",
}

COMBINED_GOODS_BUCKETS = (BUCKET_CLOTHING, BUCKET_HYGIENE, BUCKET_HOUSEHOLD)


CATEGORY_ORDER = (
    "baby_essentials",
    "diapers",
    "womens_hygiene",
    "home_goods",
    "baby_mom_items",
    "clothing_adult",
    "clothing_children",
    "clothing_infant",
    "transportation",
)

CATEGORY_LABELS = {
    "baby_essentials": "Baby Essentials",
    "diapers": "Diapers",
    "womens_hygiene": "Women's Hygiene",
    "home_goods": "Home Goods",
    "baby_mom_items": "Baby & Mom Items",
    "clothing_adult": "Clothing — Adult",
    "clothing_children": "Clothing — Children",
    "clothing_infant": "Clothing — Infant",
    "transportation": "Transportation",
}

CATEGORY_SHORT_LABELS = {
    "baby_essentials": "Baby",
    "diapers": "Diapers",
    "womens_hygiene": "Women's",
    "home_goods": "Home",
    "baby_mom_items": "Baby & Mom",
    "clothing_adult": "Adult",
    "clothing_children": "Children",
    "clothing_infant": "Infant",
    "transportation": "Transport",
}


def category_label(slug):
    if slug in CATEGORY_LABELS:
        return CATEGORY_LABELS[slug]
    return (slug or "").replace("_", " ").title()


def category_short_label(slug):
    if slug in CATEGORY_SHORT_LABELS:
        return CATEGORY_SHORT_LABELS[slug]
    return category_label(slug)


def bucket_label(slug):
    if slug in BUCKET_LABELS:
        return BUCKET_LABELS[slug]
    return (slug or "").replace("_", " ").title()


def sort_categories(slugs):
    known = [c for c in CATEGORY_ORDER if c in slugs]
    unknown = sorted(s for s in slugs if s not in CATEGORY_ORDER)
    return known + unknown
