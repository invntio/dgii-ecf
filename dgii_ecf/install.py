"""Installation and migration invariants for the standalone app."""

import re

import frappe
from frappe.utils import now_datetime
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


FISCAL_CREDIT_FIELD = "dgii_ecf_requires_fiscal_credit"


def make_custom_fields():
    """Install namespaced, upgrade-safe e-CF preferences on core documents."""
    create_custom_fields(
        {
            "Customer": [
                {
                    "fieldname": FISCAL_CREDIT_FIELD,
                    "fieldtype": "Check",
                    "label": "Requires Fiscal Credit Receipt",
                    "description": "",
                    "insert_after": "tax_category",
                    "default": "0",
                    "depends_on": (
                        'eval:frappe.boot.sysdefaults.country === "Dominican Republic"'
                    ),
                }
            ],
            "Sales Invoice": [
                {
                    "fieldname": FISCAL_CREDIT_FIELD,
                    "fieldtype": "Check",
                    "label": "Requires Fiscal Credit Receipt",
                    "description": "",
                    "insert_after": "tax_id",
                    "default": "0",
                    "fetch_from": f"customer.{FISCAL_CREDIT_FIELD}",
                    "fetch_if_empty": 1,
                    "print_hide": 1,
                    "hidden": 1,
                }
            ],
        },
        update=True,
    )


def ensure_module_owner():
    """Keep installation metadata and regional custom fields synchronized."""
    if frappe.db.exists("Module Def", "DGII ECF"):
        frappe.db.set_value(
            "Module Def",
            "DGII ECF",
            "app_name",
            "dgii_ecf",
            update_modified=False,
        )
    make_custom_fields()
    ensure_naming_series_counters()
    backfill_delivery_metadata()
    backfill_delivery_event_integrity()


def ensure_naming_series_counters():
    """Move legacy format names onto isolated, collision-safe series counters."""
    for doctype, prefix in (
        ("ECF Document Log", "ECF-LOG-"),
        ("ECF Delivery Event", "ECF-EVENT-"),
    ):
        if not frappe.db.exists("DocType", doctype):
            continue
        highest = 0
        pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
        for name in frappe.get_all(doctype, pluck="name"):
            if match := pattern.fullmatch(name):
                highest = max(highest, int(match.group(1)))
        series_row = frappe.db.sql(
            "SELECT current FROM `tabSeries` WHERE name = %s",
            prefix,
            pluck=True,
        )
        current = int(series_row[0] or 0) if series_row else 0
        if current >= highest:
            continue
        if series_row:
            frappe.db.sql(
                "UPDATE `tabSeries` SET current = %s WHERE name = %s",
                (highest, prefix),
            )
        else:
            frappe.db.sql(
                "INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, %s)",
                (prefix, highest),
            )


def backfill_delivery_metadata():
    """Upgrade existing outbox rows without sending or notifying documents."""
    if not frappe.db.exists("DocType", "ECF Document Log"):
        return
    from dgii_ecf.delivery import (
        BLOCKED_STATUSES,
        REMOTE_IN_FLIGHT_STATUSES,
        request_sha256,
    )

    for row in frappe.get_all(
        "ECF Document Log",
        fields=[
            "name",
            "status",
            "request_json",
            "request_sha256",
            "last_remote_status_at",
            "next_status_check_at",
        ],
    ):
        values = {}
        if not row.request_sha256:
            values["request_sha256"] = request_sha256(row.request_json)
        if row.status in BLOCKED_STATUSES:
            values.update(
                operator_action_required=1,
                alert_level="Critical",
                next_retry_at=None,
                next_status_check_at=None,
            )
        elif row.status in REMOTE_IN_FLIGHT_STATUSES:
            reference_time = row.last_remote_status_at or now_datetime()
            values.setdefault("last_remote_status_at", reference_time)
            values.setdefault("next_status_check_at", reference_time)
        if values:
            frappe.db.set_value(
                "ECF Document Log", row.name, values, update_modified=False
            )


