"""Sales Invoice events — generic e-CF submission gate and cancel guard."""

import frappe
from frappe import _

from dgii_ecf.config import is_enabled, is_required_for_company

_ECF_BLOCKING_STATUSES = (
    "Pending",
    "SUBMITTING",
    "UNCONFIRMED",
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


def set_print_language(doc, method=None):
    """DGII representations are issued in Spanish for Dominican companies.

    Outside the Dominican Republic ERPNext keeps its native behavior: Sales
    Invoice language is fetched from the Customer, which is preferable to
    guessing a language from a potentially multilingual country.
    """
    country = frappe.db.get_value("Company", doc.company, "country")
    if country == "Dominican Republic":
        doc.language = "es"


def on_submit(doc, method=None):
    """Persist the fiscal outbox row, then queue delivery after commit.

    A Dominican invoice cannot be committed without first persisting its local
    e-CF/e-NCF. Delivery remains asynchronous, so a provider or network outage
    leaves a durable row for retry instead of creating a non-electronic invoice.
    """
    if not is_required_for_company(doc.company):
        return
    if not is_configured(doc.company):
        frappe.throw(
            _(
                "Electronic invoicing must be configured and enabled for company {0} "
                "before submitting Sales Invoices."
            ).format(doc.company),
            title=_("Electronic invoicing is required"),
        )
    from dgii_ecf.api import prepare_sales_invoice

    log = prepare_sales_invoice(doc.name)
    frappe.enqueue(
        "dgii_ecf.api.send_ecf_log",
        queue="long",
        job_id=f"ecf-submit-{doc.name}",
        deduplicate=True,
        ecf_log=log.name,
        enqueue_after_commit=True,
    )


def on_cancel(doc, method=None):
    """Block cancelling an invoice whose e-CF is issued or in flight at DGII.
    The correction instrument is an electronic credit note (type 34)."""
    encf = frappe.db.get_value(
        "ECF Document Log",
        {
            "direction": "Issued",
            "reference_doctype": "Sales Invoice",
            "reference_name": doc.name,
            "status": ["in", _ECF_BLOCKING_STATUSES],
        },
        "encf",
    )
    if encf:
        frappe.throw(
            _(
                "This invoice has e-CF {0} issued or in progress with DGII. "
                "Issue an electronic credit note (type 34) instead of cancelling it."
            ).format(encf)
        )
