"""Scheduled e-CF jobs: poll pending documents for their DGII verdict, and mark
expired sequence ranges. Wired in hooks.py `scheduler_events`.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from dgii_ecf.config import is_enabled
from dgii_ecf.delivery import (
    REMOTE_IN_FLIGHT_STATUSES,
    apply_remote_result,
    apply_transition,
    notify_stalled,
    record_not_found,
)
from dgii_ecf.provider_health import provider_call
from dgii_ecf.providers.registry import get_provider

# Non-terminal states we keep polling.
_BATCH = 100  # MSeller batch limit


def poll_pending_documents():
    """Refresh every non-terminal e-CF via the batch status endpoint, per company."""
    if not is_enabled():
        return
    rows = frappe.get_all(
        "ECF Document Log",
        filters={
            "status": ["in", REMOTE_IN_FLIGHT_STATUSES],
            "encf": ["is", "set"],
        },
        fields=[
            "name",
            "company",
            "encf",
            "status",
            "next_status_check_at",
        ],
    )
    now = now_datetime()
    rows = [
        row
        for row in rows
        if not row.next_status_check_at
        or get_datetime(row.next_status_check_at) <= now
    ]
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
                results = provider_call(
                    company,
                    lambda: provider.get_status_batch([d.encf for d in chunk]),
                )
            except Exception as exc:
                frappe.logger("dgii_ecf").error(f"poll batch {company}: {exc}")
                continue
            by_encf = {res.encf: res for res in results if res.encf}
            for d in chunk:
                res = by_encf.get(d.encf)
                if not res or not res.success:
                    record_not_found(d.name)
                    continue
                apply_remote_result(d.name, res, event_type="Batch Status")
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
            apply_transition(
                row.name,
                {"status": "UNCONFIRMED", "next_retry_at": now},
                "Stale Submission",
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


def alert_stalled_documents():
    """Create deduplicated operational alerts without changing fiscal states."""
    if not is_enabled():
        return
    now = now_datetime()
    rows = frappe.get_all(
        "ECF Document Log",
        filters={
            "status": ["in", ["UNCONFIRMED", *REMOTE_IN_FLIGHT_STATUSES]],
            "direction": "Issued",
        },
        fields=[
            "name",
            "company",
            "encf",
            "status",
            "reference_doctype",
            "reference_name",
            "request_json",
            "request_sha256",
            "attempt_count",
            "first_unconfirmed_at",
            "last_remote_status_at",
            "last_alert_signature",
        ],
    )
    for row in rows:
        if row.status == "UNCONFIRMED" and row.first_unconfirmed_at:
            if get_datetime(row.first_unconfirmed_at) <= add_to_date(now, minutes=-15):
                notify_stalled(
                    row,
                    level="Warning",
                    reason="delivery has been unconfirmed for at least 15 minutes",
                )
        elif row.status in REMOTE_IN_FLIGHT_STATUSES and row.last_remote_status_at:
            if get_datetime(row.last_remote_status_at) <= add_to_date(now, minutes=-60):
                notify_stalled(
                    row,
                    level="Critical",
                    reason="MSeller status has not progressed for at least 60 minutes",
                )
    frappe.db.commit()


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
