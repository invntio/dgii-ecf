"""Provider factory. `get_provider(company)` picks the implementation from the
company's `ECF Provider Settings`. Adding a gateway = one entry in `_PROVIDERS`.
"""

from __future__ import annotations

import frappe
from frappe import _

from .base import EcfProvider
from .mseller import MSellerProvider

_PROVIDERS: dict[str, type[EcfProvider]] = {
    "MSeller": MSellerProvider,
}


def get_settings(company: str):
    name = frappe.db.exists("ECF Provider Settings", {"company": company})
    if not name:
        frappe.throw(_("No e-CF provider settings for company {0}").format(company))
    return frappe.get_doc("ECF Provider Settings", name)


def get_provider(company: str) -> EcfProvider:
    settings = get_settings(company)
    if not settings.enabled:
        frappe.throw(_("e-CF provider not enabled for company {0}").format(company))
    cls = _PROVIDERS.get(settings.provider_type)
    if not cls:
        frappe.throw(_("Unknown e-CF provider type: {0}").format(settings.provider_type))
    return cls(settings)
