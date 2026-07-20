"""Scheduled e-CF jobs: poll pending documents for their DGII verdict, and mark
expired sequence ranges. Wired in hooks.py `scheduler_events`.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from dgii_ecf.config import is_enabled
from dgii_ecf.delivery import (
    BLOCKED_STATUSES,
    REMOTE_IN_FLIGHT_STATUSES,
    apply_remote_result,
    apply_transition,
    notify_stalled,
    record_not_found,
    sanitized_text,
)
from dgii_ecf.event_types import REMOTE_STATUS, STALE_SUBMISSION
from dgii_ecf.provider_health import provider_call
from dgii_ecf.providers.registry import get_provider


def poll_pending_documents():
    """Refresh due non-terminal e-CFs via each provider's batch endpoint."""
    if not is_enabled():
        return
    now = now_datetime()
    rows = frappe.db.sql(
        """
        SELECT name, company, encf, status, next_status_check_at
        FROM `tabECF Document Log`
        WHERE status IN %(statuses)s
          AND encf IS NOT NULL
          AND encf != ''
          AND (
              next_status_check_at IS NULL
              OR next_status_check_at <= %(now)s
          )
        ORDER BY company, next_status_check_at, name
        """,
        {"statuses": REMOTE_IN_FLIGHT_STATUSES, "now": now},
        as_dict=True,
    )
    by_company: dict[str, list] = {}
    for r in rows:
        by_company.setdefault(r.company, []).append(r)

    for company, docs in by_company.items():
        try:
            provider = get_provider(company)
        except Exception as exc:  # settings disabled/missing — skip, don't crash the job
            frappe.logger("dgii_ecf").warning(
                f"poll skip {company}: {sanitized_text(exc)}"
            )
            continue
        batch_size = getattr(provider, "status_batch_size", 100)
        if not isinstance(batch_size, int) or batch_size < 1:
            batch_size = 100
        for i in range(0, len(docs), batch_size):
            chunk = docs[i : i + batch_size]
            try:
                results = provider_call(
                    company,
                    lambda: provider.get_status_batch([d.encf for d in chunk]),
                )
            except Exception as exc:
                frappe.logger("dgii_ecf").error(
                    f"poll batch {company}: {sanitized_text(exc)}"
                )
                continue
            by_encf = {res.encf: res for res in results if res.encf}
            for d in chunk:
                res = by_encf.get(d.encf)
                if (
                    res
                    and not res.success
                    and isinstance(res.raw, dict)
                    and res.raw.get("found") is False
                ):
                    record_not_found(d.name)
                    continue
                if not res or not res.success:
                    apply_transition(
                        d.name,
                        {
                            "status": d.status,
                            "last_status_checked_at": now_datetime(),
                            "error_kind": "Provider",
                            "error": "Provider batch response omitted this eNCF or returned an ambiguous item.",
                        },
                        REMOTE_STATUS,
                        response={"batch_result": "omitted_or_ambiguous"},
                    )
                    continue
                apply_remote_result(d.name, res, event_type=REMOTE_STATUS)
    frappe.db.commit()


def retry_failed_documents():
    """Recover durable outbox rows and reconcile uncertain submissions.

    Definitive authentication/validation failures have no next_retry_at and are
    intentionally excluded.  Stale SUBMITTING means a worker may have died after
    the provider accepted the request, so it is queried before any retransmission.
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
                STALE_SUBMISSION,
            )
            row.status = "UNCONFIRMED"
            row.next_retry_at = now
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
                    reason="provider status has not progressed for at least 60 minutes",
                )

    blocked = frappe.get_all(
        "ECF Document Log",
        filters={
            "status": ["in", BLOCKED_STATUSES],
            "direction": "Issued",
            "operator_action_required": 1,
            "last_alert_signature": ["is", "not set"],
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
            "error_kind",
            "error",
            "response_json",
            "last_alert_signature",
            "alert_level",
        ],
    )
    from dgii_ecf.delivery import notify_blocked

    for row in blocked:
        notify_blocked(row)
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
