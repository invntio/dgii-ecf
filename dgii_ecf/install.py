"""Installation and migration invariants for the standalone app."""

import frappe


def ensure_module_owner():
    """Keep the existing DGII ECF Module Def owned by this extracted app."""
    if frappe.db.exists("Module Def", "DGII ECF"):
        frappe.db.set_value(
            "Module Def",
            "DGII ECF",
            "app_name",
            "dgii_ecf",
            update_modified=False,
        )
