"""Build the MSeller `ECF` JSON from a Sales Invoice.

Reference: .claude/skills/dgii-ecf-mseller/references/ecf-document-format.md and the
DGII PDF "Formato e-CF v1.0". Issuer identity comes from NATIVE Company fields
(tax_id / company_name / primary Address) — no custom fields on Company.

Tax model: ITBIS 18% is applied to taxable items via an "ITBIS 18%" Item Tax
Template set up by the host app. Items without it are exempt. So an invoice
line is taxable iff its computed `item_tax_rate` carries a rate > 0.

Implemented: types 31 (crédito fiscal), 32 (consumo), 33 (nota de débito / mora),
34 (nota de crédito). Other types raise NotImplementedError with a clear message.
Always dry-run new documents with the client's `validate=True` before a real submit.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import flt

ITBIS_RATE = 18.0
CONSUMER_RNC = "00000000000"


def _money(value) -> float:
    """DGII amounts: explicit half-up to 2dp. `flt()` obeys the site's Rounding
    Method (possibly banker's) — a one-cent drift in Totales gets the document
    rejected, so fiscal rounding must not depend on site config."""
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _digits(value) -> str:
    """RNC/cédula as bare digits — '1-02-32070-5' -> '102320705'. DGII fields
    take the unformatted number; also turns junk like 'N/A' into empty."""
    return "".join(ch for ch in (value or "") if ch.isdigit())

# e-CF type codes
TYPE_CREDITO_FISCAL = "31"
TYPE_CONSUMO = "32"
TYPE_NOTA_DEBITO = "33"
TYPE_NOTA_CREDITO = "34"

_DATE_FMT = "%d-%m-%Y"


def _ddmmyyyy(value) -> str | None:
    if not value:
        return None
    return frappe.utils.getdate(value).strftime(_DATE_FMT)


def pick_ecf_type(si) -> str:
    """Use the invoice's explicit fiscal intent; identification alone is not intent."""
    if si.get("is_debit_note"):
        return TYPE_NOTA_DEBITO
    if si.get("is_return"):
        return TYPE_NOTA_CREDITO
    return (
        TYPE_CREDITO_FISCAL
        if si.get("dgii_ecf_requires_fiscal_credit")
        else TYPE_CONSUMO
    )


def _item_is_taxable(item) -> bool:
    """True if the line carries ITBIS. ERPNext populates `item_tax_rate` (a JSON map
    of account -> rate) once taxes are computed; any positive rate = taxable."""
    raw = item.get("item_tax_rate")
    if raw:
        try:
            rates = json.loads(raw) if isinstance(raw, str) else raw
            if any(flt(r) > 0 for r in rates.values()):
                return True
            if rates:  # explicit empty-rate map => exempt
                return False
        except (ValueError, AttributeError):
            pass
    # Fallback: look at the Item's tax template membership.
    if item.get("item_tax_template"):
        return True
    return bool(
        frappe.db.exists(
            "Item Tax", {"parent": item.item_code, "item_tax_template": ["like", "%ITBIS%"]}
        )
    )


def _emisor(company: str, posting_date) -> dict:
    c = frappe.get_doc("Company", company)
    emisor = {
        "RNCEmisor": _digits(c.tax_id),
        "RazonSocialEmisor": c.company_name,
    }
    address = _primary_address(company)
    if address:
        emisor["DireccionEmisor"] = address
    # DGII's XSD is sequence-sensitive: FechaEmision comes after all optional
    # issuer identity/address fields, including DireccionEmisor.
    # FechaEmision belongs to the invoice, not to the delivery attempt.  A retry
    # tomorrow must not silently turn yesterday's invoice into today's invoice.
    emisor["FechaEmision"] = _ddmmyyyy(posting_date)
    return emisor


def _primary_address(company: str) -> str | None:
    name = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Company", "link_name": company, "parenttype": "Address"},
        "parent",
    )
    if not name:
        return None
    return frappe.db.get_value("Address", name, "address_line1")


def _comprador(si) -> dict:
    rnc = _digits(frappe.db.get_value("Customer", si.customer, "tax_id"))
    return {
        "RNCComprador": rnc or CONSUMER_RNC,
        "RazonSocialComprador": si.customer_name or si.customer,
    }


def _items_and_totals(si) -> tuple[list, dict]:
    items = []
    gravado = 0.0
    exento = 0.0
    for idx, it in enumerate(si.items, start=1):
        amount = _money(it.amount)
        taxable = _item_is_taxable(it)
        if taxable:
            gravado += amount
            indicador = 1  # ITBIS 18%
        else:
            exento += amount
            indicador = 4  # exempt
        items.append(
            {
                "NumeroLinea": idx,
                "IndicadorFacturacion": indicador,
                "NombreItem": (it.item_name or it.item_code)[:80],
                "IndicadorBienoServicio": 2 if _is_service(it.item_code) else 1,
                "CantidadItem": flt(it.qty),
                "UnidadMedida": 43,  # DGII catalog: generic unit/service.
                "PrecioUnitarioItem": _money(it.rate),
                "MontoItem": amount,
            }
        )
    itbis = _money(gravado * ITBIS_RATE / 100.0)
    totales = {
        "MontoGravadoTotal": _money(gravado),
        "MontoGravadoI1": _money(gravado),
        "MontoExento": _money(exento),
        "ITBIS1": int(ITBIS_RATE),
        "TotalITBIS": itbis,
        "TotalITBIS1": itbis,
        "MontoTotal": _money(gravado + exento + itbis),
    }
    return items, totales


