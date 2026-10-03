"""Mock company environment: a vendor portal and an internal finance system.

Run: python -m uvicorn mock_env.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import io
import re
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from mock_env.data import STORE

app = FastAPI(title="Mock Company Environment")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def portal_date(d: date) -> str:
    """Portal displays dates as DD/MM/YYYY (finance needs YYYY-MM-DD)."""
    return d.strftime("%d/%m/%Y")


def money(amount: float) -> str:
    return f"{amount:,.2f}"


templates.env.filters["portal_date"] = portal_date
templates.env.filters["money"] = money


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code, message=exc.detail)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return render(request, "home.html")


@app.post("/reset")
async def reset():
    STORE.reset()
    return {"status": "reset"}


class ChaosConfig(BaseModel):
    flaky_save: bool = False


@app.post("/chaos")
async def chaos(cfg: ChaosConfig):
    STORE.flaky_save = cfg.flaky_save
    return {"flaky_save": STORE.flaky_save}


# ---------------------------------------------------------------- portal ---

@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request, q: str = ""):
    return render(request, "portal_index.html", q=q, vendors=STORE.search_vendors(q))


@app.get("/portal/invoices", response_class=HTMLResponse)
async def portal_invoices(request: Request, company: str = ""):
    if STORE.invoices_slow_pending:
        STORE.invoices_slow_pending = False
        await asyncio.sleep(2)
    vendor = STORE.vendor_by_name(company)
    if vendor is None:
        raise HTTPException(404, f"No vendor named '{company}'. Use the vendor search at /portal.")
    return render(request, "portal_invoices.html", vendor=vendor, invoices=STORE.invoices_for(vendor["id"]))


def _get_invoice(invoice_id: str) -> tuple[dict, dict]:
    inv = STORE.invoice(invoice_id)
    if inv is None:
        raise HTTPException(404, f"Invoice {invoice_id} not found")
    return inv, STORE.vendor(inv["vendor_id"])


@app.get("/portal/invoice/{invoice_id}", response_class=HTMLResponse)
async def portal_invoice(request: Request, invoice_id: str):
    inv, vendor = _get_invoice(invoice_id)
    return render(request, "portal_invoice.html", inv=inv, vendor=vendor)


@app.get("/portal/invoice/{invoice_id}/pdf")
async def portal_invoice_pdf(invoice_id: str):
    inv, vendor = _get_invoice(invoice_id)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    y = height - 25 * mm

    def line(text: str, size: int = 11, gap: float = 7) -> None:
        nonlocal y
        c.setFont("Helvetica", size)
        c.drawString(20 * mm, y, text)
        y -= gap * mm

    line("INVOICE", 20, 12)
    line(f"Vendor: {vendor['name']}")
    line(f"Invoice number: {inv['id']}")
    line(f"Issue date: {portal_date(inv['issue_date'])}")
    line(f"Due date: {portal_date(inv['due_date'])}", gap=12)
    line("Line items:", 12)
    for li in inv["line_items"]:
        line(f"  {li['description']}  x{li['qty']}  @ {money(li['unit_price'])}"
             f"  = {money(li['qty'] * li['unit_price'])}")
    y -= 5 * mm
    line(f"Total amount: {inv['currency']} {money(inv['amount'])}", 13)
    c.showPage()
    c.save()
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{inv["id"]}.pdf"'},
    )


# --------------------------------------------------------------- finance ---

def _bill_view(b: dict) -> dict:
    vendor = STORE.vendor(b["vendor_id"])
    return {**b, "vendor": vendor["name"] if vendor else "?", "due_date": b["due_date"].isoformat()}


@app.get("/finance")
async def finance_home():
    return RedirectResponse("/finance/bills", status_code=302)


@app.get("/finance/bills", response_class=HTMLResponse)
async def finance_bills(request: Request):
    return render(request, "bills.html", bills=[_bill_view(b) for b in STORE.bills])


@app.get("/finance/bills/new", response_class=HTMLResponse)
async def finance_bill_new(request: Request, vendor_id: int | None = None):
    return render(request, "bill_new.html", vendors=STORE.vendors, error=None,
                  form={"vendor_id": vendor_id, "invoice_number": "", "amount": "", "due_date": ""})


@app.post("/finance/bills", response_class=HTMLResponse)
async def finance_bill_create(
    request: Request,
    vendor_id: str = Form(""),
    invoice_number: str = Form(""),
    amount: str = Form(""),
    due_date: str = Form(""),
):
    form = {"vendor_id": int(vendor_id) if vendor_id.isdigit() else None,
            "invoice_number": invoice_number, "amount": amount, "due_date": due_date}

    def fail(msg: str) -> HTMLResponse:
        return render(request, "bill_new.html", status_code=400, vendors=STORE.vendors, error=msg, form=form)

    vendor = STORE.vendor(form["vendor_id"]) if form["vendor_id"] else None
    if vendor is None:
        return fail("Please select a vendor")
    if not invoice_number.strip():
        return fail("Invoice number is required")
    try:
        amount_val = float(amount)
        if amount_val <= 0:
            raise ValueError
    except ValueError:
        return fail("Amount must be a positive number")
    try:
        if not DATE_RE.match(due_date.strip()):
            raise ValueError
        due = datetime.strptime(due_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        return fail("Invalid date format, use YYYY-MM-DD")
    if STORE.bill_by_invoice_number(invoice_number):
        return fail("Duplicate bill")

    if STORE.flaky_save:  # chaos: fail once without saving
        STORE.flaky_save = False
        return render(request, "error.html", status_code=503,
                      message="Service temporarily unavailable, please try again")

    bill = STORE.add_bill(vendor["id"], invoice_number, amount_val, due)
    return RedirectResponse(f"/finance/bills/{bill['id']}", status_code=303)


@app.get("/finance/bills/{bill_id}", response_class=HTMLResponse)
async def finance_bill_detail(request: Request, bill_id: int):
    b = STORE.bill(bill_id)
    if b is None:
        raise HTTPException(404, f"Bill {bill_id} not found")
    return render(request, "bill_detail.html", bill=_bill_view(b))


@app.post("/finance/bills/{bill_id}/pay")
async def finance_bill_pay(bill_id: int):
    b = STORE.bill(bill_id)
    if b is None:
        raise HTTPException(404, f"Bill {bill_id} not found")
    b["status"] = "Paid"
    return RedirectResponse(f"/finance/bills/{bill_id}", status_code=303)


@app.get("/finance/vendors/new", response_class=HTMLResponse)
async def finance_vendor_new(request: Request):
    return render(request, "vendor_new.html", error=None, form={"name": "", "email": ""})


@app.post("/finance/vendors", response_class=HTMLResponse)
async def finance_vendor_create(request: Request, name: str = Form(""), email: str = Form("")):
    form = {"name": name, "email": email}
    if not name.strip():
        return render(request, "vendor_new.html", status_code=400, error="Vendor name is required", form=form)
    if STORE.vendor_by_name(name):
        return render(request, "vendor_new.html", status_code=400, error="Vendor already exists", form=form)
    v = STORE.add_vendor(name, email)
    return RedirectResponse(f"/finance/bills/new?vendor_id={v['id']}", status_code=303)


# ------------------------------------------------------------------- api ---

@app.get("/api/bills")
async def api_bills():
    return [_bill_view(b) for b in STORE.bills]


@app.get("/api/vendors")
async def api_vendors():
    return STORE.vendors


@app.get("/api/invoices")
async def api_invoices():
    return [
        {
            "invoice_number": i["id"],
            "vendor": STORE.vendor(i["vendor_id"])["name"],
            "issue_date": i["issue_date"].isoformat(),
            "due_date": i["due_date"].isoformat(),
            "amount": i["amount"],
            "currency": i["currency"],
            "status": i["status"],
        }
        for i in STORE.invoices
    ]
