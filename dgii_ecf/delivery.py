"""Durable delivery state, audit events, and operator notifications."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

ACCEPTED_STATUSES = ("Aceptado", "Aceptado Condicional")
REMOTE_IN_FLIGHT_STATUSES = ("RECIBIDO", "PROCESANDO")
BLOCKED_STATUSES = ("Rechazado", "ERROR")
RECONCILE_STATUSES = ("SUBMITTING", "UNCONFIRMED")
STATUS_CHECK_MINUTES = (1, 2, 5, 15, 30, 60)
NOT_FOUND_GRACE_MINUTES = 2

_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "password",
    "token",
    "idtoken",
    "access_token",
    "refresh_token",
}


def request_sha256(request_json: str | None) -> str:
    return hashlib.sha256((request_json or "").encode("utf-8")).hexdigest()


def _sanitized(value: Any):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).casefold() in _SENSITIVE_KEYS else _sanitized(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitized(item) for item in value]
    return value


def sanitized_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value[:10000]
    return frappe.as_json(_sanitized(value))[:10000]


def record_event(
    log,
    event_type: str,
    *,
    status_before: str | None = None,
    status_after: str | None = None,
    http_status: int | None = None,
    error_kind: str | None = None,
    response: Any = None,
    actor: str | None = None,
):
    """Append an audit event in the same transaction as the state change."""
    if not frappe.db.exists("DocType", "ECF Delivery Event"):
        return None
    return frappe.get_doc(
        {
            "doctype": "ECF Delivery Event",
            "company": log.company,
            "ecf_document_log": log.name,
            "encf": log.encf,
            "reference_doctype": log.reference_doctype,
            "reference_name": log.reference_name,
            "event_type": event_type,
            "event_timestamp": now_datetime(),
            "actor": actor or frappe.session.user or "Scheduler",
            "status_before": status_before,
            "status_after": status_after,
            "attempt_number": log.attempt_count or 0,
            "request_sha256": log.request_sha256 or request_sha256(log.request_json),
            "http_status": http_status or 0,
            "error_kind": error_kind,
            "response_json": sanitized_json(response),
        }
    ).insert(ignore_permissions=True)


def _next_status_check(check_count: int | None):
    index = min(max(check_count or 0, 0), len(STATUS_CHECK_MINUTES) - 1)
    return add_to_date(now_datetime(), minutes=STATUS_CHECK_MINUTES[index])


def apply_transition(
    log_name: str,
    values: dict,
    event_type: str,
    *,
    response: Any = None,
):
    """Apply one normalized transition and enforce polling/blocked invariants."""
    log = frappe.get_doc("ECF Document Log", log_name)
    before = log.status
    after = values.get("status", before)
    now = now_datetime()

    if not log.request_sha256:
        values.setdefault("request_sha256", request_sha256(log.request_json))

    if after in ACCEPTED_STATUSES:
        values.update(
            operator_action_required=0,
            alert_level=None,
            last_alert_signature=None,
            next_retry_at=None,
            next_status_check_at=None,
            not_found_count=0,
            first_unconfirmed_at=None,
        )
    elif after in BLOCKED_STATUSES:
        values.update(
            operator_action_required=1,
            alert_level="Critical",
            next_retry_at=None,
            next_status_check_at=None,
            first_unconfirmed_at=None,
        )
    elif after in REMOTE_IN_FLIGHT_STATUSES:
        changed = after != before
        check_count = 0 if changed else (log.status_check_count or 0) + 1
        values.update(
            operator_action_required=0,
            alert_level=None,
            last_alert_signature=None,
            first_unconfirmed_at=None,
            not_found_count=0,
            next_retry_at=None,
            status_check_count=check_count,
            next_status_check_at=_next_status_check(check_count),
        )
        if changed or not log.last_remote_status_at:
            values["last_remote_status_at"] = now
    elif after == "UNCONFIRMED":
        values.update(
            operator_action_required=0,
            next_status_check_at=None,
        )
        if not log.first_unconfirmed_at:
            values["first_unconfirmed_at"] = now
    elif after in ("Pending", "SUBMITTING"):
        values.update(
            operator_action_required=0,
            next_status_check_at=None,
        )

    frappe.db.set_value(
        "ECF Document Log", log.name, values, update_modified=False
    )
    for key, value in values.items():
        setattr(log, key, value)
    record_event(
        log,
        event_type,
        status_before=before,
        status_after=after,
        http_status=values.get("last_http_status"),
        error_kind=values.get("error_kind"),
        response=response,
    )
    if after in BLOCKED_STATUSES:
        notify_blocked(log, response=response)
    return log


def apply_remote_result(log_name: str, result, event_type: str = "Remote Status"):
    log = frappe.get_doc("ECF Document Log", log_name)
    now = now_datetime()
    values = {
        "last_status_checked_at": now,
        "last_http_status": 0,
        "error_kind": None,
        "error": result.error,
        "response_json": sanitized_json(result.raw),
        "not_found_count": 0,
    }
    if result.status:
        values["status"] = result.status
    for fieldname, value in {
        "internal_track_id": result.track_id,
        "security_code": result.security_code,
        "qr_url": result.qr_url,
        "signed_date": result.signed_date,
        "signed_xml_path": result.signed_xml_path,
    }.items():
        if value is not None:
            values[fieldname] = value
    if result.status and (result.status != log.status or not log.last_remote_status_at):
        values["last_remote_status_at"] = now
    return apply_transition(log_name, values, event_type, response=result.raw)


def record_not_found(log_name: str) -> bool:
    """Record an absence observation; return True only after two spaced misses."""
    log = frappe.get_doc("ECF Document Log", log_name)
    now = now_datetime()
    previous_check = get_datetime(log.last_status_checked_at) if log.last_status_checked_at else None
    count = log.not_found_count or 0
    grace_elapsed = bool(
        count >= 1
        and previous_check
        and previous_check <= add_to_date(now, minutes=-NOT_FOUND_GRACE_MINUTES)
    )
    if not grace_elapsed:
        values = {
            "status": "UNCONFIRMED",
            "not_found_count": max(count, 1),
            "last_status_checked_at": now,
            "next_retry_at": add_to_date(now, minutes=NOT_FOUND_GRACE_MINUTES),
        }
        apply_transition(log.name, values, "Remote Not Found")
        return False

    apply_transition(
        log.name,
        {
            "status": "UNCONFIRMED",
            "not_found_count": count + 1,
            "last_status_checked_at": now,
            "next_retry_at": None,
        },
        "Remote Absence Confirmed",
    )
    return True


def manager_users(company: str) -> list[str]:
    candidates = set(
        frappe.get_all(
            "User Permission",
            filters={"allow": "Company", "for_value": company},
            pluck="user",
        )
    )
    if not candidates:
        return []
    role_users = set(
        frappe.get_all(
            "Has Role",
            filters={
                "parenttype": "User",
                "role": "Condo Manager",
                "parent": ["in", list(candidates)],
            },
            pluck="parent",
        )
    )
    return sorted(
        user
        for user in role_users
        if frappe.db.get_value("User", user, "enabled")
    )


def system_manager_users() -> list[str]:
    users = set(
        frappe.get_all(
            "Has Role",
            filters={"parenttype": "User", "role": "System Manager"},
            pluck="parent",
        )
    )
    users.add("Administrator")
    return sorted(
        user
        for user in users
        if user == "Administrator" or frappe.db.get_value("User", user, "enabled")
    )


def _create_notifications(
    users: list[str], subject: str, log=None, content: str | None = None
):
    for user in sorted(set(users)):
        frappe.get_doc(
            {
                "doctype": "Notification Log",
                "for_user": user,
                "type": "Alert",
                "document_type": log.reference_doctype if log else None,
                "document_name": log.reference_name if log else None,
                "subject": subject,
                "email_content": content,
            }
        ).insert(ignore_permissions=True)


def notify_blocked(log, *, response: Any = None):
    response_text = sanitized_json(response) or log.response_json or log.error or ""
    signature = hashlib.sha256(
        f"blocked|{log.status}|{log.error_kind or ''}|{response_text}".encode("utf-8")
    ).hexdigest()
    if log.last_alert_signature == signature:
        return

    users = manager_users(log.company)
    if log.error_kind in ("Authentication", "Provider") or not users:
        users.extend(system_manager_users())
    subject = _("e-CF {0} requires action: {1}").format(log.encf, log.status)
    cause = log.error_kind or log.error or response_text or _("No provider detail")
    content = (
        f"{_('Invoice')}: {log.reference_name}\n"
        f"eNCF: {log.encf}\n"
        f"{_('Status')}: {log.status}\n"
        f"{_('Cause/provider response')}: {str(cause)[:1500]}\n\n"
        f"{_('Open the linked invoice and its e-CF log before retrying.')}"
    )
    _create_notifications(users, subject, log=log, content=content)
    frappe.db.set_value(
        "ECF Document Log",
        log.name,
        {"last_alert_signature": signature, "alert_level": "Critical"},
        update_modified=False,
    )
    log.last_alert_signature = signature
    log.alert_level = "Critical"


def notify_stalled(log, *, level: str, reason: str):
    signature = hashlib.sha256(
        f"stalled|{log.status}|{level}|{reason}".encode("utf-8")
    ).hexdigest()
    if log.last_alert_signature == signature:
        return False
    users = manager_users(log.company) or system_manager_users()
    subject = _("e-CF {0} is stalled: {1}").format(log.encf, reason)
    _create_notifications(
        users,
        subject,
        log=log,
        content=(
            f"{_('Invoice')}: {log.reference_name}\n"
            f"eNCF: {log.encf}\n"
            f"{_('Status')}: {log.status}\n"
            f"{_('Reason')}: {reason}"
        ),
    )
    frappe.db.set_value(
        "ECF Document Log",
        log.name,
        {"last_alert_signature": signature, "alert_level": level},
        update_modified=False,
    )
    record_event(
        log,
        "Operational Alert",
        status_before=log.status,
        status_after=log.status,
        response={"level": level, "reason": reason},
    )
    return True


def notify_provider_outage(company: str):
    users = manager_users(company) + system_manager_users()
    _create_notifications(
        users,
        _("Electronic invoicing provider is temporarily unavailable for {0}.").format(
            company
        ),
    )
