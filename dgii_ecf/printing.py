"""Print helpers for the native Dominican e-CF representation."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.twofactor import get_qr_svg_code
from frappe.utils import formatdate

_DOCUMENT_TITLES = {
    "31": "Electronic Fiscal Credit Invoice",
    "32": "Electronic Consumer Invoice",
    "33": "Electronic Debit Note",
    "34": "Electronic Credit Note",
    "41": "Electronic Purchases Invoice",
    "43": "Electronic Minor Expenses Invoice",
    "44": "Electronic Special Regimes Invoice",
    "45": "Electronic Government Invoice",
    "46": "Electronic Export Invoice",
    "47": "Electronic Foreign Payments Invoice",
}


def _money(value) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _company_address(company: str) -> str | None:
    name = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
        "parent",
    )
    if not name:
        return None
    address = frappe.get_doc("Address", name)
    parts = [
        address.address_line1,
        address.address_line2,
        address.city,
        address.state,
        address.country,
    ]
    return ", ".join(part for part in parts if part)


def get_ecf_print_data(sales_invoice: str) -> frappe._dict | None:
    """Build the printed representation from the exact e-CF request and log.

    The request payload supplies fiscal amounts and identifiers; Sales Invoice
    only enriches presentation fields such as UOM. ECF Document Log remains the
    source of truth and no custom fields are copied to ERPNext core doctypes.
    """
    rows = frappe.get_all(
        "ECF Document Log",
        filters={"sales_invoice": sales_invoice},
        fields=[
            "name",
            "encf",
            "ecf_type",
            "status",
            "security_code",
            "qr_url",
            "signed_date",
            "request_json",
        ],
        order_by="creation desc",
        limit=1,
    )
    if not rows:
        return None

    log = rows[0]
    invoice = frappe.get_doc("Sales Invoice", sales_invoice)
    company = frappe.get_cached_doc("Company", invoice.company)
    request = frappe.parse_json(log.request_json or "{}")
    ecf = request.get("ECF", {})
    header = ecf.get("Encabezado", {})
    id_doc = header.get("IdDoc", {})
    issuer = header.get("Emisor", {})
    buyer = header.get("Comprador", {})
    totals = header.get("Totales", {})
    reference = ecf.get("InformacionReferencia", {})

    items = []
    payload_items = ecf.get("DetallesItems", {}).get("Item", [])
    for index, item in enumerate(payload_items):
        source_item = invoice.items[index] if index < len(invoice.items) else None
        amount = _money(item.get("MontoItem"))
        tax_rate = 18 if item.get("IndicadorFacturacion") == 1 else 0
        items.append(
            frappe._dict(
                quantity=item.get("CantidadItem"),
                description=item.get("NombreItem"),
                uom=source_item.uom if source_item else "",
                rate=_money(item.get("PrecioUnitarioItem")),
                tax=_money(amount * tax_rate / 100),
                amount=amount,
            )
        )

    return frappe._dict(
        log_name=log.name,
        title=_DOCUMENT_TITLES.get(log.ecf_type, "Electronic Fiscal Invoice"),
        ecf_type=log.ecf_type,
        encf=log.encf,
        status=log.status,
        security_code=log.security_code,
        qr_url=log.qr_url,
        signed_date=log.signed_date,
        issuer_name=issuer.get("RazonSocialEmisor") or company.company_name,
        issuer_rnc=issuer.get("RNCEmisor") or company.tax_id,
        issuer_address=_company_address(invoice.company) or issuer.get("DireccionEmisor"),
        company_logo=company.company_logo,
        issue_date=issuer.get("FechaEmision"),
        sequence_expiry=id_doc.get("FechaVencimientoSecuencia"),
        payment_due=id_doc.get("FechaLimitePago")
        or (formatdate(invoice.due_date, "dd-mm-yyyy") if invoice.due_date else None),
        buyer_name=buyer.get("RazonSocialComprador") or invoice.customer_name,
        buyer_rnc=buyer.get("RNCComprador"),
        modified_encf=reference.get("NCFModificado"),
        correction_reason=reference.get("RazonModificacion"),
        lines=items,
        taxable_total=_money(totals.get("MontoGravadoTotal")),
        exempt_total=_money(totals.get("MontoExento")),
        tax_total=_money(totals.get("TotalITBIS")),
        grand_total=_money(totals.get("MontoTotal")),
        currency=invoice.currency,
    )


def qr_svg_data_uri(value: str | None) -> str:
    """Render a QR value with Frappe's bundled QR library as an inline SVG."""
    if not value:
        return ""
    encoded_svg = get_qr_svg_code(value).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded_svg}"
