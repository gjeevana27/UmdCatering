"""
Loads and normalizes event contracts and production sheets.

For this portfolio version, contracts and production sheets are structured
JSON (see data/sample_events/). A production version would add a PDF/DOCX
ingestion step (contract text extraction, OCR fallback for scanned banquet
event orders) upstream of this same normalized schema -- the agent and
discrepancy logic below don't need to change either way.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MenuItem:
    name: str
    ingredients: list
    declared_allergens: list = field(default_factory=list)  # allergens the
    # kitchen/chef already declared on the recipe card, if any


@dataclass
class Contract:
    event_id: str
    client_name: str
    guest_count: int
    contracted_menu_items: list       # list[str] -- item names client signed off on
    guaranteed_allergen_free: list    # list[str] -- categories client was promised are absent, e.g. ["tree_nuts"]
    price_per_head: float
    special_instructions: str = ""
    event_date: str = ""              # e.g. "2026-02-16" -- a change here is a reschedule
    venue: str = ""                   # delivery/event location
    event_time: str = ""              # e.g. "6:30 PM - 7:45 PM"
    status: str = "confirmed"         # "confirmed" | "cancelled" -- see contract_diff.py
    division: str = ""                # e.g. "Good Tidings" | "Goodies To Go" -- which
    # catering division/brand this event belongs to. Purely an organizing/
    # filtering field -- the compliance logic itself doesn't treat one
    # division differently from another; allergen reference and recipe
    # data are shared across all divisions.


@dataclass
class ProductionSheet:
    event_id: str
    menu_items: list  # list[MenuItem]
    guest_count: int
    notes: str = ""


def load_contract(path) -> Contract:
    data = json.loads(Path(path).read_text())
    return Contract(**data)


def load_production_sheet(path) -> ProductionSheet:
    data = json.loads(Path(path).read_text())
    items = [MenuItem(**item) for item in data["menu_items"]]
    return ProductionSheet(
        event_id=data["event_id"],
        menu_items=items,
        guest_count=data["guest_count"],
        notes=data.get("notes", ""),
    )


def load_event(event_dir) -> tuple:
    """event_dir contains contract.json and production_sheet.json"""
    event_dir = Path(event_dir)
    contract = load_contract(event_dir / "contract.json")
    sheet = load_production_sheet(event_dir / "production_sheet.json")
    return contract, sheet


_CONTRACT_FIELDS = {"event_id", "client_name", "guest_count",
                     "contracted_menu_items", "guaranteed_allergen_free",
                     "price_per_head", "special_instructions",
                     "event_date", "venue", "event_time", "status", "division"}
_SHEET_FIELDS = {"event_id", "menu_items", "guest_count", "notes"}
_MENU_ITEM_FIELDS = {"name", "ingredients", "declared_allergens"}
_PULL_SHEET_FIELDS = {"event_id", "guest_count", "secure_items"}
_SECURE_ITEM_FIELDS = {"name", "quantity", "notes"}


@dataclass
class SecureItem:
    """One line on a kitchen-board pull sheet: 'Secure [thing]: qty'."""
    name: str
    quantity: str = ""   # kept as string -- board sheets mix "6", "35", "2 (1 per)"
    notes: str = ""


@dataclass
class PullSheet:
    """
    A kitchen-board / prep-checklist sheet, generated from a chef's Excel
    workbook -- structurally different from ProductionSheet (dish ->
    ingredients). This is a flat list of "Secure X" line items grouped
    under one event, which is what an Excel-exported pull sheet actually
    looks like, as opposed to the recipe-card style ProductionSheet.
    """
    event_id: str
    secure_items: list       # list[SecureItem]
    guest_count: int = 0


def pull_sheet_from_extracted(data: dict) -> tuple:
    """Same contract as production_sheet_from_extracted() -- returns
    (PullSheet, confidence, notes)."""
    confidence = data.get("_confidence", "unknown")
    notes = data.get("_confidence_notes", "")
    items = []
    for item in data.get("secure_items", []):
        filtered = {k: v for k, v in item.items() if k in _SECURE_ITEM_FIELDS}
        items.append(SecureItem(**filtered))
    sheet = PullSheet(
        event_id=data.get("event_id", "unknown"),
        secure_items=items,
        guest_count=data.get("guest_count", 0),
    )
    return sheet, confidence, notes


def contract_from_extracted(data: dict) -> tuple:
    """
    Builds a Contract from extract.py's output, which includes
    _confidence/_confidence_notes fields the Contract dataclass doesn't
    have. Returns (Contract, confidence, notes) -- callers (app.py) should
    surface low/medium confidence to the reviewer before trusting the
    downstream compliance findings.
    """
    confidence = data.get("_confidence", "unknown")
    notes = data.get("_confidence_notes", "")
    filtered = {k: v for k, v in data.items() if k in _CONTRACT_FIELDS}
    return Contract(**filtered), confidence, notes


def production_sheet_from_extracted(data: dict) -> tuple:
    confidence = data.get("_confidence", "unknown")
    notes = data.get("_confidence_notes", "")
    items = []
    for item in data.get("menu_items", []):
        filtered_item = {k: v for k, v in item.items() if k in _MENU_ITEM_FIELDS}
        items.append(MenuItem(**filtered_item))
    sheet = ProductionSheet(
        event_id=data.get("event_id", "unknown"),
        menu_items=items,
        guest_count=data.get("guest_count", 0),
        notes=data.get("notes", ""),
    )
    return sheet, confidence, notes
