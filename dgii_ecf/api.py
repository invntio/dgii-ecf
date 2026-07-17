"""Whitelisted e-CF operations and resilient MSeller delivery orchestration."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from dgii_ecf.config import require_enabled
from dgii_ecf.delivery import (
    ACCEPTED_STATUSES,
    BLOCKED_STATUSES,
    REMOTE_IN_FLIGHT_STATUSES,
    apply_remote_result,
    apply_transition,
    record_event,
    record_not_found,
)
from dgii_ecf.ecf.builder import build_ecf_json, pick_ecf_type
from dgii_ecf.provider_health import provider_call
from dgii_ecf.providers.registry import get_provider
from dgii_ecf.mseller.client import (
    MSellerAuthError,
    MSellerConnectionError,
    MSellerHTTPError,
)

_MANUAL_RETRY_STATUSES = ("ERROR", "Rechazado", "UNCONFIRMED")
_BACKOFF_MINUTES = (1, 2, 5, 15, 30, 60)


def _invoice_log(sales_invoice: str):
    name = frappe.db.get_value(
        "ECF Document Log",
        {
            "direction": "Issued",
            "reference_doctype": "Sales Invoice",
            "reference_name": sales_invoice,
        },
        "name",
        order_by="creation desc",
    )
    return frappe.get_doc("ECF Document Log", name) if name else None


def _company_environment(company: str) -> str:
    """The environment the company operates in (from its provider settings).
    Sequence ranges are per-environment: TesteCF/CerteCF/eCF numbers are
    separate DGII universes."""
    env = frappe.db.get_value(
        "ECF Provider Settings", {"company": company}, "environment"
    )
    if not env:
        frappe.throw(_("No ECF Provider Settings for company {0}.").format(company))
    return env


def _sequence_expiry_for(company: str, environment: str, ecf_type: str, encf: str):
    """Expiry date of the range an already-allocated eNCF belongs to (retry path)."""
    seq = int(encf[3:])
    return frappe.db.get_value(
        "ECF Sequence Range",
        {
            "company": company,
            "environment": environment,
            "ecf_type": ecf_type,
            "sequence_from": ["<=", seq],
            "sequence_to": [">=", seq],
        },
        "expiry_date",
    )


@frappe.whitelist()
def submit_sales_invoice(sales_invoice: str) -> dict:
    """Backward-compatible worker entry point: prepare once, then deliver."""
    si = frappe.get_doc("Sales Invoice", sales_invoice)
    if not frappe.has_permission("Sales Invoice", "submit", doc=si, throw=False):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    log = prepare_sales_invoice(sales_invoice)
    if log.status == "UNCONFIRMED":
        return reconcile_ecf_log(log.name)
    return send_ecf_log(log.name)


def prepare_sales_invoice(sales_invoice: str):
    """Allocate and persist the fiscal outbox row without external I/O.

    Called inside the Sales Invoice submission transaction.  If Redis is down
    after commit, the scheduled dispatcher still sees this durable Pending row.
    """
    require_enabled()
    existing = _invoice_log(sales_invoice)
    if existing:
        return existing

    si = frappe.get_doc("Sales Invoice", sales_invoice)
    from dgii_ecf.readiness import validate_sales_invoice_readiness
    validate_sales_invoice_readiness(si)
    ecf_type = pick_ecf_type(si)
    environment = _company_environment(si.company)

    from dgii_ecf.dgii_ecf.doctype.ecf_sequence_range.ecf_sequence_range import (
        get_next_encf,
    )

    encf, range_name = get_next_encf(si.company, ecf_type, environment)
    expiry = frappe.db.get_value("ECF Sequence Range", range_name, "expiry_date")
    ecf_json = build_ecf_json(si, encf, ecf_type, sequence_expiry=expiry)
    log = frappe.get_doc(
        {
            "doctype": "ECF Document Log",
            "company": si.company,
            "direction": "Issued",
            "reference_doctype": "Sales Invoice",
            "reference_name": si.name,
            "modified_ecf_document": _modified_ecf_document(si, ecf_type),
            "ecf_type": ecf_type,
            "encf": encf,
            "status": "Pending",
            "request_json": frappe.as_json(ecf_json),
        }
    ).insert(ignore_permissions=True)
    record_event(log, "Outbox Prepared", status_after="Pending")
    return log


def send_ecf_log(ecf_log: str, force: bool = False) -> dict:
    """Send one persisted outbox row and classify failures for safe recovery."""
    log = frappe.get_doc("ECF Document Log", ecf_log)
    if log.reference_doctype == "Sales Invoice":
        invoice = frappe.get_doc("Sales Invoice", log.reference_name)
        if not frappe.has_permission("Sales Invoice", "submit", doc=invoice, throw=False):
            frappe.throw(_("Not permitted"), frappe.PermissionError)
    # Job de-duplication is an optimization; this lock is the actual cross-worker
    # guarantee that one outbox row cannot produce concurrent POSTs.
    with frappe.cache.lock(
        f"dgii_ecf:send:{ecf_log}", timeout=180, blocking_timeout=180
    ):
        return _send_ecf_log(ecf_log, force=force)


def _send_ecf_log(ecf_log: str, force: bool = False) -> dict:
    log = frappe.get_doc("ECF Document Log", ecf_log)
    if log.status in ACCEPTED_STATUSES or (
        log.status in REMOTE_IN_FLIGHT_STATUSES and not force
    ):
        return log.as_dict()
    if log.status == "UNCONFIRMED" and not force:
        return reconcile_ecf_log(log.name)

    attempt = (log.attempt_count or 0) + 1
    log = apply_transition(
        log.name,
        {
            "status": "SUBMITTING",
            "attempt_count": attempt,
            "last_attempt_at": now_datetime(),
            "next_retry_at": None,
            "last_http_status": 0,
            "error_kind": None,
            "error": None,
            "not_found_count": 0,
            "operator_action_required": 0,
            "alert_level": None,
            "last_alert_signature": None,
        },
        "POST Started",
    )
    # The outbox and attempt marker must survive a worker crash after MSeller
    # accepts the POST but before our local response handling completes.
    frappe.db.commit()

    provider = get_provider(log.company)
    try:
        res = provider_call(
            log.company,
            lambda: provider.send(frappe.parse_json(log.request_json)),
        )
    except Exception as exc:
        _record_failure(log.name, exc, attempt)
        return frappe.get_doc("ECF Document Log", log.name).as_dict()

    if not res.status:
        res.status = "RECIBIDO"
    apply_remote_result(log.name, res, event_type="POST Received")
    frappe.db.commit()
    return frappe.get_doc("ECF Document Log", log.name).as_dict()


def reconcile_ecf_log(
    ecf_log: str,
    resend_if_missing: bool = True,
    resend_remote_failure: bool = False,
) -> dict:
    """Resolve an uncertain POST by querying MSeller before retransmission."""
    log = frappe.get_doc("ECF Document Log", ecf_log)
    if log.status in ACCEPTED_STATUSES:
        return log.as_dict()
    provider = get_provider(log.company)
    try:
        results = provider_call(
            log.company, lambda: provider.get_status_batch([log.encf])
        )
    except Exception as exc:
        _record_failure(log.name, exc, log.attempt_count or 1)
        return frappe.get_doc("ECF Document Log", log.name).as_dict()

    result = next((item for item in results if item.encf == log.encf), None)
    if result and result.success:
        apply_remote_result(log.name, result, event_type="Reconciled")
        frappe.db.commit()
        if result.status in BLOCKED_STATUSES and resend_remote_failure:
            return send_ecf_log(log.name, force=True)
        return frappe.get_doc("ECF Document Log", log.name).as_dict()

    if log.status in BLOCKED_STATUSES and not resend_remote_failure:
        apply_transition(
            log.name,
            {
                "status": log.status,
                "last_status_checked_at": now_datetime(),
                "next_retry_at": None,
                "next_status_check_at": None,
            },
            "Blocked Status Not Found",
            response={"found": False},
        )
        frappe.db.commit()
        return frappe.get_doc("ECF Document Log", log.name).as_dict()

    absence_confirmed = record_not_found(log.name)
    frappe.db.commit()
    if resend_if_missing and absence_confirmed:
        return send_ecf_log(log.name, force=True)
    return frappe.get_doc("ECF Document Log", log.name).as_dict()


def _apply_result(log_name: str, res, fallback_status: str | None = None):
    if not res.status and fallback_status:
        res.status = fallback_status
    apply_remote_result(log_name, res)
    frappe.db.commit()


def _record_failure(log_name: str, exc: Exception, attempt: int):
    status, kind, http_status, retry = _classify_failure(exc)
    apply_transition(
        log_name,
        {
            "status": status,
            "error_kind": kind,
            "last_http_status": http_status or 0,
            "next_retry_at": _next_retry_at(attempt) if retry else None,
            "error": str(exc)[:1000],
        },
        "Delivery Failed",
        response={"error": str(exc)},
    )
    frappe.db.commit()
    frappe.logger("dgii_ecf").warning(f"MSeller delivery {log_name}: {kind}: {exc}")


def _classify_failure(exc: Exception) -> tuple[str, str, int | None, bool]:
    if isinstance(exc, MSellerConnectionError):
        return "UNCONFIRMED", "Connection", None, True
    if isinstance(exc, MSellerAuthError):
        return "ERROR", "Authentication", exc.status_code, False
    if isinstance(exc, MSellerHTTPError):
        if exc.status_code == 429:
            return "UNCONFIRMED", "Rate Limit", 429, True
        if exc.status_code >= 500:
            return "UNCONFIRMED", "Provider", exc.status_code, True
        return "ERROR", "Validation", exc.status_code, False
    return "ERROR", "Unknown", None, False


def _next_retry_at(attempt: int | None):
    index = min(max((attempt or 1) - 1, 0), len(_BACKOFF_MINUTES) - 1)
    return add_to_date(now_datetime(), minutes=_BACKOFF_MINUTES[index])


@frappe.whitelist()
def get_sales_invoice_ecf_state(sales_invoice: str) -> dict:
    """Return the small amount of state needed by the Sales Invoice form."""
    si = frappe.get_doc("Sales Invoice", sales_invoice)
    if not frappe.has_permission("Sales Invoice", "submit", doc=si, throw=False):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    from dgii_ecf.events.sales_invoice import is_configured

    if si.docstatus != 1 or not is_configured(si.company):
        return {"can_retry": False}

    log = frappe.db.get_value(
        "ECF Document Log",
        {
            "direction": "Issued",
            "reference_doctype": "Sales Invoice",
            "reference_name": si.name,
        },
        [
            "name",
            "status",
            "encf",
            "error",
            "error_kind",
            "next_retry_at",
            "next_status_check_at",
            "attempt_count",
            "operator_action_required",
            "alert_level",
        ],
        as_dict=True,
    )
    return {
        "can_retry": not log or log.status in _MANUAL_RETRY_STATUSES,
        "can_refresh": bool(log and log.status not in ACCEPTED_STATUSES),
        "log": log,
    }


@frappe.whitelist()
def refresh_sales_invoice_ecf_status(sales_invoice: str) -> dict:
    """Query the provider without ever retransmitting the document."""
    si = frappe.get_doc("Sales Invoice", sales_invoice)
    if not frappe.has_permission("Sales Invoice", "submit", doc=si, throw=False):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    log = _invoice_log(si.name)
    if not log:
        return {"found": False}
    if log.status in ACCEPTED_STATUSES:
        return {"found": True, "log": log.as_dict()}
    result = reconcile_ecf_log(log.name, resend_if_missing=False)
    return {"found": True, "log": result}


@frappe.whitelist()
def retry_sales_invoice(sales_invoice: str) -> dict:
    """Safely requeue e-CF creation for a submitted invoice.

    The worker itself is idempotent. ``deduplicate`` also prevents two rapid
    button clicks from running concurrent jobs before the first log exists.
    """
    si = frappe.get_doc("Sales Invoice", sales_invoice)
    if not frappe.has_permission("Sales Invoice", "submit", doc=si, throw=False):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if si.docstatus != 1:
        frappe.throw(_("Only submitted invoices can be sent as e-CF."))

    require_enabled()
    from dgii_ecf.events.sales_invoice import is_configured

    if not is_configured(si.company):
        frappe.throw(_("Electronic invoicing is disabled for this company."))

    # Fail in the user's request with an actionable readiness message instead
    # of queueing a job that is guaranteed to fail invisibly in the worker.
    from dgii_ecf.readiness import validate_sales_invoice_readiness

    validate_sales_invoice_readiness(si)

    log = prepare_sales_invoice(si.name)
    frappe.enqueue(
        "dgii_ecf.api.reconcile_ecf_log",
        queue="long",
        job_id=f"ecf-submit-{si.name}",
        deduplicate=True,
        ecf_log=log.name,
        resend_if_missing=True,
        resend_remote_failure=True,
    )
    return {"queued": True}


@frappe.whitelist()
def validate_only(sales_invoice: str) -> dict:
    """Dry-run against MSeller (`?validate=true`) — consumes no eNCF."""
    require_enabled()
    si = frappe.get_doc("Sales Invoice", sales_invoice)
    if not frappe.has_permission("Sales Invoice", "submit", doc=si, throw=False):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    ecf_type = pick_ecf_type(si)
    placeholder_encf = f"E{ecf_type}0000000000"
    # Use the active range's expiry so type-31 validation sees a plausible value.
    expiry = frappe.db.get_value(
        "ECF Sequence Range",
        {
            "company": si.company,
            "environment": _company_environment(si.company),
            "ecf_type": ecf_type,
            "status": "Active",
        },
        "expiry_date",
    )
    ecf_json = build_ecf_json(si, placeholder_encf, ecf_type, sequence_expiry=expiry)
    provider = get_provider(si.company)
    res = provider_call(
        si.company, lambda: provider.send(ecf_json, validate=True)
    )
    return {"valid": res.success, "error": res.error, "raw": res.raw}


@frappe.whitelist()
def query_status(encf: str) -> dict:
    """Refresh one document's status from the gateway and update its log."""
    log_name = frappe.db.exists("ECF Document Log", {"encf": encf})
    if not log_name:
        frappe.throw(_("No e-CF log for {0}").format(encf))
    log = frappe.get_doc("ECF Document Log", log_name)
    if not frappe.has_permission("ECF Document Log", "read", doc=log, throw=False):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    provider = get_provider(log.company)
    res = provider_call(log.company, lambda: provider.get_status(encf))
    if res.status:
        apply_remote_result(log.name, res, event_type="Manual Status Refresh")
        frappe.db.commit()
    return frappe.get_doc("ECF Document Log", log.name).as_dict()


def _modified_ecf_document(si, ecf_type: str) -> str | None:
    if ecf_type not in ("33", "34") or not si.return_against:
        return None
    rows = frappe.get_all(
        "ECF Document Log",
        filters={
            "direction": "Issued",
            "reference_doctype": "Sales Invoice",
            "reference_name": si.return_against,
            "encf": ["is", "set"],
        },
        pluck="name",
        order_by="creation desc",
        limit=1,
    )
    return rows[0] if rows else None
