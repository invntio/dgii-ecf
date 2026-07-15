"""Desk dashboard connections for the e-CF module.

The fiscal reference lives in ECF Document Log (no custom fields on core
doctypes), so the Sales Invoice form surfaces it through the native Connections
dashboard instead.
"""


def _append_ecf_connection(data, reference_doctype):
    data.setdefault("non_standard_fieldnames", {})["ECF Document Log"] = "reference_name"
    data.setdefault("dynamic_links", {})["reference_name"] = [
        reference_doctype,
        "reference_doctype",
    ]
    data.setdefault("transactions", []).append(
        {"label": "DGII e-CF", "items": ["ECF Document Log"]}
    )
    return data


def sales_invoice_dashboard(data):
    return _append_ecf_connection(data, "Sales Invoice")


def purchase_invoice_dashboard(data):
    return _append_ecf_connection(data, "Purchase Invoice")
