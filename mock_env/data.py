"""In-memory seed data for the mock company environment.

All state lives in the module-level `STORE` and is rebuilt by `reset()`.
"""
from __future__ import annotations

import copy
from datetime import date

_SEED_VENDORS = [
    {"id": 1, "name": "Company X", "email": "billing@companyx.example"},
    {"id": 2, "name": "Company X Ltd", "email": "accounts@companyxltd.example"},
    {"id": 3, "name": "Acme Supplies", "email": "ar@acme.example"},
]

# Invoice list order is deliberately NOT sorted by issue date: the latest
# Company X invoice (INV-X-1042) sits in the middle of the list.
_SEED_INVOICES = [
    {
        "id": "INV-X-0987",
        "vendor_id": 1,
        "issue_date": date(2026, 6, 14),
        "due_date": date(2026, 7, 14),
        "currency": "USD",
        "status": "Paid",
        "line_items": [
            {"description": "Cloud hosting - June", "qty": 1, "unit_price": 3200.00},
            {"description": "Support hours", "qty": 10, "unit_price": 85.00},
        ],
    },
    {
        "id": "INV-X-1042",
        "vendor_id": 1,
        "issue_date": date(2026, 9, 22),
        "due_date": date(2026, 10, 22),
        "currency": "USD",
        "status": "Open",
        "line_items": [
            {"description": "Cloud hosting - September", "qty": 1, "unit_price": 3450.00},
            {"description": "Support hours", "qty": 12, "unit_price": 85.00},
            {"description": "SSL certificate renewal", "qty": 1, "unit_price": 230.50},
        ],
    },
    {
        "id": "INV-X-1013",
        "vendor_id": 1,
        "issue_date": date(2026, 8, 3),
        "due_date": date(2026, 9, 2),
        "currency": "USD",
        "status": "Paid",
        "line_items": [
            {"description": "Cloud hosting - July", "qty": 1, "unit_price": 3300.00},
            {"description": "Support hours", "qty": 6, "unit_price": 85.00},
        ],
    },
    # Similar-name trap: issued more recently than any Company X invoice.
    {
        "id": "CXL-2026-118",
        "vendor_id": 2,
        "issue_date": date(2026, 9, 28),
        "due_date": date(2026, 10, 28),
        "currency": "EUR",
        "status": "Open",
        "line_items": [
            {"description": "Consulting retainer", "qty": 1, "unit_price": 7800.00},
        ],
    },
    {
        "id": "CXL-2026-097",
        "vendor_id": 2,
        "issue_date": date(2026, 7, 30),
        "due_date": date(2026, 8, 29),
        "currency": "EUR",
        "status": "Paid",
        "line_items": [
            {"description": "Consulting retainer", "qty": 1, "unit_price": 7800.00},
            {"description": "Travel expenses", "qty": 1, "unit_price": 642.10},
        ],
    },
    # High-value invoice (> 100,000) - candidate for human approval.
    {
        "id": "ACME-55120",
        "vendor_id": 3,
        "issue_date": date(2026, 9, 15),
        "due_date": date(2026, 10, 15),
        "currency": "USD",
        "status": "Open",
        "line_items": [
            {"description": "Industrial server racks", "qty": 12, "unit_price": 9800.00},
            {"description": "Installation & cabling", "qty": 1, "unit_price": 6450.00},
        ],
    },
]


def invoice_amount(inv: dict) -> float:
    return round(sum(li["qty"] * li["unit_price"] for li in inv["line_items"]), 2)


class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.vendors: list[dict] = copy.deepcopy(_SEED_VENDORS)
        self.invoices: list[dict] = copy.deepcopy(_SEED_INVOICES)
        for inv in self.invoices:
            inv["amount"] = invoice_amount(inv)
        self.bills: list[dict] = []
        self.next_bill_id = 1
        self.next_vendor_id = max(v["id"] for v in self.vendors) + 1
        # Trap: first /portal/invoices request after reset is slow.
        self.invoices_slow_pending = True
        # Chaos switch: next valid bill save fails once with a 503.
        self.flaky_save = False

    # --- vendors -----------------------------------------------------------
    def vendor(self, vendor_id: int) -> dict | None:
        return next((v for v in self.vendors if v["id"] == vendor_id), None)

    def vendor_by_name(self, name: str) -> dict | None:
        """Exact (case-insensitive, trimmed) match only - no fuzzy matching."""
        key = name.strip().lower()
        return next((v for v in self.vendors if v["name"].lower() == key), None)

    def search_vendors(self, q: str) -> list[dict]:
        q = q.strip().lower()
        return [v for v in self.vendors if q in v["name"].lower()] if q else list(self.vendors)

    def add_vendor(self, name: str, email: str) -> dict:
        v = {"id": self.next_vendor_id, "name": name.strip(), "email": email.strip()}
        self.next_vendor_id += 1
        self.vendors.append(v)
        return v

    # --- invoices ----------------------------------------------------------
    def invoices_for(self, vendor_id: int) -> list[dict]:
        return [i for i in self.invoices if i["vendor_id"] == vendor_id]

    def invoice(self, invoice_id: str) -> dict | None:
        return next((i for i in self.invoices if i["id"] == invoice_id), None)

    # --- bills -------------------------------------------------------------
    def bill(self, bill_id: int) -> dict | None:
        return next((b for b in self.bills if b["id"] == bill_id), None)

    def bill_by_invoice_number(self, invoice_number: str) -> dict | None:
        key = invoice_number.strip().lower()
        return next((b for b in self.bills if b["invoice_number"].lower() == key), None)

    def add_bill(self, vendor_id: int, invoice_number: str, amount: float, due_date: date) -> dict:
        b = {
            "id": self.next_bill_id,
            "vendor_id": vendor_id,
            "invoice_number": invoice_number.strip(),
            "amount": round(amount, 2),
            "due_date": due_date,
            "status": "Unpaid",
        }
        self.next_bill_id += 1
        self.bills.append(b)
        return b


STORE = Store()
