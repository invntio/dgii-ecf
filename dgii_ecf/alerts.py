"""Provider-agnostic operational alert dispatch.

Consumer applications can register ``dgii_ecf_operational_alert_handlers`` in
their hooks. A handler receives a plain dictionary and returns either a truthy
value or ``{"delivered": bool}``. No consumer app is imported from here.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from dgii_ecf.providers.errors import sanitize_provider_text

ALERT_HANDLERS_HOOK = "dgii_ecf_operational_alert_handlers"


def _system_manager_users() -> list[str]:
    users = set(
        frappe.get_all(
            "Has Role",
            filters={"parenttype": "User", "role": "System Manager"},
            pluck="parent",
        )
    )
    users.add("Administrator")
    return sorted(
        user
        for user in users
        if user == "Administrator" or frappe.db.get_value("User", user, "enabled")
    )


def _default_desk_handler(alert: dict[str, Any]) -> dict[str, Any]:
    """Safe fallback for a standalone installation: notify System Managers."""
    users = _system_manager_users()
    for user in users:
        frappe.get_doc(
            {
                "doctype": "Notification Log",
                "for_user": user,
                "type": "Alert",
                "document_type": alert.get("reference_doctype"),
                "document_name": alert.get("reference_name"),
                "subject": alert.get("title") or _("Electronic invoicing alert"),
                "email_content": alert.get("message") or "",
            }
        ).insert(ignore_permissions=True)
    return {
        "delivered": bool(users),
        "handler": "dgii_ecf.default_desk_handler",
        "recipient_count": len(users),
    }


def _was_delivered(result: Any) -> bool:
    if result is False or result is None:
        return False
    if isinstance(result, dict) and "delivered" in result:
        return bool(result["delivered"])
    return True


def dispatch_operational_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Dispatch through installed consumer hooks, with a generic Desk fallback.

    Alert delivery must never roll back a fiscal state transition. Handler errors
    are logged, and the generic System Manager notification is attempted when no
    consumer reports a successful delivery.
    """
    handlers = list(frappe.get_hooks(ALERT_HANDLERS_HOOK) or [])
    results: list[dict[str, Any]] = []
    delivered = False

    for path in handlers:
        try:
            result = frappe.get_attr(path)(dict(alert))
            handler_delivered = _was_delivered(result)
            delivered = delivered or handler_delivered
            results.append({"handler": path, "delivered": handler_delivered})
        except Exception:
            frappe.log_error(
                sanitize_provider_text(frappe.get_traceback())[:10000],
                f"e-CF operational alert handler failed: {path}",
            )
            results.append({"handler": path, "delivered": False})

    used_fallback = False
    if not delivered:
        used_fallback = True
        try:
            fallback = _default_desk_handler(alert)
            delivered = _was_delivered(fallback)
            results.append(fallback)
        except Exception:
            frappe.log_error(
                sanitize_provider_text(frappe.get_traceback())[:10000],
                "e-CF fallback operational alert delivery failed",
            )

    return {
        "delivered": delivered,
        "used_fallback": used_fallback,
        "handlers": results,
    }
