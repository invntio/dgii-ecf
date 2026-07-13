"""Scheduled e-CF jobs: poll pending documents for their DGII verdict, and mark
expired sequence ranges. Wired in hooks.py `scheduler_events`.
"""

from __future__ import annotations

import frappe
from frappe import _

from dgii_ecf.config import is_enabled
from dgii_ecf.providers.registry import get_provider

# Non-terminal states we keep polling.
_PENDING = ("Pending", "RECIBIDO", "PROCESANDO")
_TERMINAL = ("Aceptado", "Aceptado Condicional", "Rechazado")
_BATCH = 100  # MSeller batch limit


def poll_pending_documents():
    """Refresh every non-terminal e-CF via the batch status endpoint, per company."""
    if not is_enabled():
        return
    rows = frappe.get_all(
        "ECF Document Log",
        filters={"status": ["in", _PENDING], "encf": ["is", "set"]},
        fields=["name", "company", "encf", "status"],
    )
    by_company: dict[str, list] = {}
    for r in rows:
        by_company.setdefault(r.company, []).append(r)

    for company, docs in by_company.items():
        try:
            provider = get_provider(company)
        except Exception as exc:  # settings disabled/missing — skip, don't crash the job
            frappe.logger("dgii_ecf").warning(f"poll skip {company}: {exc}")
            continue
        for i in range(0, len(docs), _BATCH):
            chunk = docs[i : i + _BATCH]
            try:
                results = provider.get_status_batch([d.encf for d in chunk])
            except Exception as exc:
                frappe.logger("dgii_ecf").error(f"poll batch {company}: {exc}")
                continue
            by_encf = {res.encf: res for res in results if res.encf}
            for d in chunk:
                res = by_encf.get(d.encf)
                if res and res.status and res.status != d.status:
                    frappe.db.set_value(
                        "ECF Document Log",
                        d.name,
                        {
                            "status": res.status,
                            "response_json": frappe.as_json(res.raw),
                        },
                        update_modified=False,
                    )
                    if res.status == "Rechazado":
                        _notify_rejection(d.name)
    frappe.db.commit()


def _notify_rejection(log_name: str):
    """Surface a DGII rejection to the invoice owner (best-practices)."""
    log = frappe.get_doc("ECF Document Log", log_name)
    if not log.sales_invoice:
        return
    owner = frappe.db.get_value("Sales Invoice", log.sales_invoice, "owner")
    if not owner:
        return
    frappe.get_doc(
        {
            "doctype": "Notification Log",
            "for_user": owner,
            "type": "Alert",
            "document_type": "Sales Invoice",
            "document_name": log.sales_invoice,
            "subject": _("e-CF {0} rejected by DGII").format(log.encf),
        }
    ).insert(ignore_permissions=True)


def expire_ranges():
    """Mark active eNCF ranges whose expiry date has passed."""
    if not is_enabled():
        return
    today = frappe.utils.today()
    expired = frappe.get_all(
        "ECF Sequence Range",
        filters={"status": "Active", "expiry_date": ["<", today]},
        pluck="name",
    )
    for name in expired:
        frappe.db.set_value("ECF Sequence Range", name, "status", "Expired")
    if expired:
        frappe.db.commit()
