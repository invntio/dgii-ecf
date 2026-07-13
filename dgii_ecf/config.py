"""Runtime feature gate for e-CF submission.

Installing the app creates its schema, but provider traffic is opt-in through
the ``dgii_ecf_enabled`` site config key.
"""

import frappe
from frappe.utils import cint


def is_enabled() -> bool:
    return bool(cint(frappe.conf.get("dgii_ecf_enabled", 0)))


def require_enabled() -> None:
    if not is_enabled():
        frappe.throw("e-CF is disabled for this site.", frappe.ValidationError)
