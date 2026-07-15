"""Scheduled e-CF jobs: poll pending documents for their DGII verdict, and mark
expired sequence ranges. Wired in hooks.py `scheduler_events`.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from dgii_ecf.config import is_enabled
from dgii_ecf.providers.registry import get_provider

# Non-terminal states we keep polling.
_PENDING = ("RECIBIDO", "PROCESANDO")
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
                if not res or not res.success:
                    continue
                values = {
                    key: value
                    for key, value in {
                        "internal_track_id": res.track_id,
                        "security_code": res.security_code,
                        "qr_url": res.qr_url,
                        "signed_date": res.signed_date,
                        "signed_xml_path": res.signed_xml_path,
                    }.items()
                    if value is not None
                }
                status_changed = bool(res.status and res.status != d.status)
                if status_changed:
                    values.update(
                        status=res.status,
                        response_json=frappe.as_json(res.raw),
                    )
                if values:
                    frappe.db.set_value(
                        "ECF Document Log",
                        d.name,
                        values,
                        update_modified=False,
                    )
                if status_changed and res.status == "Rechazado":
                    _notify_rejection(d.name)
    frappe.db.commit()


def retry_failed_documents():
    """Recover durable outbox rows and reconcile uncertain submissions.

    Definitive authentication/validation failures have no next_retry_at and are
    intentionally excluded.  Stale SUBMITTING means a worker may have died after
    MSeller accepted the request, so it is queried before any retransmission.
    """
    if not is_enabled():
        return
    now = now_datetime()
    stale_before = add_to_date(now, minutes=-10)
    rows = frappe.get_all(
        "ECF Document Log",
        filters={
            "status": ["in", ["Pending", "SUBMITTING", "UNCONFIRMED"]],
            "direction": "Issued",
            "reference_doctype": "Sales Invoice",
            "reference_name": ["is", "set"],
        },
        fields=["name", "reference_name", "status", "last_attempt_at", "next_retry_at"],
    )
    for row in rows:
        if row.status == "SUBMITTING":
            if not row.last_attempt_at or get_datetime(row.last_attempt_at) > stale_before:
                continue
            frappe.db.set_value(
                "ECF Document Log",
                row.name,
                {"status": "UNCONFIRMED", "next_retry_at": now},
                update_modified=False,
            )
            row.status = "UNCONFIRMED"
        if row.status == "UNCONFIRMED" and (
            not row.next_retry_at or get_datetime(row.next_retry_at) > now
        ):
            continue

        method = (
            "dgii_ecf.api.send_ecf_log"
            if row.status == "Pending"
            else "dgii_ecf.api.reconcile_ecf_log"
        )
        kwargs = {"ecf_log": row.name}
        if row.status == "UNCONFIRMED":
            kwargs.update(resend_if_missing=True, resend_remote_failure=False)
        frappe.enqueue(
            method,
            queue="long",
            job_id=f"ecf-submit-{row.reference_name}",
            deduplicate=True,
            **kwargs,
        )


def _notify_rejection(log_name: str):
    """Surface a DGII rejection to the invoice owner (best-practices)."""
    log = frappe.get_doc("ECF Document Log", log_name)
    if log.reference_doctype != "Sales Invoice" or not log.reference_name:
        return
    owner = frappe.db.get_value("Sales Invoice", log.reference_name, "owner")
    if not owner:
        return
    frappe.get_doc(
        {
            "doctype": "Notification Log",
            "for_user": owner,
            "type": "Alert",
            "document_type": "Sales Invoice",
            "document_name": log.reference_name,
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
