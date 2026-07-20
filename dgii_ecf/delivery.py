"""Durable delivery state, audit events, and operator notifications."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from dgii_ecf.alerts import dispatch_operational_alert
from dgii_ecf.event_types import (
    ALERT_DELIVERED,
    ALERT_FAILED,
    OPERATIONAL_ALERT,
    RECOVERED,
    REMOTE_ABSENCE_CONFIRMED,
    REMOTE_NOT_FOUND,
    REMOTE_STATUS,
    validate_event_type,
)
from dgii_ecf.providers.errors import (
    sanitize_provider_data,
    sanitize_provider_text,
)

ACCEPTED_STATUSES = ("Aceptado", "Aceptado Condicional")
REMOTE_IN_FLIGHT_STATUSES = ("RECIBIDO", "PROCESANDO")
BLOCKED_STATUSES = ("Rechazado", "ERROR")
RECONCILE_STATUSES = ("SUBMITTING", "UNCONFIRMED")
STATUS_CHECK_MINUTES = (1, 2, 5, 15, 30, 60)
NOT_FOUND_GRACE_MINUTES = 2

_ZERO_HASH = "0" * 64
_MAX_RESPONSE_CHARS = 10000
_TRANSIENT_DGII_REJECTION_CODES = {"004"}


def dgii_response_details(value: Any) -> dict[str, Any]:
    """Extract operator-safe DGII messages from an MSeller status response."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {"codes": [], "messages": [], "sequence_used": None}
    if not isinstance(value, dict):
        return {"codes": [], "messages": [], "sequence_used": None}

    data = value.get("data") if isinstance(value.get("data"), dict) else value
    responses = data.get("dgiiResponse") or value.get("dgiiResponse") or []
    if not isinstance(responses, list):
        responses = [responses]

    codes: list[str] = []
    messages: list[str] = []
    sequence_used = None
    for response in responses:
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except (TypeError, ValueError):
                continue
        if not isinstance(response, dict):
            continue
        if "secuenciaUtilizada" in response:
            sequence_used = bool(response.get("secuenciaUtilizada"))
        for message in response.get("mensajes") or []:
            if not isinstance(message, dict):
                continue
            code = str(message.get("codigo") or "").strip()
            text = str(message.get("valor") or "").strip()
            if code:
                codes.append(code)
            if text:
                messages.append(text)
    return {
        "codes": list(dict.fromkeys(codes)),
        "messages": list(dict.fromkeys(messages)),
        "sequence_used": sequence_used,
    }


def ecf_operator_presentation(log) -> dict[str, Any] | None:
    """Describe provider state without replacing its auditable fiscal status."""
    details = dgii_response_details(log.response_json)
    transient = (
        log.status == "Rechazado"
        and bool(_TRANSIENT_DGII_REJECTION_CODES.intersection(details["codes"]))
        and details["sequence_used"] is False
    )
    if not transient:
        return None
    return {
        "kind": "temporary_validation_error",
        "indicator": "orange",
        "title": _("Temporary DGII validation error"),
        "message": _(
            "DGII could not complete validation. The accounting invoice remains "
            "submitted, but this e-CF is not fiscally valid yet. Do not create "
            "another invoice or eNCF; wait a few minutes, refresh its status, and "
            "retry delivery if the error persists."
        ),
        "codes": details["codes"],
        "provider_messages": details["messages"],
        "sequence_used": details["sequence_used"],
    }


def request_sha256(request_json: str | None) -> str:
    return hashlib.sha256((request_json or "").encode("utf-8")).hexdigest()


def sanitized_text(value: Any, max_chars: int = 1000) -> str:
    """Return bounded, redacted text safe for persistence and operator output."""
    return sanitize_provider_text(value)[:max_chars]


