"""ECF Sequence Range — DGII-authorized eNCF ranges with concurrency-safe handout.

A duplicated eNCF is rejected by DGII and permanently burns an authorized number,
so `get_next_encf` hands out sequences under a pessimistic row lock (FOR UPDATE),
serializing concurrent submissions on the same range. Allocate inside the same
transaction that records the send, so a rollback doesn't leak a consumed number.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class ECFSequenceRange(Document):
    def validate(self):
        if self.sequence_from > self.sequence_to:
            frappe.throw(_("Sequence From must be <= Sequence To."))
        if not self.current or self.current < self.sequence_from - 1:
            # `current` = last handed out; starts just below the range.
            self.current = self.sequence_from - 1
        self._check_overlap()

    def _check_overlap(self):
        # Ranges may legitimately repeat numbers across environments (TesteCF and
        # eCF are separate DGII universes), so overlap is only checked within one.
        overlap = frappe.db.sql(
            """
            SELECT name FROM `tabECF Sequence Range`
            WHERE company=%s AND environment=%s AND ecf_type=%s AND name!=%s
              AND status='Active'
              AND sequence_from <= %s AND sequence_to >= %s
            LIMIT 1
            """,
            (self.company, self.environment, self.ecf_type, self.name or "new",
             self.sequence_to, self.sequence_from),
        )
        if overlap:
            frappe.throw(
                _("Range overlaps active range {0} for company/environment/type.").format(
                    overlap[0][0]
                )
            )


def get_next_encf(company: str, ecf_type: str, environment: str) -> tuple[str, str]:
    """Return (eNCF, range_name) for the next authorized number, row-locked.

    `environment` must match the company's ECF Provider Settings: TesteCF/CerteCF/eCF
    sequences are distinct DGII universes, so a range is only valid in the
    environment it was authorized for.
    """
    rows = frappe.db.sql(
        """
        SELECT name, `current`, sequence_to
        FROM `tabECF Sequence Range`
        WHERE company=%s AND environment=%s AND ecf_type=%s AND status='Active'
          AND `current` < sequence_to AND expiry_date >= CURDATE()
        ORDER BY sequence_from ASC
        LIMIT 1
        FOR UPDATE
        """,
        (company, environment, ecf_type),
        as_dict=True,
    )
    if not rows:
        frappe.throw(
            _(
                "No active eNCF sequence range for company {0}, environment {1}, "
                "e-CF type {2}."
            ).format(company, environment, ecf_type)
        )
    r = rows[0]
    nxt = r.current + 1
    frappe.db.set_value("ECF Sequence Range", r.name, "current", nxt,
                        update_modified=False)
    if nxt >= r.sequence_to:
        frappe.db.set_value("ECF Sequence Range", r.name, "status", "Exhausted",
                            update_modified=False)
    return f"E{ecf_type}{nxt:010d}", r.name
