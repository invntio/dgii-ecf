"""ECF Provider Settings — per-Company e-CF gateway config (multi-tenant).

One row per Dominican-Republic company. Non-DR companies have none.

Credential model: the gateway LOGIN (email/password → bearer token) is the SaaS
operator's provider account and lives in the global `ECF Gateway Account` Single
(System Manager only). What identifies THIS company at the gateway is its own
per-environment API Key, pasted here by its manager like any third-party
integration. `use_custom_credentials` (permlevel 1) covers standalone setups where
the company logs in with its own gateway account instead.
"""

import frappe
from frappe import _
from frappe.model.document import Document

# environment Select value -> API-key fieldname
API_KEY_FIELD = {
    "TesteCF": "api_key_testecf",
    "CerteCF": "api_key_certecf",
    "eCF": "api_key_ecf",
}


class ECFProviderSettings(Document):
    def validate(self):
        country = frappe.db.get_value("Company", self.company, "country")
        if country != "Dominican Republic":
            frappe.throw(
                _("e-CF applies only to Dominican Republic companies (got {0}).").format(
                    country or "unset"
                )
            )
        if not self.enabled:
            return

        key_field = API_KEY_FIELD[self.environment]
        if not self.get(key_field):
            frappe.throw(
                _("The API Key for environment {0} is required before enabling.").format(
                    self.environment
                )
            )

        if self.use_custom_credentials:
            if not (self.email and self.get("password")):
                frappe.throw(
                    _("Custom credentials require both gateway email and password.")
                )
        else:
            gateway = frappe.get_single("ECF Gateway Account")
            if not (gateway.email and gateway.get_password("password", raise_exception=False)):
                frappe.throw(
                    _(
                        "No gateway login available: configure ECF Gateway Account "
                        "(platform) or enable custom credentials for this company."
                    )
                )

    def active_api_key(self) -> str:
        """The API Key matching this company's current environment (decrypted)."""
        key_field = API_KEY_FIELD[self.environment]
        key = self.get_password(key_field, raise_exception=False)
        if not key:
            frappe.throw(
                _("No API Key configured for company {0} in environment {1}.").format(
                    self.company, self.environment
                )
            )
        return key

    def login_credentials(self) -> tuple[str, str, str]:
        """(email, password, base_url) — company override or the platform account."""
        if self.use_custom_credentials:
            return (
                self.email,
                self.get_password("password"),
                self.base_url or "https://ecf.api.mseller.app",
            )
        gateway = frappe.get_single("ECF Gateway Account")
        if not gateway.email:
            frappe.throw(
                _(
                    "No gateway login available: configure ECF Gateway Account "
                    "(platform) or enable custom credentials for this company."
                )
            )
        return (
            gateway.email,
            gateway.get_password("password"),
            self.base_url or gateway.base_url or "https://ecf.api.mseller.app",
        )
