"""Importing the catalog from the program director's Valuations workbook.

The workbook ("Valuations - FOCUS Houston.xlsx", exported from Numbers) is
where FOCUS keeps what each donated item is worth. The catalog in the app
started as a hand-typed copy of an older photo of it, so the two have
drifted: the workbook has items the catalog lacks, and spells the shared ones
differently ("Formula (7-oz Similac )" vs "Formula - Similac 7oz").

The import happens in two steps, and the first never writes anything:

  plan_import()   Reads the workbook and compares it with the catalog. Returns
                  a Plan: new items, price changes, other changes, rows it
                  could not use, and catalog items the workbook doesn't list.
  apply_plan()    Makes exactly the changes in a Plan, in one transaction,
                  recorded in the audit log under the admin who ran it.

Rules (docs/PRODUCTION_DESIGN.md, section 12, Phase 3):

  * Nothing is guessed. A blank price, a range, text in a number cell, or a
    used value that doesn't match the item's rate becomes price_entry=manual
    with a note saying why -- the price is typed when the item goes out.
  * A price change ADDS an item_price row. Old rows are never edited, so every
    batch committed before the import still reports what it was worth.
  * Names are matched exactly (after tidying spaces, case and dashes) or
    through the reviewed ALIASES table below. Never fuzzily: "Pants" exists in
    three categories at three prices.
  * The workbook has no report-bucket column. A new item's bucket comes from
    NEW_ITEM_BUCKETS, which is reviewed in the dry run like everything else.
    An existing item keeps its bucket unless a decision below changes it.
  * Running it twice changes nothing the second time.

Nothing in this module imports Flask (see the layering rule in
docs/PRODUCTION_DESIGN.md, section 3).
"""

import dataclasses
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# --- Reading the workbook ------------------------------------------------------

# Section headings in the workbook, and the catalog category each one means.
# A heading not in this table stops the import: a new section is a decision
# for a person (which category? which used rate?), not something to infer.
SECTIONS = {
    "baby essentials": "baby_essentials",
    "womens hygine": "womens_hygiene",   # sic, as spelled in the workbook
    "home goods": "home_goods",
    "baby/mom items": "baby_mom_items",
    "adult": "clothing_adult",
    "children 4 - 18": "clothing_children",
    "infant": "clothing_infant",
}

# The used rate each sheet states in its own headings: "half the price if the
# item were used" on Baby Essentials, "Used (75%)" on Clothing Val.
GOODS_USED_RATE = Decimal("0.50")
CLOTHING_USED_RATE = Decimal("0.75")

# Sheets read, and sheets deliberately not read (with the reason, so the dry
# run can say so instead of silently skipping them).
GOODS_SHEET = "Baby Essentials"
CLOTHING_SHEET = "Clothing Val"
SKIPPED_SHEETS = {
    "Export Summary": "Numbers' description of the export, not data.",
    "SnacksDrinks": "This center doesn't hand out food (decision of 27 "
                    "October 2026).",
}


class ImportError_(Exception):
    """The workbook can't be imported as it is. The message says where and why."""


@dataclasses.dataclass(frozen=True)
class SourceRow:
    """One item row of the workbook, as read. Nothing interpreted yet."""

    sheet: str
    row: int
    category: str
    name: str            # as written, trimmed
    new_cell: object     # the "price new" cell: a number, text, or None
    used_rate: Decimal
    # Baby Essentials' "Quantity" and "$ Received" cells. Where "$ Received"
    # is filled in, one item's price is $ Received / Quantity (decision of
    # 27 October 2026: "a crib with quantity 2 and $200 is $100 a crib").
    quantity_cell: object = None
    received_cell: object = None

    @property
    def where(self):
        return f"{self.sheet} row {self.row}"


