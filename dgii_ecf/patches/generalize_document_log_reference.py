"""Replace the Sales Invoice-specific log link with a Dynamic Link."""

from __future__ import annotations

import json

import frappe


def execute():
    if not frappe.db.table_exists("ECF Document Log"):
        return

    if frappe.db.has_column("ECF Document Log", "sales_invoice"):
        rows = frappe.db.sql(
            """
            select name, sales_invoice, request_json
            from `tabECF Document Log`
            where ifnull(sales_invoice, '') != ''
            """,
            as_dict=True,
        )
        for row in rows:
            issuer_tax_id, receiver_tax_id = _tax_ids(row.request_json)
            frappe.db.set_value(
                "ECF Document Log",
                row.name,
                {
                    "direction": "Issued",
                    "reference_doctype": "Sales Invoice",
                    "reference_name": row.sales_invoice,
                    "issuer_tax_id": issuer_tax_id,
                    "receiver_tax_id": receiver_tax_id,
                },
                update_modified=False,
            )

        frappe.db.sql_ddl(
            "alter table `tabECF Document Log` drop column `sales_invoice`"
        )


def _tax_ids(request_json: str | None) -> tuple[str, str]:
    try:
        header = json.loads(request_json or "{}").get("ECF", {}).get("Encabezado", {})
    except (TypeError, ValueError):
        return "", ""
    return (
        _digits(header.get("Emisor", {}).get("RNCEmisor")),
        _digits(header.get("Comprador", {}).get("RNCComprador")),
    )


def _digits(value: str | None) -> str:
    return "".join(character for character in (value or "") if character.isdigit())
