"""Desk dashboard connections for the e-CF module.

The fiscal reference lives in ECF Document Log (no custom fields on core
doctypes), so the Sales Invoice form surfaces it through the native Connections
dashboard instead.
"""


def sales_invoice_dashboard(data):
    data.setdefault("non_standard_fieldnames", {})["ECF Document Log"] = "sales_invoice"
    data.setdefault("transactions", []).append(
        {"label": "DGII e-CF", "items": ["ECF Document Log"]}
    )
    return data
