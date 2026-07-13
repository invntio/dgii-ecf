"""Print helpers for the native Sales Invoice e-CF format."""

from __future__ import annotations

import frappe
from frappe.twofactor import get_qr_svg_code


def get_ecf_print_data(sales_invoice: str) -> dict | None:
    """Return the latest fiscal reference for a Sales Invoice.

    ECF Document Log remains the source of truth; no fiscal fields are copied to
    ERPNext's core Sales Invoice schema merely for printing.
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
        ],
        order_by="creation desc",
        limit=1,
    )
    return rows[0] if rows else None


def qr_svg_data_uri(value: str | None) -> str:
    """Render a QR value with Frappe's bundled QR library as an inline SVG."""
    if not value:
        return ""
    encoded_svg = get_qr_svg_code(value).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded_svg}"