def sanitized_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return sanitized_text(value, _MAX_RESPONSE_CHARS)
    serialized = frappe.as_json(sanitize_provider_data(value))
    if len(serialized) <= _MAX_RESPONSE_CHARS:
        return serialized
    # Preserve valid JSON when a provider returns an unexpectedly large body.
    # 3,500 characters remain safely below the bound even when every character
    # requires JSON escaping.
    return frappe.as_json(
        {"truncated": True, "preview": serialized[:3500]}
    )


def calculate_event_hash(values: dict[str, Any]) -> str:
    canonical = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def event_hash_values(event) -> dict[str, Any]:
    """Return the canonical, tamper-evident representation of an event row."""
    return {
        "company": event.company,
        "ecf_document_log": event.ecf_document_log,
        "encf": event.encf,
        "reference_doctype": event.reference_doctype,
        "reference_name": event.reference_name,
        "event_sequence": int(event.event_sequence or 0),
        "event_type": event.event_type,
        "event_timestamp": event.event_timestamp,
        "actor": event.actor,
        "status_before": event.status_before,
        "status_after": event.status_after,
        "attempt_number": int(event.attempt_number or 0),
        "request_sha256": event.request_sha256,
        "http_status": int(event.http_status or 0),
        "error_kind": event.error_kind,
        "response_json": event.response_json,
        "previous_event_hash": event.previous_event_hash or _ZERO_HASH,
    }


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
    event_type = validate_event_type(event_type)
    # Serialize sequence allocation with every state transition for this log.
    frappe.db.sql(
        "SELECT name FROM `tabECF Document Log` WHERE name = %s FOR UPDATE",
        log.name,
    )
    previous = frappe.get_all(
        "ECF Delivery Event",
        filters={"ecf_document_log": log.name},
        fields=["event_sequence", "event_hash"],
        order_by="event_sequence desc",
        limit=1,
    )
    sequence = int(previous[0].event_sequence or 0) + 1 if previous else 1
    previous_hash = previous[0].event_hash if previous else _ZERO_HASH
    timestamp = now_datetime()
    resolved_actor = actor or (
        "Scheduler"
        if getattr(frappe.flags, "in_scheduler", False)
        else (frappe.session.user or "Worker")
    )
    doc = frappe.get_doc(
        {
            "doctype": "ECF Delivery Event",
            "company": log.company,
            "ecf_document_log": log.name,
            "encf": log.encf,
            "reference_doctype": log.reference_doctype,
            "reference_name": log.reference_name,
            "event_sequence": sequence,
            "event_type": event_type,
            "event_timestamp": timestamp,
            "actor": resolved_actor,
            "status_before": status_before,
            "status_after": status_after,
            "attempt_number": log.attempt_count or 0,
            "request_sha256": log.request_sha256 or request_sha256(log.request_json),
            "http_status": http_status or 0,
            "error_kind": error_kind,
            "response_json": sanitized_json(response),
            "previous_event_hash": previous_hash,
        }
    )
    doc.event_hash = calculate_event_hash(event_hash_values(doc))
    doc.flags.from_delivery_service = True
    doc.insert(ignore_permissions=True)
    frappe.db.set_value(
        "ECF Document Log",
        log.name,
        {"last_event_hash": doc.event_hash, "event_count": sequence},
        update_modified=False,
    )
    log.last_event_hash = doc.event_hash
    log.event_count = sequence
    return doc


