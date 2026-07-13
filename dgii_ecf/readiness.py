"""Structured preflight checks for electronic fiscal invoice submission."""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import getdate, today

from dgii_ecf.ecf.builder import pick_ecf_type


def _missing(code: str, section: str, fieldname: str, label: str, reason: str) -> dict:
    return {
        "code": code,
        "section": section,
        "fieldname": fieldname,
        "label": label,
        "reason": reason,
    }


def _company_address(company: str) -> str | None:
    return frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
        "parent",
    )


def get_ecf_readiness(company: str, ecf_type: str) -> dict:
    """Return all configuration problems for ``company`` and an e-CF type."""
    missing = []
    company_doc = frappe.get_cached_doc("Company", company)
    rnc = re.sub(r"\D", "", company_doc.tax_id or "")
    if len(rnc) not in (9, 11):
        missing.append(_missing(
            "company_tax_id_invalid", "Company", "tax_id", "RNC/Tax ID",
            "A Dominican issuer tax ID must contain 9 or 11 digits.",
        ))

    address_name = _company_address(company)
    address_line = frappe.db.get_value("Address", address_name, "address_line1") if address_name else None
    if not address_line:
        missing.append(_missing(
            "company_address_missing", "Fiscal Data", "address_line1", "Fiscal Address",
            "No linked fiscal address was found for the company.",
        ))

    settings_name = frappe.db.exists("ECF Provider Settings", {"company": company})
    if not settings_name:
        missing.append(_missing(
            "provider_settings_missing", "Electronic Invoicing", "company", "ECF Provider",
            "Electronic invoicing has not been configured for this company.",
        ))
        return {"ready": False, "ecf_type": ecf_type, "missing": missing}

    settings = frappe.get_doc("ECF Provider Settings", settings_name)
    if not settings.enabled:
        missing.append(_missing(
            "provider_disabled", "Electronic Invoicing", "enabled", "ECF Provider",
            "Electronic invoicing is disabled for this company.",
        ))

    environment = settings.environment
    key_field = {
        "TesteCF": "api_key_testecf",
        "CerteCF": "api_key_certecf",
        "eCF": "api_key_ecf",
    }.get(environment)
    if not key_field or not settings.get_password(key_field, raise_exception=False):
        missing.append(_missing(
            "api_key_missing", "Electronic Invoicing", key_field or "environment", "API Key",
            f"The API key for {environment or 'the selected environment'} is missing.",
        ))

    if settings.use_custom_credentials:
        has_login = bool(settings.email and settings.get_password("password", raise_exception=False))
    else:
        gateway = frappe.get_single("ECF Gateway Account")
        has_login = bool(gateway.email and gateway.get_password("password", raise_exception=False))
    if not has_login:
        missing.append(_missing(
            "gateway_login_missing", "Electronic Invoicing", "email", "Gateway Login",
            "No usable gateway login is configured.",
        ))

    ranges = frappe.get_all(
        "ECF Sequence Range",
        filters={"company": company, "environment": environment, "ecf_type": ecf_type},
        fields=["name", "status", "sequence_from", "sequence_to", "current", "expiry_date"],
        order_by="expiry_date desc, sequence_from asc",
    )
    usable = [
        row for row in ranges
        if row.status == "Active"
        and (row.current or 0) < row.sequence_to
        and row.expiry_date
        and getdate(row.expiry_date) >= getdate(today())
    ]
    if not usable:
        if ranges and all(row.expiry_date and getdate(row.expiry_date) < getdate(today()) for row in ranges):
            latest = ranges[0].expiry_date
            code, reason = "sequence_expired", f"The authorized type {ecf_type} sequence expired on {latest}."
        elif ranges and all((row.current or 0) >= row.sequence_to or row.status == "Exhausted" for row in ranges):
            code, reason = "sequence_exhausted", f"The authorized type {ecf_type} sequence is exhausted."
        else:
            code, reason = "sequence_missing", f"No active type {ecf_type} sequence is available."
        missing.append(_missing(
            code, "Electronic Invoicing", "expiry_date", f"e-NCF Type {ecf_type} Sequence", reason,
        ))

    return {"ready": not missing, "ecf_type": ecf_type, "missing": missing}


@frappe.whitelist()
def sales_invoice_readiness(sales_invoice: str) -> dict:
    invoice = frappe.get_doc("Sales Invoice", sales_invoice)
    if not frappe.has_permission("Sales Invoice", "read", doc=invoice, throw=False):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    return get_ecf_readiness(invoice.company, pick_ecf_type(invoice))


def validate_sales_invoice_readiness(doc, method=None) -> None:
    """Block submit with one actionable message containing every missing field."""
    settings = frappe.db.exists("ECF Provider Settings", {"company": doc.company, "enabled": 1})
    if not settings:
        return
    result = get_ecf_readiness(doc.company, pick_ecf_type(doc))
    if result["ready"]:
        return
    items = "".join(
        f"<li><strong>{_(item['section'])} → {_(item['label'])}:</strong> {_(item['reason'])}</li>"
        for item in result["missing"]
    )
    frappe.throw(
        _("This invoice cannot be submitted for e-CF. Correct the following fields:")
        + f"<ul>{items}</ul>",
        title=_("Electronic invoicing is not ready"),
    )