def backfill_delivery_event_integrity():
    """Initialize only legacy event histories that do not have a chain anchor.

    Once ``event_count`` and ``last_event_hash`` are stored on the parent log,
    that chain is evidence and must never be rewritten by a migration. A failed
    verification therefore remains visible instead of being silently healed.
    """
    if not frappe.db.exists("DocType", "ECF Delivery Event") or not frappe.db.exists(
        "DocType", "ECF Document Log"
    ):
        return

    from dgii_ecf.delivery import calculate_event_hash, event_hash_values
    from dgii_ecf.event_types import BACKFILL, EVENT_TYPES

    legacy_types = {
        "Outbox Prepared": "OUTBOX_PREPARED",
        "POST Started": "POST_STARTED",
        "POST Received": "POST_RECEIVED",
        "Remote Status": "REMOTE_STATUS",
        "Batch Status": "REMOTE_STATUS",
        "Manual Status Refresh": "REMOTE_STATUS",
        "Remote Not Found": "REMOTE_NOT_FOUND",
        "Remote Absence Confirmed": "REMOTE_ABSENCE_CONFIRMED",
        "Reconciled": "RECONCILED",
        "Blocked Status Not Found": "BLOCKED_STATUS_NOT_FOUND",
        "Stale Submission": "STALE_SUBMISSION",
        "Delivery Failed": "DELIVERY_FAILED",
        "Operational Alert": "OPERATIONAL_ALERT",
    }
    legacy_logs = frappe.db.sql(
        """
        SELECT DISTINCT
            event.ecf_document_log,
            parent.name AS parent_name,
            parent.event_count,
            parent.last_event_hash
        FROM `tabECF Delivery Event` event
        LEFT JOIN `tabECF Document Log` parent
            ON parent.name = event.ecf_document_log
        WHERE event.ecf_document_log IS NOT NULL
        ORDER BY event.ecf_document_log
        """,
        as_dict=True,
    )
    event_fields = [
        "name",
        "company",
        "ecf_document_log",
        "encf",
        "reference_doctype",
        "reference_name",
        "event_sequence",
        "event_type",
        "event_timestamp",
        "actor",
        "status_before",
        "status_after",
        "attempt_number",
        "request_sha256",
        "http_status",
        "error_kind",
        "response_json",
        "creation",
    ]

    for legacy_log in legacy_logs:
        if not legacy_log.parent_name:
            frappe.logger("dgii_ecf").warning(
                "Skipping orphaned ECF delivery events for missing log %s",
                legacy_log.ecf_document_log,
            )
            continue
        if int(legacy_log.event_count or 0) > 0 or legacy_log.last_event_hash:
            continue

        previous_hash = "0" * 64
        events = frappe.get_all(
            "ECF Delivery Event",
            filters={"ecf_document_log": legacy_log.ecf_document_log},
            fields=event_fields,
            order_by="event_timestamp, creation, name",
        )
        for sequence, event in enumerate(events, start=1):
            event.event_sequence = sequence
            event.event_type = legacy_types.get(event.event_type, event.event_type)
            if event.event_type not in EVENT_TYPES:
                event.event_type = BACKFILL
            event.previous_event_hash = previous_hash
            event.event_hash = calculate_event_hash(event_hash_values(event))
            previous_hash = event.event_hash
            frappe.db.set_value(
                "ECF Delivery Event",
                event.name,
                {
                    "event_sequence": event.event_sequence,
                    "event_type": event.event_type,
                    "previous_event_hash": event.previous_event_hash,
                    "event_hash": event.event_hash,
                },
                update_modified=False,
            )

        frappe.db.set_value(
            "ECF Document Log",
            legacy_log.ecf_document_log,
            {"event_count": len(events), "last_event_hash": previous_hash},
            update_modified=False,
        )
