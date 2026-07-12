"""ECF Document Log — one row per submitted e-CF. System of record for the fiscal
reference (eNCF, security code, QR, status) and the poll target for the scheduler.
The Sales Invoice reads from here; no custom fields on the invoice."""

from frappe.model.document import Document


class ECFDocumentLog(Document):
    pass
