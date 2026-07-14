"""Desk dashboard connections for the e-CF module.

The fiscal reference lives in ECF Document Log (no custom fields on core
doctypes), so the Sales Invoice form surfaces it through the native Connections
dashboard instead.
"""


def sales_invoice_dashboard(data):
    data.setdefault("non_standard_fieldnames", {})["ECF Document Log"] = "reference_name"
    data.setdefault("dynamic_links", {})["reference_name"] = [
        "Sales Invoice",
        "reference_doctype",
    ]
    data.setdefault("transactions", []).append(
        {"label": "DGII e-CF", "items": ["ECF Document Log"]}
    )
    return data
