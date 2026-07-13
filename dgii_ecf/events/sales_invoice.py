"""Sales Invoice events — generic e-CF submission gate and cancel guard."""

import frappe
from frappe import _

from dgii_ecf.config import is_enabled

_ECF_BLOCKING_STATUSES = (
    "Pending",
    "RECIBIDO",
    "PROCESANDO",
    "Aceptado",
    "Aceptado Condicional",
)


def is_configured(company: str) -> bool:
    if not is_enabled():
        return False
    return bool(
        frappe.db.exists("ECF Provider Settings", {"company": company, "enabled": 1})
    )


def on_submit(doc, method=None):
    """Queue the e-CF submission off the invoice's critical path.

    Companies without enabled ECF Provider Settings are skipped entirely, so
    the app is inert until configured."""
    if not is_configured(doc.company):
        return
    frappe.enqueue(
        "dgii_ecf.api.submit_sales_invoice",
        queue="long",
        job_id=f"ecf-submit-{doc.name}",
        sales_invoice=doc.name,
        enqueue_after_commit=True,
    )


def on_cancel(doc, method=None):
    """Block cancelling an invoice whose e-CF is issued or in flight at DGII.
    The correction instrument is an electronic credit note (type 34)."""
    encf = frappe.db.get_value(
        "ECF Document Log",
        {"sales_invoice": doc.name, "status": ["in", _ECF_BLOCKING_STATUSES]},
        "encf",
    )
    if encf:
        frappe.throw(
            _(
                "This invoice has e-CF {0} issued or in progress with DGII. "
                "Issue an electronic credit note (type 34) instead of cancelling it."
            ).format(encf)
        )
