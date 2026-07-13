"""Whitelisted e-CF operations. `submit_sales_invoice` is what the async job
(dgii_ecf.events.sales_invoice.on_submit) enqueues; `validate_only` is a safe dry-run.
"""

from __future__ import annotations

import frappe
from frappe import _

from dgii_ecf.config import require_enabled
from dgii_ecf.ecf.builder import build_ecf_json, pick_ecf_type
from dgii_ecf.providers.registry import get_provider

# Statuses that mean "don't re-submit this invoice".
_LIVE_STATUSES = ("Pending", "RECIBIDO", "PROCESANDO", "Aceptado", "Aceptado Condicional")


def _existing_live_log(sales_invoice: str) -> str | None:
    return frappe.db.exists(
        "ECF Document Log",
        {"sales_invoice": sales_invoice, "status": ["in", _LIVE_STATUSES]},
    )


def _company_environment(company: str) -> str:
    """The environment the company operates in (from its provider settings).
    Sequence ranges are per-environment: TesteCF/CerteCF/eCF numbers are
    separate DGII universes."""
    env = frappe.db.get_value(
        "ECF Provider Settings", {"company": company}, "environment"
    )
    if not env:
        frappe.throw(_("No ECF Provider Settings for company {0}.").format(company))
    return env


def _sequence_expiry_for(company: str, environment: str, ecf_type: str, encf: str):
    """Expiry date of the range an already-allocated eNCF belongs to (retry path)."""
    seq = int(encf[3:])
    return frappe.db.get_value(
        "ECF Sequence Range",
        {
            "company": company,
            "environment": environment,
            "ecf_type": ecf_type,
            "sequence_from": ["<=", seq],
            "sequence_to": [">=", seq],
        },
        "expiry_date",
    )


@frappe.whitelist()
def submit_sales_invoice(sales_invoice: str) -> dict:
    """Allocate an eNCF, build the e-CF, send it, and log the result.

    Idempotent: if a non-terminal-failure log already exists for this invoice, it is
    returned instead of issuing a second eNCF. Raises on transient send errors so the
    enqueuing layer can retry.
    """
    require_enabled()
    existing = _existing_live_log(sales_invoice)
    if existing:
        return frappe.get_doc("ECF Document Log", existing).as_dict()

    si = frappe.get_doc("Sales Invoice", sales_invoice)
    ecf_type = pick_ecf_type(si)
    environment = _company_environment(si.company)

    # A failed previous attempt (ERROR) keeps its eNCF: the retry is the SAME
    # document, so it must reuse the number — allocating a fresh one would burn
    # an authorized sequence per retry (and MSeller may already hold the first).
    retry_log = frappe.db.exists(
        "ECF Document Log", {"sales_invoice": sales_invoice, "status": "ERROR"}
    )
    if retry_log:
        log = frappe.get_doc("ECF Document Log", retry_log)
        encf = log.encf
        expiry = _sequence_expiry_for(si.company, environment, ecf_type, encf)
        ecf_json = build_ecf_json(si, encf, ecf_type, sequence_expiry=expiry)
        log.db_set("status", "Pending")
        log.db_set("error", None)
        log.db_set("request_json", frappe.as_json(ecf_json))
    else:
        # Allocate the eNCF inside this transaction (row-locked); see the sequence
        # range controller. A duplicate eNCF is rejected by DGII and burns an
        # authorized number.
        from dgii_ecf.dgii_ecf.doctype.ecf_sequence_range.ecf_sequence_range import (
            get_next_encf,
        )

        encf, range_name = get_next_encf(si.company, ecf_type, environment)
        expiry = frappe.db.get_value("ECF Sequence Range", range_name, "expiry_date")
        ecf_json = build_ecf_json(si, encf, ecf_type, sequence_expiry=expiry)

        log = frappe.get_doc(
            {
                "doctype": "ECF Document Log",
                "company": si.company,
                "sales_invoice": si.name,
                "ecf_type": ecf_type,
                "encf": encf,
                "status": "Pending",
                "request_json": frappe.as_json(ecf_json),
            }
        ).insert(ignore_permissions=True)

    provider = get_provider(si.company)
    try:
        res = provider.send(ecf_json)
    except Exception as exc:
        log.db_set("status", "ERROR")
        log.db_set("error", str(exc)[:1000])
        frappe.db.commit()
        raise  # let the job retry transient failures

    log.db_set("status", res.status or "RECIBIDO")
    log.db_set("internal_track_id", res.track_id)
    log.db_set("security_code", res.security_code)
    log.db_set("qr_url", res.qr_url)
    log.db_set("signed_date", res.signed_date)
    log.db_set("response_json", frappe.as_json(res.raw))
    log.db_set("error", res.error)
    frappe.db.commit()
    return log.as_dict()


@frappe.whitelist()
def validate_only(sales_invoice: str) -> dict:
    """Dry-run against MSeller (`?validate=true`) — consumes no eNCF."""
    require_enabled()
    si = frappe.get_doc("Sales Invoice", sales_invoice)
    ecf_type = pick_ecf_type(si)
    placeholder_encf = f"E{ecf_type}0000000000"
    # Use the active range's expiry so type-31 validation sees a plausible value.
    expiry = frappe.db.get_value(
        "ECF Sequence Range",
        {
            "company": si.company,
            "environment": _company_environment(si.company),
            "ecf_type": ecf_type,
            "status": "Active",
        },
        "expiry_date",
    )
    ecf_json = build_ecf_json(si, placeholder_encf, ecf_type, sequence_expiry=expiry)
    res = get_provider(si.company).send(ecf_json, validate=True)
    return {"valid": res.success, "error": res.error, "raw": res.raw}


@frappe.whitelist()
def query_status(encf: str) -> dict:
    """Refresh one document's status from the gateway and update its log."""
    log_name = frappe.db.exists("ECF Document Log", {"encf": encf})
    if not log_name:
        frappe.throw(_("No e-CF log for {0}").format(encf))
    log = frappe.get_doc("ECF Document Log", log_name)
    res = get_provider(log.company).get_status(encf)
    if res.status:
        log.db_set("status", res.status)
        log.db_set("response_json", frappe.as_json(res.raw))
        frappe.db.commit()
    return log.as_dict()
