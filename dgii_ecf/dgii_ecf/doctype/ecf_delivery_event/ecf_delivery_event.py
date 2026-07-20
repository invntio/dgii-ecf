"""Append-only audit events for provider delivery and reconciliation."""

import frappe
from frappe import _
from frappe.model.document import Document

from dgii_ecf.delivery import calculate_event_hash, event_hash_values
from dgii_ecf.event_types import validate_event_type


class ECFDeliveryEvent(Document):
    def before_insert(self):
        if not self.flags.from_delivery_service:
            frappe.throw(_("ECF delivery events can only be created by the delivery service."))
        validate_event_type(self.event_type)
        expected = calculate_event_hash(event_hash_values(self))
        if self.event_hash != expected:
            frappe.throw(_("The ECF delivery event integrity hash is invalid."))

    def validate(self):
        if not self.is_new():
            frappe.throw(_("ECF delivery events are append-only and cannot be changed."))

    def on_trash(self):
        frappe.throw(_("ECF delivery events are append-only and cannot be deleted."))
