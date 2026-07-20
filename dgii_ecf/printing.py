"""Print helpers for the native Dominican e-CF representation."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.twofactor import get_qr_svg_code
from frappe.utils import formatdate

from dgii_ecf.providers.errors import sanitize_provider_text
from dgii_ecf.delivery import ecf_operator_presentation

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


def get_billing_reference_print_data(sales_invoice: str) -> frappe._dict | None:
    """Return the first billing-reference extension supplied by a consumer app."""
    for path in frappe.get_hooks("dgii_ecf_billing_reference_handlers") or []:
        try:
            value = frappe.get_attr(path)(sales_invoice)
        except Exception:
            frappe.log_error(
                sanitize_provider_text(frappe.get_traceback())[:10000],
                f"e-CF billing reference handler failed: {path}",
            )
            continue
        if value:
            return frappe._dict(value)
    return None


def get_billing_property_print_data(sales_invoice: str) -> frappe._dict | None:
    """Deprecated compatibility alias for the generic extension point."""
    return get_billing_reference_print_data(sales_invoice)


def _sequence_expiry_for(company: str, ecf_type: str, encf: str) -> str | None:
    """Return the expiry of the authorized range containing ``encf``.

    Only some e-CF payload types carry ``FechaVencimientoSecuencia``. The
    printed representation still needs the range expiry for every e-NCF, so
    resolve it from the same company/environment/type range when absent.
    """
    try:
        sequence = int(encf[3:])
    except (TypeError, ValueError):
        return None

    environment = frappe.db.get_value(
        "ECF Provider Settings", {"company": company}, "environment"
    )
    if not environment:
        return None

    expiry = frappe.db.get_value(
        "ECF Sequence Range",
        {
            "company": company,
            "environment": environment,
            "ecf_type": ecf_type,
            "sequence_from": ["<=", sequence],
            "sequence_to": [">=", sequence],
        },
        "expiry_date",
    )
    return formatdate(expiry, "dd-mm-yyyy") if expiry else None


def get_ecf_print_data(sales_invoice: str) -> frappe._dict | None:
    """Build the printed representation from the exact e-CF request and log.

    The request payload supplies fiscal amounts and identifiers; Sales Invoice
    only enriches presentation fields such as UOM. ECF Document Log remains the
    source of truth and no custom fields are copied to ERPNext core doctypes.
    """
    rows = frappe.get_all(
        "ECF Document Log",
        filters={
            "direction": "Issued",
            "reference_doctype": "Sales Invoice",
            "reference_name": sales_invoice,
        },
        fields=[
            "name",
            "encf",
            "ecf_type",
            "status",
            "error_kind",
            "security_code",
            "qr_url",
            "signed_date",
            "request_json",
            "response_json",
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

    operator_presentation = ecf_operator_presentation(log)
    return frappe._dict(
        log_name=log.name,
        title=_DOCUMENT_TITLES.get(log.ecf_type, "Electronic Fiscal Invoice"),
        ecf_type=log.ecf_type,
        encf=log.encf,
        status=log.status,
        error_kind=log.error_kind,
        operator_presentation=(
            frappe._dict(operator_presentation) if operator_presentation else None
        ),
        security_code=log.security_code,
        qr_url=log.qr_url,
        signed_date=log.signed_date,
        issuer_name=issuer.get("RazonSocialEmisor") or company.company_name,
        issuer_rnc=issuer.get("RNCEmisor") or company.tax_id,
        issuer_address=_company_address(invoice.company) or issuer.get("DireccionEmisor"),
        company_logo=company.company_logo,
        issue_date=issuer.get("FechaEmision"),
        sequence_expiry=id_doc.get("FechaVencimientoSecuencia")
        or _sequence_expiry_for(invoice.company, log.ecf_type, log.encf),
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
        
    import pyqrcode
    from io import BytesIO
    from base64 import b64encode
    
    url = pyqrcode.create(value)
    stream = BytesIO()
    # Generamos el QR con fondo blanco puro y módulos negros para alto contraste
    url.svg(stream, scale=4, background="#ffffff", module_color="#000000")
    
    svg = stream.getvalue().decode("utf-8").replace("\n", "")
    encoded_svg = b64encode(svg.encode("utf-8")).decode("ascii")
    
    return f"data:image/svg+xml;base64,{encoded_svg}"
