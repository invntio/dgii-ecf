"""Runtime feature gate for e-CF submission.

Installing the app creates its schema, but provider traffic is opt-in through
the ``dgii_ecf_enabled`` site config key.
"""

import frappe
from frappe.utils import cint


def is_enabled() -> bool:
    return bool(cint(frappe.conf.get("dgii_ecf_enabled", 0)))


def is_required_for_company(company: str | None) -> bool:
    """Whether submitted Sales Invoices for this company require an e-CF.

    The site feature gate enables the integration. Dominican companies are
    then treated as electronic issuers and must finish their per-company setup
    before an invoice can be submitted. Drafts remain available for normal
    accounting preparation.
    """
    if not is_enabled() or not company:
        return False
    return frappe.db.get_value("Company", company, "country") == "Dominican Republic"


def require_enabled() -> None:
    if not is_enabled():
        frappe.throw("e-CF is disabled for this site.", frappe.ValidationError)
