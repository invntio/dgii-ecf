"""Installation and migration invariants for the standalone app."""

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
    backfill_delivery_metadata()


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
