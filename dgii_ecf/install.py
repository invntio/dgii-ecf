"""Installation and migration invariants for the standalone app."""

import frappe
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