def verify_event_chain(log_name: str) -> dict[str, Any]:
    """Verify sequence continuity, hashes, and the head stored on the parent log."""
    previous_hash = _ZERO_HASH
    errors: list[str] = []
    events = frappe.get_all(
        "ECF Delivery Event",
        filters={"ecf_document_log": log_name},
        fields=["*"],
        order_by="event_sequence asc",
    )
    for expected_sequence, event in enumerate(events, start=1):
        if int(event.event_sequence or 0) != expected_sequence:
            errors.append(f"sequence:{event.name}")
        if event.previous_event_hash != previous_hash:
            errors.append(f"previous_hash:{event.name}")
        expected_hash = calculate_event_hash(event_hash_values(event))
        if event.event_hash != expected_hash:
            errors.append(f"event_hash:{event.name}")
        previous_hash = event.event_hash or previous_hash

    parent = frappe.db.get_value(
        "ECF Document Log",
        log_name,
        ["event_count", "last_event_hash"],
        as_dict=True,
    )
    if not parent:
        errors.append("missing_parent")
    else:
        if int(parent.event_count or 0) != len(events):
            errors.append("parent_event_count")
        expected_head = previous_hash if events else None
        if (parent.last_event_hash or None) != expected_head:
            errors.append("parent_last_event_hash")
    return {"valid": not errors, "event_count": len(events), "errors": errors}


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
    # Lock before reading the current state. Otherwise two status workers can
    # both capture the same stale `status_before` value even though their writes
    # are serialized later by InnoDB.
    frappe.db.sql(
        "SELECT name FROM `tabECF Document Log` WHERE name = %s FOR UPDATE",
        log_name,
    )
    log = frappe.get_doc("ECF Document Log", log_name)
    before = log.status
    previous_alert_level = log.alert_level
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
            first_unconfirmed_at=None,
            not_found_count=0,
            next_retry_at=None,
            status_check_count=check_count,
            next_status_check_at=_next_status_check(check_count),
        )
        if changed:
            values.update(alert_level=None, last_alert_signature=None)
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
    elif previous_alert_level and (
        after in ACCEPTED_STATUSES
        or (after in REMOTE_IN_FLIGHT_STATUSES and after != before)
    ):
        notify_recovered(log, previous_status=before, previous_level=previous_alert_level)
    return log


def apply_remote_result(log_name: str, result, event_type: str = REMOTE_STATUS):
    log = frappe.get_doc("ECF Document Log", log_name)
    now = now_datetime()
    values = {
        "last_status_checked_at": now,
        "last_http_status": 0,
        "error_kind": None,
        "error": sanitized_text(result.error) if result.error else None,
        "response_json": sanitized_json(result.raw),
        "not_found_count": 0,
    }
    details = dgii_response_details(result.raw)
    if (
        result.status == "Rechazado"
        and bool(_TRANSIENT_DGII_REJECTION_CODES.intersection(details["codes"]))
        and details["sequence_used"] is False
    ):
        values.update(
            error_kind="Provider",
            error=_("DGII temporary validation error (code 004). Retry later."),
        )
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
        apply_transition(log.name, values, REMOTE_NOT_FOUND)
        return False

    apply_transition(
        log.name,
        {
            "status": "UNCONFIRMED",
            "not_found_count": count + 1,
            "last_status_checked_at": now,
            "next_retry_at": None,
        },
        REMOTE_ABSENCE_CONFIRMED,
    )
    return True


def _alert_payload(
    log,
    *,
    event_id: str,
    kind: str,
    severity: str,
    title: str,
    message: str,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "kind": kind,
        "severity": severity,
        "company": log.company,
        "ecf_log": log.name,
        "encf": log.encf,
        "status": log.status,
        "reference_doctype": log.reference_doctype,
        "reference_name": log.reference_name,
        "title": title,
        "message": message,
    }


def _record_alert_result(log, result: dict[str, Any]):
    record_event(
        log,
        ALERT_DELIVERED if result.get("delivered") else ALERT_FAILED,
        status_before=log.status,
        status_after=log.status,
        response=result,
    )