def tidy(text):
    """Trim, collapse runs of spaces, and turn Numbers' special dashes and
    spaces into plain ones. "Baby Wash/Shampoo (2‑in‑1)" uses U+2011, a
    non-breaking hyphen, which looks identical and compares different."""
    text = unicodedata.normalize("NFKC", str(text))
    text = re.sub(r"[‐-―−]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def match_key(text):
    """The form two names are compared in: tidied and case-folded."""
    return tidy(text).casefold()


def read_workbook(path):
    """Every item row in the sheets we import, as SourceRows.

    openpyxl reads the cached cell VALUES (data_only), not formulas: the file
    comes from Numbers, and its values are what the program director sees.
    """
    import openpyxl  # here, not at the top: only the import command needs it

    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as error:  # openpyxl raises a zoo of exception types
        raise ImportError_(f"Can't open {path} as an .xlsx workbook: {error}")

    try:
        names = set(book.sheetnames)
        missing = {GOODS_SHEET, CLOTHING_SHEET} - names
        if missing:
            raise ImportError_(
                f"The workbook has no sheet named {', '.join(sorted(missing))}. "
                f"Sheets found: {', '.join(book.sheetnames)}."
            )
        unknown = names - {GOODS_SHEET, CLOTHING_SHEET} - set(SKIPPED_SHEETS)
        if unknown:
            raise ImportError_(
                f"The workbook has sheets this import doesn't know: "
                f"{', '.join(sorted(unknown))}. Decide what they are before importing."
            )
        rows = _read_goods(book[GOODS_SHEET]) + _read_clothing(book[CLOTHING_SHEET])
    finally:
        book.close()
    return rows


def _cells(sheet, width):
    """(row number, [first `width` cell values]) for every row."""
    for number, values in enumerate(
        sheet.iter_rows(max_col=width, values_only=True), start=1
    ):
        values = list(values) + [None] * (width - len(values))
        yield number, values


def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _section(name, sheet, row):
    category = SECTIONS.get(match_key(name))
    if category is None:
        raise ImportError_(
            f"{sheet} row {row}: '{tidy(name)}' looks like a section heading, "
            f"but it isn't one this import knows ({', '.join(SECTIONS)})."
        )
    return category


def _read_goods(sheet):
    """Baby Essentials: column A is the item, B its price new. A row with a
    name and nothing (or "Range") in B is a section heading. The sheet starts
    in the Baby Essentials section, which has no heading row of its own.

    Columns C and D are "Quantity" and "$ Received"; see read_price().
    """
    rows = []
    category = SECTIONS["baby essentials"]
    for number, (name, price, quantity, received) in _cells(sheet, 4):
        if number == 1 or _is_blank(name):
            continue  # the header row; blank rows; the note row under the header
        if _is_blank(price) or match_key(price) == "range":
            if match_key(name) in SECTIONS:
                category = _section(name, sheet.title, number)
                continue
        rows.append(SourceRow(
            sheet=sheet.title, row=number, category=category, name=tidy(name),
            new_cell=price, used_rate=GOODS_USED_RATE,
            quantity_cell=quantity, received_cell=received,
        ))
    return rows


def _read_clothing(sheet):
    """Clothing Val: a section name in column A, then item rows with the name
    in B and retail in C. Column D, "Used (75%)", is not read: every clothing
    item is valued used at 75% (decision of 27 October 2026), and a used
    value that differs is corrected per line on the review screen, not
    imported. Text in column A above the
    first section is the sheet's title ("FOCUS North America", "FY 2025").
    After it, any heading must be a known section: otherwise its rows would
    quietly be filed under the section before it."""
    rows = []
    category = None
    for number, (heading, name, retail) in _cells(sheet, 3):
        if not _is_blank(name):
            if category is None:
                continue  # the column headings above the first section
            rows.append(SourceRow(
                sheet=sheet.title, row=number, category=category, name=tidy(name),
                new_cell=retail, used_rate=CLOTHING_USED_RATE,
            ))
        elif isinstance(heading, str) and not _is_blank(heading):
            if match_key(heading) in SECTIONS:
                category = _section(heading, sheet.title, number)
            elif category is not None:
                _section(heading, sheet.title, number)  # raises, naming it
    return rows


# --- Reading a cell ------------------------------------------------------------

PENNY = Decimal("0.01")


def as_money(value):
    """A cell as dollars, or None if it isn't a plain number. Text that is
    only a number with a $ ("$12", "12.50") counts; a range or a word doesn't."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        amount = Decimal(str(value))
    elif isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
            return None
        try:
            amount = Decimal(cleaned)
        except InvalidOperation:
            return None
    else:
        return None
    if amount < 0:
        return None
    return amount.quantize(PENNY, rounding=ROUND_HALF_UP)


# --- The reviewed tables ---------------------------------------------------------
#
# Every entry below was written by a person reading the workbook next to the
# catalog, and is shown in the dry run for approval. If the workbook gains a
# row that none of them covers, the dry run lists it and --apply refuses.

# Same item, different spelling: the catalog's name was tidied when it was
# typed in. (category, name as in the workbook) -> name in the catalog.
ALIASES = {
    ("baby_essentials", "Baby Wash/Shampoo (2-in-1) (13-27oz)"): "Baby Wash/Shampoo 2-in-1 (13-27oz)",
    ("baby_essentials", "Formula (7-oz Similac )"): "Formula - Similac 7oz",
    ("baby_essentials", "Formula (12.5oz Enfamil)"): "Formula - Enfamil 12.5oz",
    ("baby_essentials", "Formula (20 oz Enfamil)"): "Formula - Enfamil 20oz",
    ("baby_essentials", "Formula (30-oz Similac )"): "Formula - Similac 30oz",
    ("baby_essentials", "Baby Powder (15 oz.)"): "Baby Powder (15oz)",
    ("baby_essentials", "Fridababy:Sick day Prep Kit"): "Fridababy Sick Day Prep Kit",
    ("baby_essentials", "Dimetapp Cold &Cough 2bottles"): "Dimetapp Cold & Cough (2 bottles)",
    ("baby_essentials", "DayQuil/ NyQuil Cold &Cough 3bottles"): "DayQuil/NyQuil Cold & Cough (3 bottles)",
    ("womens_hygiene", "Dispossable Underwear"): "Disposable Underwear",
    ("womens_hygiene", "Resusable Nursing pads"): "Reusable Nursing Pads",
    ("womens_hygiene", "Dispossable Nursing Pads"): "Disposable Nursing Pads",
    ("womens_hygiene", "Spectra baby S2 Electric Breast pumps"): "Spectra Baby S2 Electric Breast Pump",
    ("womens_hygiene", "Manual breast pumps"): "Manual Breast Pump",
    ("home_goods", "Decorations Wall"): "Wall Decorations",
    ("baby_mom_items", "Car accessories mirror/car seat toys"): "Car Accessories (mirror / car seat toys)",
    ("baby_mom_items", "Pregnant body pillow"): "Pregnancy Body Pillow",
    ("baby_mom_items", "Gym Play (Finn &Emma)"): "Gym Play (Finn & Emma)",
    ("baby_mom_items", "Gym Play Matt"): "Gym Play Mat",
    ("baby_mom_items", "Accessories for play Matt"): "Play Mat Accessories",
    ("baby_mom_items", "Graco Blossom 6-in 1Convertible chair"): "Graco Blossom 6-in-1 Convertible Chair",
    ("baby_mom_items", "Booster Seat 2in 1"): "Booster Seat 2-in-1",
    ("baby_mom_items", "Munchkin Steam Microwave Baby bottle Sterilizer"): "Munchkin Steam Microwave Bottle Sterilizer",
    ("baby_mom_items", "Bath Seat Soft Spport"): "Bath Seat Soft Support",
    ("baby_mom_items", "Sleep Bundle dorm esential"): "Sleep Bundle (Dorm Essential)",
    ("baby_mom_items", "birb cloth 3pk"): "Bib Cloth 3pk",
}

# Items the catalog doesn't have yet: (category, name as in the workbook) ->
# (name to give it, report bucket). The workbook has no bucket column, so
# each bucket here is a proposal, following the catalog's existing items:
# health and personal care -> hygiene, equipment and things for the home ->
# household, anything worn -> clothing.
NEW_ITEMS = {
    ("baby_essentials", "Suction"): ("Nasal Suction", "hygiene"),
    ("baby_essentials", "Thermometer"): ("Thermometer", "hygiene"),
    ("baby_essentials", "Toilet seat covers 20 pk"): ("Toilet Seat Covers (20 pk)", "hygiene"),
    ("womens_hygiene", "Milk Storage bags motif 100ct"): ("Milk Storage Bags (Motif, 100 ct)", "household"),
    ("womens_hygiene", "microwave steralizing bags 7ct"): ("Microwave Sterilizing Bags (7 ct)", "household"),
    ("womens_hygiene", "Breast feeding Pillow"): ("Breastfeeding Pillow", "household"),
    ("home_goods", "Hair wave curler"): ("Hair Wave Curler", "household"),
    ("home_goods", "Diaper changing pad"): ("Diaper Changing Pad", "household"),
    ("home_goods", "Reversible receiving blanket"): ("Reversible Receiving Blanket", "household"),
    ("baby_mom_items", "Newborn basket with varirty of items"): ("Newborn Basket (variety of items)", "household"),
    ("clothing_adult", "Skirt"): ("Skirt", "clothing"),
    ("clothing_adult", "Cap/hat"): ("Cap / Hat", "clothing"),
    ("clothing_adult", "Scarf"): ("Scarf", "clothing"),
    ("clothing_adult", "shorts"): ("Shorts", "clothing"),
    ("clothing_children", "shorts"): ("Shorts", "clothing"),
    ("clothing_children", "Skirt"): ("Skirt", "clothing"),
    ("clothing_children", "pPJs"): ("PJs", "clothing"),
    ("clothing_children", "overalls"): ("Overalls", "clothing"),
    ("clothing_children", "Swimmsuit"): ("Swimsuit", "clothing"),
    ("clothing_children", "vest"): ("Vest", "clothing"),
    ("clothing_children", "Hat"): ("Hat", "clothing"),
    ("clothing_children", "Crochet blanket"): ("Crochet Blanket", "clothing"),
    ("clothing_children", "Crochet blanket w beanie"): ("Crochet Blanket with Beanie", "clothing"),
    ("clothing_infant", "Dress"): ("Dress", "clothing"),
    ("clothing_infant", "overalls"): ("Overalls", "clothing"),
    ("clothing_infant", "socks"): ("Socks", "clothing"),
    ("clothing_infant", "crochet blanket"): ("Crochet Blanket", "clothing"),
    ("clothing_infant", "crochet blanket w beanie"): ("Crochet Blanket with Beanie", "clothing"),
    ("clothing_infant", "hat"): ("Hat", "clothing"),
    ("clothing_infant", "5 pk beanies"): ("Beanies (5 pk)", "clothing"),
    ("clothing_infant", "4 pk mittens"): ("Mittens (4 pk)", "clothing"),
    ("clothing_infant", "Swim Suit"): ("Swimsuit", "clothing"),
    ("clothing_infant", "Sunglasses"): ("Sunglasses", "clothing"),
    ("clothing_infant", "Hair bows"): ("Hair Bows", "clothing"),
    ("clothing_infant", "Tights"): ("Tights", "clothing"),
    # Listed under Infant clothing, but not clothing. Category stays where the
    # workbook puts them (that's where the program director looks); the bucket follows
    # what they are.
    ("clothing_infant", "washcloths"): ("Washcloths", "hygiene"),
    ("clothing_infant", "sipy cups"): ("Sippy Cups", "household"),
    ("clothing_infant", "headbands"): ("Headbands", "clothing"),
    ("clothing_infant", "loofah"): ("Loofah", "hygiene"),
}

# Decided in Phase 0 (PRODUCTION_DESIGN section 1), not in the workbook.
DECIDED_ITEMS = (
    {
        "name": "Individual Diaper", "category": "diapers", "report_bucket": "diapers",
        "price": Decimal("0.44"), "price_entry": "fixed", "used_multiplier": 0.5,
        "note": None,
        "why": "Decision 8a: one diaper item at $0.44; the program director adds the rest.",
    },
    {
        "name": "Bus Pass", "category": "transportation", "report_bucket": "transportation",
        "price": Decimal("10.00"), "price_entry": "manual", "used_multiplier": 0.5,
        "note": "Type the card's value. Pre-filled at $10, the value her sheets "
                "use. Reported on its own line, never in Non-Food GIK.",
        "why": "Bus pass decision: counted, value typed, outside GIK.",
    },
)

# Decided changes to items that already exist.
FORMULA_NOTE = ("Typed per receipt (decision 8b). Pre-filled with the "
                "catalog price; change it to what the receipt says.")
# Kept "type the price" even though the workbook shows a clean price: the
# photo the catalog was typed from showed used values that aren't the item's
# rate (decision of 27 October 2026). Catalog names.
KEEP_MANUAL = {
    ("home_goods", "Small Rideable Toy Cars"),
    ("home_goods", "Big Rideable Toy Cars"),
    ("baby_mom_items", "Crib Wind Chime"),
}
DISPLAY_GROUPS = {
    # Hygiene money, listed with the diapers on the monthly report (decision 9).
    ("baby_essentials", "Individual Pack of Wipes"): "diapers",
}


def _keyed(table):
    return {(category, match_key(name)): value for (category, name), value in table.items()}


_ALIASES = _keyed(ALIASES)
_NEW_ITEMS = _keyed(NEW_ITEMS)
_DISPLAY_GROUPS = _keyed(DISPLAY_GROUPS)
_KEEP_MANUAL = {(category, match_key(name)) for category, name in KEEP_MANUAL}


# --- Planning --------------------------------------------------------------------


@dataclasses.dataclass
class Reading:
    """What one workbook row says about an item's price."""

    price: object        # Decimal, or None if the row gives no usable price
    price_entry: str     # "fixed" or "manual"
    reason: object       # why it's manual (str), or None


def read_price(row):
    """Interpret a row's price cells. Never guesses: anything that isn't a
    clear price comes back manual, with why.

    Where "$ Received" is filled in, one item's price is $ Received divided
    by Quantity (a blank quantity means one). That replaces the "price new"
    column for the row, and the used rate then applies on top as usual.
    """
    new = as_money(row.new_cell)
    if not _is_blank(row.received_cell):
        return _per_item_received(row, new)
    if new is None:
        return Reading(None, "manual", _why_not_a_price(row))
    return Reading(new, "fixed", None)


def _why_not_a_price(row):
    if _is_blank(row.new_cell):
        return f"No price in the Valuations workbook ({row.where})."
    if match_key(row.new_cell) == "manual price":
        return f"The Valuations workbook says \"Manual price\" ({row.where})."
    if re.search(r"\d\s*-\s*\$?\d", str(row.new_cell)):
        return (f"The Valuations workbook gives a range, "
                f"{tidy(row.new_cell)}, not one price ({row.where}).")
    return (f"The Valuations workbook's price cell says "
            f"\"{tidy(row.new_cell)}\", which isn't a price ({row.where}).")


def _per_item_received(row, new):
    received = as_money(row.received_cell)
    if _is_blank(row.quantity_cell):
        quantity = 1
    elif isinstance(row.quantity_cell, (int, float)) and not isinstance(row.quantity_cell, bool) \
            and row.quantity_cell == int(row.quantity_cell) and row.quantity_cell > 0:
        quantity = int(row.quantity_cell)
    else:
        quantity = None

    if received is None or quantity is None:
        return Reading(new, "manual", (
            f"The Valuations workbook's \"$ Received\" is "
            f"\"{tidy(row.received_cell)}\" for quantity "
            f"\"{tidy(row.quantity_cell) if not _is_blank(row.quantity_cell) else 1}\", "
            f"so one item's price can't be worked out ({row.where})."
        ))

    per_item = (received / quantity).quantize(PENNY, rounding=ROUND_HALF_UP)
    if new is None:
        # The per-item price is clear, but "price new" is a range or text:
        # that says the item varies, so it's typed each time, pre-filled.
        return Reading(per_item, "manual", (
            f"{_why_not_a_price(row)} Pre-filled at ${per_item}, its "
            f"\"$ Received\" per item (${received} for {quantity})."
        ))
    return Reading(per_item, "fixed", None)


@dataclasses.dataclass
class NewItem:
    name: str
    category: str
    report_bucket: str
    price: object
    price_entry: str
    used_multiplier: float
    note: object
    display_group: object
    source: str

    @property
    def key(self):
        return (self.category, match_key(self.name))


@dataclasses.dataclass
class Change:
    """One field of one existing item changing."""

    item_id: int
    item_label: str
    field: str           # "unit_price_new" means a new price row
    old: object
    new: object
    why: str


@dataclasses.dataclass
class Plan:
    new_items: list
    price_changes: list
    other_changes: list
    unmatched_rows: list     # workbook rows no table covers: block --apply
    left_alone: list         # catalog items the workbook doesn't list
    skipped_sheets: dict

    @property
    def blocked(self):
        return bool(self.unmatched_rows)

    @property
    def is_empty(self):
        return not (self.new_items or self.price_changes or self.other_changes)

    @property
    def manual_items(self):
        """Everything the plan leaves needing a typed price, with why."""
        listed = [(f"{n.name} · {n.category}", n.note) for n in self.new_items
                  if n.price_entry == "manual"]
        listed += [(c.item_label, c.why) for c in self.other_changes
                   if c.field == "price_entry" and c.new == "manual"]
        return listed


def plan_import(rows, items):
    """Compare workbook rows with the catalog. Pure: `items` is any list of
    objects shaped like app.models.Item, and nothing is written.

    Returns a Plan. Call apply_plan() to carry it out.
    """
    by_key = {(item.category, match_key(item.name)): item for item in items}

    # Which catalog item (or new item) each row is about.
    targets = {}
    unmatched = []
    for row in rows:
        key = (row.category, match_key(row.name))
        if key in _ALIASES:
            key = (row.category, match_key(_ALIASES[key]))
        elif key in _NEW_ITEMS:
            # Created by an earlier import, under its tidied name: it's an
            # existing item now, or running the import twice would add it twice.
            created = (row.category, match_key(_NEW_ITEMS[key][0]))
            if created in by_key:
                key = created
        if key in by_key:
            targets.setdefault(("existing", key), []).append(row)
        elif key in _NEW_ITEMS:
            targets.setdefault(("new", key), []).append(row)
        else:
            unmatched.append(row)

    new_items, price_changes, other_changes = [], [], []
    seen = set()

    for (kind, key), group in targets.items():
        reading = _reading_for(group)
        if kind == "new":
            name, bucket = _NEW_ITEMS[key]
            new_items.append(NewItem(
                name=name, category=key[0], report_bucket=bucket,
                price=reading.price, price_entry=reading.price_entry,
                used_multiplier=float(group[0].used_rate), note=reading.reason,
                display_group=None, source=", ".join(r.where for r in group),
            ))
            continue

        item = by_key[key]
        seen.add(key)
        label = f"{item.name} · {item.category}"
        source = ", ".join(r.where for r in group)

        current = item.current_price()
        if reading.price is not None and reading.price != current:
            price_changes.append(Change(item.id, label, "unit_price_new",
                                        current, reading.price, source))

        entry, note = reading.price_entry, reading.reason
        if item.report_bucket == "formula":
            entry, note = "manual", FORMULA_NOTE
        elif key in _KEEP_MANUAL:
            entry, note = "manual", item.note
        if entry != item.price_entry:
            other_changes.append(Change(item.id, label, "price_entry",
                                        item.price_entry, entry, note or source))
            # The note explains why a price is typed. Moving to "manual" sets
            # the new reason; moving to "fixed" removes a reason that no
            # longer applies. An item staying manual keeps its own note.
            new_note = note if entry == "manual" else None
            if new_note != item.note:
                other_changes.append(Change(item.id, label, "note",
                                            item.note, new_note, source))

        rate = float(group[0].used_rate)
        if rate != item.used_multiplier:
            other_changes.append(Change(item.id, label, "used_multiplier",
                                        item.used_multiplier, rate,
                                        f"The workbook's rate for this section ({source})."))

        group_wanted = _DISPLAY_GROUPS.get(key)
        if group_wanted is not None and group_wanted != item.display_group:
            other_changes.append(Change(item.id, label, "display_group",
                                        item.display_group, group_wanted,
                                        "Decision 9: listed with the diapers on reports."))

    for decided in DECIDED_ITEMS:
        key = (decided["category"], match_key(decided["name"]))
        if key in by_key:
            seen.add(key)
            continue
        new_items.append(NewItem(
            name=decided["name"], category=decided["category"],
            report_bucket=decided["report_bucket"], price=decided["price"],
            price_entry=decided["price_entry"],
            used_multiplier=decided["used_multiplier"], note=decided["note"],
            display_group=None, source=decided["why"],
        ))

    left_alone = sorted(
        f"{item.name} · {item.category}" for item in items
        if (item.category, match_key(item.name)) not in seen
    )

    return Plan(
        new_items=sorted(new_items, key=lambda n: (n.category, n.name)),
        price_changes=price_changes,
        other_changes=other_changes,
        unmatched_rows=unmatched,
        left_alone=left_alone,
        skipped_sheets=dict(SKIPPED_SHEETS),
    )


def _reading_for(group):
    """One row: read it. The same item listed more than once: manual, with
    every listing in the note, because two prices for one item is a question
    for the program director, not something to pick between."""
    if len(group) == 1:
        return read_price(group[0])
    def shown(r):
        price = read_price(r).price
        return tidy(r.new_cell) if price is None else f"${price}"

    listings = "; ".join(f"{shown(r)} ({r.where})" for r in group)
    return Reading(None, "manual",
                   f"Listed {len(group)} times in the Valuations workbook: "
                   f"{listings}. May be different products.")


# --- Applying --------------------------------------------------------------------


def apply_plan(plan, effective_from):
    """Carry out a Plan in one transaction. The caller sets who is doing it
    (audit.acting_as) and commits; on any error, roll back and nothing at all
    has changed.

    Uses app.models only here, so plan_import() stays testable without a
    database.
    """
    from app.extensions import db
    from app.models import Item
    from app.services import audit

    if plan.blocked:
        raise ImportError_(
            f"{len(plan.unmatched_rows)} workbook row(s) aren't in the reviewed "
            f"tables yet; nothing was changed. See the dry run."
        )

    for new in plan.new_items:
        item = Item(
            name=new.name, category=new.category, report_bucket=new.report_bucket,
            used_multiplier=new.used_multiplier, price_entry=new.price_entry,
            note=new.note, display_group=new.display_group, active=True,
        )
        db.session.add(item)
        db.session.flush()
        item.set_price(new.price, effective_from=effective_from)
        audit.record_change(
            table_name="item", record_id=item.id, field="created",
            old_value=None, new_value=item.display_name,
            description=f"Added {item.display_name} ({new.source})",
            action=audit.ACTION_IMPORT,
        )

    for change in plan.price_changes:
        item = db.session.get(Item, change.item_id)
        item.set_price(change.new, effective_from=effective_from)
        audit.record_change(
            table_name="item", record_id=item.id, field="unit_price_new",
            old_value=change.old, new_value=change.new,
            description=f"Price for {item.display_name} from the Valuations "
                        f"workbook ({change.why})",
            action=audit.ACTION_IMPORT,
        )

    for change in plan.other_changes:
        item = db.session.get(Item, change.item_id)
        audit.apply_change(
            item, change.field, change.new, "item",
            description=f"{item.display_name}: {change.why}",
            action=audit.ACTION_IMPORT,
        )

    db.session.flush()


# --- Showing a plan --------------------------------------------------------------


def _money(value):
    return "no price" if value is None else f"${value}"


def describe(plan):
    """The dry run, as lines of text for a person to read and approve."""
    lines = []

    def section(title, entries):
        lines.append("")
        lines.append(f"{title} ({len(entries)})")
        lines.append("-" * len(lines[-1]))
        if not entries:
            lines.append("  none")
        lines.extend(entries)

    if plan.unmatched_rows:
        section("BLOCKING: workbook rows no reviewed table covers", [
            f"  {r.where}: '{r.name}' in {r.category} -- add it to ALIASES "
            f"(same item as one in the catalog) or NEW_ITEMS (a new item)"
            for r in plan.unmatched_rows
        ])

    section("New items", [
        f"  {n.name} · {n.category}  ->  {n.report_bucket}, {_money(n.price)}, "
        f"{n.price_entry}, used rate {n.used_multiplier:g}"
        f"{'' if not n.note else chr(10) + '      note: ' + n.note}"
        f"{chr(10)}      from: {n.source}"
        for n in plan.new_items
    ])
    section("Price changes (each adds a price row; old prices are kept)", [
        f"  {c.item_label}: {_money(c.old)} -> {_money(c.new)}   ({c.why})"
        for c in plan.price_changes
    ])
    section("Other changes to existing items", [
        f"  {c.item_label}: {c.field} {c.old!r} -> {c.new!r}{chr(10)}      why: {c.why}"
        for c in plan.other_changes
    ])
    section("Newly needing a typed price, and why", [
        f"  {label}{chr(10)}      {why}" for label, why in plan.manual_items
    ])
    section("In the catalog but not in the workbook (left exactly as they are)", [
        f"  {label}" for label in plan.left_alone
    ])
    section("Sheets not read", [
        f"  {name}: {why}" for name, why in plan.skipped_sheets.items()
    ])

    lines.append("")
    if plan.blocked:
        lines.append("Nothing can be applied until the blocking rows are resolved.")
    elif plan.is_empty:
        lines.append("Nothing to change: the catalog already matches the workbook.")
    else:
        lines.append("DRY RUN: nothing has been changed. Re-run with --apply to make "
                     "exactly these changes.")
    return lines