def _is_service(item_code: str) -> bool:
    # Non-stock items are treated as services for DGII (IndicadorBienoServicio=2).
    return not frappe.db.get_value("Item", item_code, "is_stock_item")


def _reference_section(si) -> dict:
    """InformacionReferencia for notas (33/34): point at the modified e-CF."""
    source = si.get("return_against")
    if not source:
        frappe.throw(
            _(
                "A debit/credit note e-CF must reference the original invoice "
                "(Return Against is not set)."
            )
        )
    rows = frappe.get_all(
        "ECF Document Log",
        filters={
            "direction": "Issued",
            "reference_doctype": "Sales Invoice",
            "reference_name": source,
            "encf": ["is", "set"],
        },
        fields=["name", "encf"],
        order_by="creation desc",
        limit=1,
    )
    if not rows:
        frappe.throw(_("No e-CF found for the referenced invoice {0}.").format(source))
    ref = {
        "NCFModificado": rows[0].encf,
        "FechaNCFModificado": _ddmmyyyy(si.posting_date),
    }
    return ref


def build_ecf_json(si, encf: str, ecf_type: str, sequence_expiry=None) -> dict:
    """Assemble the full `{"ECF": {...}}` payload for the given Sales Invoice.

    `sequence_expiry` is the authorized range's expiry date — required by DGII on
    type 31 as FechaVencimientoSecuencia (the caller gets it from the range the
    eNCF was allocated from).
    """
    items, totales = _items_and_totals(si)

    # 1=contado, 2=crédito. A due date after posting means the buyer pays later.
    is_credit = bool(
        si.get("due_date")
        and frappe.utils.getdate(si.due_date) > frappe.utils.getdate(si.posting_date)
    )
    id_doc = {
        "TipoeCF": ecf_type,
        "eNCF": encf,
    }
    # IdDoc is an XSD sequence, not an unordered bag of fields. Insert each
    # conditional value at its schema position instead of appending it later.
    if ecf_type == TYPE_CREDITO_FISCAL and sequence_expiry:
        id_doc["FechaVencimientoSecuencia"] = _ddmmyyyy(sequence_expiry)
    id_doc["IndicadorMontoGravado"] = "1" if totales["MontoGravadoTotal"] else "0"
    id_doc["TipoIngresos"] = "01"
    id_doc["TipoPago"] = "2" if is_credit else "1"
    if ecf_type == TYPE_CREDITO_FISCAL and is_credit:
        id_doc["FechaLimitePago"] = _ddmmyyyy(si.due_date)
    id_doc["TotalPaginas"] = "1"

    encabezado = {
        "Version": "1.0",
        "IdDoc": id_doc,
        "Emisor": _emisor(si.company, si.posting_date),
        "Comprador": _comprador(si),
        "Totales": totales,
    }

    ecf: dict = {
        "Encabezado": encabezado,
        "DetallesItems": {"Item": items},
    }

    if ecf_type == TYPE_CREDITO_FISCAL:
        ecf["Paginacion"] = {"Pagina": [_single_page(items, totales)]}
    elif ecf_type in (TYPE_NOTA_DEBITO, TYPE_NOTA_CREDITO):
        ecf["InformacionReferencia"] = _reference_section(si)
    elif ecf_type == TYPE_CONSUMO:
        pass  # RFCE for < RD$250K is handled by MSeller; send the full ECF as-is
    else:
        raise NotImplementedError(
            f"e-CF type {ecf_type} is not implemented in the builder yet "
            "(only 31, 32, 33, 34). See ecf-document-format.md for its sections."
        )

    # MSeller applies the real signing timestamp.  Keep the schema element but do
    # not pretend the client-side build time is the cryptographic signing time.
    ecf["FechaHoraFirma"] = ""

    return {"ECF": ecf}


def _single_page(items: list, totales: dict) -> dict:
    return {
        "PaginaNo": 1,
        "NoLineaDesde": 1,
        "NoLineaHasta": len(items),
        "SubtotalMontoGravadoPagina": totales["MontoGravadoTotal"],
        "SubtotalMontoGravado1Pagina": totales["MontoGravadoI1"],
        "SubtotalExentoPagina": totales["MontoExento"],
        "SubtotalItbisPagina": totales["TotalITBIS"],
        "SubtotalItbis1Pagina": totales["TotalITBIS1"],
        "MontoSubtotalPagina": totales["MontoTotal"],
        "SubtotalMontoNoFacturablePagina": 0.0,
    }
