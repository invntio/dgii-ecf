"""Per-company MSeller circuit breaker."""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from dgii_ecf.delivery import notify_provider_outage
from dgii_ecf.mseller.client import (
    MSellerConnectionError,
    MSellerHTTPError,
)

FAILURE_THRESHOLD = 5
FAILURE_WINDOW_MINUTES = 5
OPEN_MINUTES = 5


class ProviderCircuitOpen(MSellerConnectionError):
    pass


def _settings_name(company: str) -> str | None:
    return frappe.db.exists("ECF Provider Settings", {"company": company})


def _lock(company: str):
    return frappe.cache.lock(
        f"dgii_ecf:provider_health:{company}", timeout=15, blocking_timeout=15
    )


def before_provider_call(company: str):
    with _lock(company):
        name = _settings_name(company)
        if not name:
            return
        state, open_until = frappe.db.get_value(
            "ECF Provider Settings", name, ["circuit_state", "circuit_open_until"]
        )
        if state == "Half Open":
            raise ProviderCircuitOpen(
                f"MSeller circuit probe is already running for {company}"
            )
        if state != "Open":
            return
        now = now_datetime()
        if open_until and get_datetime(open_until) > now:
            raise ProviderCircuitOpen(
                f"MSeller circuit is open for {company} until {open_until}"
            )
        frappe.db.set_value(
            "ECF Provider Settings",
            name,
            {"circuit_state": "Half Open", "circuit_open_until": None},
            update_modified=False,
        )


def record_provider_success(company: str):
    with _lock(company):
        name = _settings_name(company)
        if not name:
            return
        frappe.db.set_value(
            "ECF Provider Settings",
            name,
            {
                "circuit_state": "Closed",
                "circuit_open_until": None,
                "consecutive_failures": 0,
                "failure_window_started_at": None,
                "last_provider_success_at": now_datetime(),
            },
            update_modified=False,
        )


def _counts_toward_circuit(exc: Exception) -> bool:
    if isinstance(exc, ProviderCircuitOpen):
        return False
    if isinstance(exc, MSellerConnectionError):
        return True
    return isinstance(exc, MSellerHTTPError) and exc.status_code >= 500


def record_provider_failure(company: str, exc: Exception):
    if not _counts_toward_circuit(exc):
        return
    with _lock(company):
        name = _settings_name(company)
        if not name:
            return
        values = frappe.db.get_value(
            "ECF Provider Settings",
            name,
            ["circuit_state", "consecutive_failures", "failure_window_started_at"],
            as_dict=True,
        )
        now = now_datetime()
        window_start = (
            get_datetime(values.failure_window_started_at)
            if values and values.failure_window_started_at
            else None
        )
        if not window_start or window_start < add_to_date(
            now, minutes=-FAILURE_WINDOW_MINUTES
        ):
            count = 1
            window_start = now
        else:
            count = (values.consecutive_failures or 0) + 1

        updates = {
            "consecutive_failures": count,
            "failure_window_started_at": window_start,
            "last_provider_failure_at": now,
        }
        was_open = bool(values and values.circuit_state == "Open")
        if count >= FAILURE_THRESHOLD:
            updates.update(
                circuit_state="Open",
                circuit_open_until=add_to_date(now, minutes=OPEN_MINUTES),
            )
        frappe.db.set_value(
            "ECF Provider Settings", name, updates, update_modified=False
        )
        if count >= FAILURE_THRESHOLD and not was_open:
            notify_provider_outage(company)


def provider_call(company: str, callback):
    before_provider_call(company)
    try:
        result = callback()
    except Exception as exc:
        record_provider_failure(company, exc)
        raise
    record_provider_success(company)
    return result
