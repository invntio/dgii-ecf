"""Fiscal audit record linked to any native Frappe/ERPNext document."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document


class ECFDocumentLog(Document):
    def validate(self):
        self._normalize_tax_ids()
        self._populate_tax_ids_from_request()
        self._validate_reference()
        self._validate_modified_document()
        self._validate_received_document()
        self._validate_duplicate()

    def _normalize_tax_ids(self):
        self.issuer_tax_id = _digits(self.issuer_tax_id)
        self.receiver_tax_id = _digits(self.receiver_tax_id)

    def _populate_tax_ids_from_request(self):
        if self.issuer_tax_id and self.receiver_tax_id:
            return
        try:
            request = json.loads(self.request_json or "{}")
        except (TypeError, ValueError):
            return
        header = request.get("ECF", {}).get("Encabezado", {})
        if not self.issuer_tax_id:
            self.issuer_tax_id = _digits(
                header.get("Emisor", {}).get("RNCEmisor")
            )
        if not self.receiver_tax_id:
            self.receiver_tax_id = _digits(
                header.get("Comprador", {}).get("RNCComprador")
            )

    def _validate_reference(self):
        if not frappe.db.exists(self.reference_doctype, self.reference_name):
            frappe.throw(
                _("Referenced document {0} {1} does not exist.").format(
                    self.reference_doctype, self.reference_name
                )
            )
        meta = frappe.get_meta(self.reference_doctype)
        if meta.has_field("company"):
            reference_company = frappe.db.get_value(
                self.reference_doctype, self.reference_name, "company"
            )
            if reference_company and reference_company != self.company:
                frappe.throw(
                    _("The referenced document belongs to company {0}, not {1}.").format(
                        reference_company, self.company
                    )
                )

    def _validate_modified_document(self):
        if not self.modified_ecf_document:
            return
        if self.modified_ecf_document == self.name:
            frappe.throw(_("An e-CF document cannot modify itself."))
        modified_company = frappe.db.get_value(
            "ECF Document Log", self.modified_ecf_document, "company"
        )
        if modified_company and modified_company != self.company:
            frappe.throw(
                _("The modified e-CF document must belong to the same company.")
            )

    def _validate_received_document(self):
        if self.direction == "Received" and not self.issuer_tax_id:
            frappe.throw(_("Issuer Tax ID is required for a received e-CF."))

    def _validate_duplicate(self):
        filters = {
            "encf": self.encf,
            "issuer_tax_id": self.issuer_tax_id or "",
            "name": ["!=", self.name or ""],
        }
        if frappe.db.exists("ECF Document Log", filters):
            frappe.throw(
                _("e-NCF {0} is already registered for issuer {1}.").format(
                    self.encf, self.issuer_tax_id or _("Unknown")
                )
            )


def _digits(value: str | None) -> str:
    return "".join(character for character in (value or "") if character.isdigit())
