"""Per-company provider circuit breaker."""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from dgii_ecf.delivery import notify_provider_outage
from dgii_ecf.providers.errors import (
    ProviderConnectionError,
    ProviderHTTPError,
)

FAILURE_THRESHOLD = 5
FAILURE_WINDOW_MINUTES = 5
OPEN_MINUTES = 5
PROBE_LEASE_MINUTES = 2


class ProviderCircuitOpen(ProviderConnectionError):
    pass


def _settings_name(company: str) -> str | None:
    return frappe.db.exists("ECF Provider Settings", {"company": company})


def _lock(company: str):
    return frappe.cache.lock(
        f"dgii_ecf:provider_health:{company}", timeout=15, blocking_timeout=15
    )


def _probe_key(company: str) -> str:
    return f"dgii_ecf:provider_probe:{company}"


def _set_probe_lease(company: str):
    frappe.cache.set_value(
        _probe_key(company),
        1,
        expires_in_sec=PROBE_LEASE_MINUTES * 60,
    )


def _clear_probe_lease(company: str):
    frappe.cache.delete_value(_probe_key(company))


def before_provider_call(company: str):
    with _lock(company):
        name = _settings_name(company)
        if not name:
            return
        state, open_until, probe_expires_at = frappe.db.get_value(
            "ECF Provider Settings",
            name,
            ["circuit_state", "circuit_open_until", "circuit_probe_expires_at"],
        )
        now = now_datetime()
        if state == "Half Open":
            if frappe.cache.get_value(_probe_key(company)) or (
                probe_expires_at and get_datetime(probe_expires_at) > now
            ):
                raise ProviderCircuitOpen(
                    f"e-CF provider circuit probe is already running for {company}"
                )
            # The previous probe worker disappeared. Reclaim its expired lease.
            _set_probe_lease(company)
            frappe.db.set_value(
                "ECF Provider Settings",
                name,
                "circuit_probe_expires_at",
                add_to_date(now, minutes=PROBE_LEASE_MINUTES),
                update_modified=False,
            )
            return
        if state != "Open":
            return
        if open_until and get_datetime(open_until) > now:
            raise ProviderCircuitOpen(
                f"e-CF provider circuit is open for {company} until {open_until}"
            )
        _set_probe_lease(company)
        frappe.db.set_value(
            "ECF Provider Settings",
            name,
            {
                "circuit_state": "Half Open",
                "circuit_open_until": None,
                "circuit_probe_expires_at": add_to_date(
                    now, minutes=PROBE_LEASE_MINUTES
                ),
            },
            update_modified=False,
        )


def record_provider_success(company: str):
    with _lock(company):
        _clear_probe_lease(company)
        name = _settings_name(company)
        if not name:
            return
        frappe.db.set_value(
            "ECF Provider Settings",
            name,
            {
                "circuit_state": "Closed",
                "circuit_open_until": None,
                "circuit_probe_expires_at": None,
                "consecutive_failures": 0,
                "failure_window_started_at": None,
                "last_provider_success_at": now_datetime(),
            },
            update_modified=False,
        )


def _counts_toward_circuit(exc: Exception) -> bool:
    if isinstance(exc, ProviderCircuitOpen):
        return False
    if isinstance(exc, ProviderConnectionError):
        return True
    return isinstance(exc, ProviderHTTPError) and exc.status_code >= 500


def record_provider_failure(company: str, exc: Exception):
    counts_toward_circuit = _counts_toward_circuit(exc)
    should_notify = False
    with _lock(company):
        name = _settings_name(company)
        if not name:
            _clear_probe_lease(company)
            return
        values = frappe.db.get_value(
            "ECF Provider Settings",
            name,
            ["circuit_state", "consecutive_failures", "failure_window_started_at"],
            as_dict=True,
        )
        previous_state = values.circuit_state if values else "Closed"
        if not counts_toward_circuit:
            # A 4xx/429 response proves the provider is reachable. If this was
            # the single recovery probe, close the availability breaker while
            # leaving the document-level error policy to the caller.
            if previous_state == "Half Open":
                frappe.db.set_value(
                    "ECF Provider Settings",
                    name,
                    {
                        "circuit_state": "Closed",
                        "circuit_open_until": None,
                        "circuit_probe_expires_at": None,
                        "consecutive_failures": 0,
                        "failure_window_started_at": None,
                    },
                    update_modified=False,
                )
            _clear_probe_lease(company)
            return

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
        was_unhealthy = previous_state in ("Open", "Half Open")
        if previous_state == "Half Open":
            count = max(count, FAILURE_THRESHOLD)
            updates["consecutive_failures"] = count
        if count >= FAILURE_THRESHOLD or previous_state == "Half Open":
            updates.update(
                circuit_state="Open",
                circuit_open_until=add_to_date(now, minutes=OPEN_MINUTES),
                circuit_probe_expires_at=None,
            )
        frappe.db.set_value(
            "ECF Provider Settings", name, updates, update_modified=False
        )
        _clear_probe_lease(company)
        should_notify = count >= FAILURE_THRESHOLD and not was_unhealthy

    # Notification adapters may enqueue email/push and must never extend the
    # critical section that serializes provider-health state.
    if should_notify:
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