def notify_blocked(log, *, response: Any = None):
    response_text = sanitized_json(response) or log.response_json or log.error or ""
    signature = hashlib.sha256(
        f"blocked|{log.status}|{log.error_kind or ''}|{response_text}".encode("utf-8")
    ).hexdigest()
    if log.last_alert_signature == signature:
        return True

    title = _("e-CF {0} requires action: {1}").format(log.encf, log.status)
    normalized_cause = log.error_kind or _("Remote rejection")
    provider_detail = (
        sanitized_text(log.error, 1500)
        or response_text
        or _("No provider detail")
    )
    message = (
        f"{_('Invoice')}: {log.reference_name}\n"
        f"eNCF: {log.encf}\n"
        f"{_('Status')}: {log.status}\n"
        f"{_('Cause')}: {normalized_cause}\n"
        f"{_('Provider response')}: {str(provider_detail)[:1500]}\n\n"
        f"{_('Open the linked invoice and its e-CF log before retrying.')}"
    )
    record_event(
        log,
        OPERATIONAL_ALERT,
        status_before=log.status,
        status_after=log.status,
        response={"kind": "blocked", "severity": "Critical"},
    )
    result = dispatch_operational_alert(
        _alert_payload(
            log,
            event_id=signature,
            kind="blocked",
            severity="Critical",
            title=title,
            message=message,
        )
    )
    _record_alert_result(log, result)
    if result.get("delivered"):
        frappe.db.set_value(
            "ECF Document Log",
            log.name,
            {"last_alert_signature": signature, "alert_level": "Critical"},
            update_modified=False,
        )
        log.last_alert_signature = signature
        log.alert_level = "Critical"
    return bool(result.get("delivered"))


def notify_stalled(log, *, level: str, reason: str):
    signature = hashlib.sha256(
        f"stalled|{log.status}|{level}|{reason}".encode("utf-8")
    ).hexdigest()
    if log.last_alert_signature == signature:
        return False
    title = _("e-CF {0} is stalled: {1}").format(log.encf, reason)
    message = (
        f"{_('Invoice')}: {log.reference_name}\n"
        f"eNCF: {log.encf}\n"
        f"{_('Status')}: {log.status}\n"
        f"{_('Reason')}: {reason}"
    )
    record_event(
        log,
        OPERATIONAL_ALERT,
        status_before=log.status,
        status_after=log.status,
        response={"level": level, "reason": reason},
    )
    result = dispatch_operational_alert(
        _alert_payload(
            log,
            event_id=signature,
            kind="stalled",
            severity=level,
            title=title,
            message=message,
        )
    )
    _record_alert_result(log, result)
    if result.get("delivered"):
        frappe.db.set_value(
            "ECF Document Log",
            log.name,
            {"last_alert_signature": signature, "alert_level": level},
            update_modified=False,
        )
        log.last_alert_signature = signature
        log.alert_level = level
    return bool(result.get("delivered"))


def notify_recovered(log, *, previous_status: str, previous_level: str):
    signature = hashlib.sha256(
        f"recovered|{log.name}|{previous_status}|{log.status}".encode("utf-8")
    ).hexdigest()
    title = _("e-CF {0} delivery recovered").format(log.encf)
    message = (
        f"{_('Invoice')}: {log.reference_name}\n"
        f"eNCF: {log.encf}\n"
        f"{_('Previous status')}: {previous_status}\n"
        f"{_('Current status')}: {log.status}"
    )
    record_event(
        log,
        RECOVERED,
        status_before=previous_status,
        status_after=log.status,
        response={"previous_alert_level": previous_level},
    )
    result = dispatch_operational_alert(
        _alert_payload(
            log,
            event_id=signature,
            kind="recovered",
            severity="Info",
            title=title,
            message=message,
        )
    )
    _record_alert_result(log, result)
    if result.get("delivered"):
        frappe.db.set_value(
            "ECF Document Log",
            log.name,
            {"last_alert_signature": signature, "alert_level": None},
            update_modified=False,
        )
        log.last_alert_signature = signature
        log.alert_level = None
    return bool(result.get("delivered"))


def notify_provider_outage(company: str):
    title = _("Electronic invoicing provider is temporarily unavailable for {0}.").format(
        company
    )
    signature = hashlib.sha256(f"provider_outage|{company}".encode("utf-8")).hexdigest()
    return dispatch_operational_alert(
        {
            "event_id": signature,
            "kind": "provider_outage",
            "severity": "Critical",
            "company": company,
            "title": title,
            "message": _(
                "Automatic e-CF delivery is paused temporarily. Persisted documents will be reconciled after provider recovery."
            ),
        }
    )
