"""dgii_ecf module unit tests — provider mapping and eNCF sequence handout.
Collected by `bench run-tests`. The MSeller transport is mocked; no network.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dgii_ecf.events.sales_invoice import set_print_language
from dgii_ecf.printing import get_ecf_print_data, qr_svg_data_uri
from dgii_ecf.providers.base import EcfResult
from dgii_ecf.providers.mseller import MSellerProvider
from dgii_ecf.mseller.client import MSellerError
from dgii_ecf.dgii_ecf.doctype.ecf_sequence_range.ecf_sequence_range import (
    get_next_encf,
)

COMPANY = "ECF Test Co"


def _ensure_test_company():
    if frappe.db.exists("Company", COMPANY):
        return
    frappe.get_doc({
        "doctype": "Company", "company_name": COMPANY, "abbr": "ETC",
        "default_currency": "DOP", "country": "Dominican Republic",
        "tax_id": "102320705",
    }).insert(ignore_permissions=True)


def _ensure_test_invoice():
    """A submitted Sales Invoice for COMPANY (created once, reused)."""
    _ensure_test_company()
    name = frappe.db.get_value("Sales Invoice",
        {"company": COMPANY, "docstatus": 1, "is_debit_note": 0}, "name")
    if name:
        return frappe.get_doc("Sales Invoice", name)
    if not frappe.db.exists("Customer", "ECF Test Customer"):
        frappe.get_doc({"doctype": "Customer",
            "customer_name": "ECF Test Customer",
            "tax_id": "130123456"}).insert(ignore_permissions=True)
    if not frappe.db.exists("Item", "ECF-TEST-SERVICE"):
        frappe.get_doc({"doctype": "Item", "item_code": "ECF-TEST-SERVICE",
            "item_name": "ECF Test Service", "item_group": "All Item Groups",
            "is_stock_item": 0}).insert(ignore_permissions=True)
    si = frappe.get_doc({
        "doctype": "Sales Invoice", "company": COMPANY,
        "customer": "ECF Test Customer",
        "currency": "DOP", "conversion_rate": 1,
        "ignore_pricing_rule": 1,
        "due_date": frappe.utils.add_days(frappe.utils.today(), 30),
        "items": [{"item_code": "ECF-TEST-SERVICE", "qty": 1, "rate": 1000}],
    })
    si.insert(ignore_permissions=True)
    si.submit()
    return si


def _fake_settings():
    s = MagicMock()
    s.environment = "TesteCF"
    s.email = "x@y.do"
    s.base_url = "https://ecf.api.mseller.app"
    s.get_password = MagicMock(return_value="secret")
    return s


class TestPrinting(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.si = _ensure_test_invoice()
        frappe.db.delete("ECF Document Log", {"sales_invoice": self.si.name})

    def tearDown(self):
        frappe.db.delete("ECF Document Log", {"sales_invoice": self.si.name})

    def test_print_data_comes_from_document_log(self):
        log = frappe.get_doc({
            "doctype": "ECF Document Log",
            "company": COMPANY,
            "sales_invoice": self.si.name,
            "ecf_type": "32",
            "encf": "E320000088888",
            "status": "Aceptado",
            "security_code": "ABC123",
            "qr_url": "https://example.invalid/verify",
            "request_json": frappe.as_json({
                "ECF": {
                    "Encabezado": {
                        "IdDoc": {"TipoeCF": "32", "eNCF": "E320000088888"},
                        "Emisor": {
                            "RNCEmisor": "102320705",
                            "RazonSocialEmisor": COMPANY,
                            "FechaEmision": "13-07-2026",
                        },
                        "Comprador": {
                            "RNCComprador": "130123456",
                            "RazonSocialComprador": "ECF Test Customer",
                        },
                        "Totales": {
                            "MontoGravadoTotal": 1000,
                            "TotalITBIS": 180,
                            "MontoTotal": 1180,
                        },
                    },
                    "DetallesItems": {"Item": [{
                        "CantidadItem": 1,
                        "NombreItem": "ECF Test Service",
                        "PrecioUnitarioItem": 1000,
                        "MontoItem": 1000,
                        "IndicadorFacturacion": 1,
                    }]},
                }
            }),
        }).insert(ignore_permissions=True)

        data = get_ecf_print_data(self.si.name)

        self.assertEqual(data.log_name, log.name)
        self.assertEqual(data.encf, "E320000088888")
        self.assertEqual(data.qr_url, "https://example.invalid/verify")
        self.assertEqual(data.title, "Electronic Consumer Invoice")
        self.assertEqual(data.payment_due, frappe.utils.formatdate(self.si.due_date, "dd-mm-yyyy"))
        self.assertEqual(data.lines[0].tax, 180)
        self.assertEqual(data.grand_total, 1180)

    def test_qr_is_an_inline_svg_data_uri(self):
        data_uri = qr_svg_data_uri("https://example.invalid/verify")
        self.assertTrue(data_uri.startswith("data:image/svg+xml;base64,"))
        self.assertEqual(qr_svg_data_uri(None), "")

    def test_dominican_company_forces_spanish_print_language(self):
        doc = frappe._dict(company=COMPANY, language="en")
        set_print_language(doc)
        self.assertEqual(doc.language, "es")

    def test_other_countries_keep_invoice_language(self):
        doc = frappe._dict(company="Foreign Company", language="fr")
        with patch.object(frappe.db, "get_value", return_value="France"):
            set_print_language(doc)
        self.assertEqual(doc.language, "fr")


class TestMSellerProviderMapping(FrappeTestCase):
    """MSeller JSON -> normalized EcfResult (the swap-layer contract)."""

    def test_send_maps_submission_response(self):
        provider = MSellerProvider(_fake_settings())
        with patch.object(MSellerProvider, "_client") as client:
            client.return_value.send_document.return_value = {
                "rnc": "102320705",
                "ecf": "E310000009175",
                "internalTrackId": "uuid-1",
                "securityCode": "fWCZCV",
                "qr_url": "https://ecf.dgii.gov.do/qr",
                "signedDate": "14-05-2025 02:57:33",
            }
            res = provider.send({"ECF": {}})
        self.assertIsInstance(res, EcfResult)
        self.assertTrue(res.success)
        self.assertEqual(res.status, "RECIBIDO")  # 2xx = received, NOT accepted
        self.assertEqual(res.encf, "E310000009175")
        self.assertEqual(res.track_id, "uuid-1")
        self.assertEqual(res.security_code, "fWCZCV")

    def test_send_validate_maps_validation_failure(self):
        provider = MSellerProvider(_fake_settings())
        with patch.object(MSellerProvider, "_client") as client:
            client.return_value.send_document.return_value = {
                "message": "1 error(s): ECF.Encabezado.Totales.MontoTotal",
                "code": "ECF_VALIDATION_FAILED",
            }
            res = provider.send({"ECF": {}}, validate=True)
        self.assertFalse(res.success)
        self.assertIn("MontoTotal", res.error)

    def test_send_validate_rejects_submission_receipt(self):
        provider = MSellerProvider(_fake_settings())
        with patch.object(MSellerProvider, "_client") as client:
            client.return_value.send_document.return_value = {
                "ecf": "E320000000000",
                "internalTrackId": "uuid-unexpected",
                "securityCode": "ABC123",
                "qr_url": "https://example.invalid/qr",
            }
            with self.assertRaisesRegex(MSellerError, "ignored validate=true"):
                provider.send({"ECF": {}}, validate=True)

    def test_batch_maps_found_and_missing(self):
        provider = MSellerProvider(_fake_settings())
        with patch.object(MSellerProvider, "_client") as client:
            client.return_value.get_status_batch.return_value = {
                "total": 2,
                "results": [
                    {"ecf": "E310000000001", "status": "Aceptado", "found": True,
                     "data": {"securityCode": "A7HXQC", "qr_url": "u"}},
                    {"ecf": "E310000000002", "found": False},
                ],
            }
            out = provider.get_status_batch(["E310000000001", "E310000000002"])
        self.assertEqual(out[0].status, "Aceptado")
        self.assertTrue(out[0].success)
        self.assertFalse(out[1].success)


class TestSequenceHandout(FrappeTestCase):
    """get_next_encf: format, monotonicity, exhaustion, no duplicates."""

    def setUp(self):
        frappe.set_user("Administrator")
        _ensure_test_company()
        frappe.db.delete("ECF Sequence Range", {"company": COMPANY})
        self.range = frappe.get_doc({
            "doctype": "ECF Sequence Range",
            "company": COMPANY,
            "environment": "TesteCF",
            "ecf_type": "31",
            "sequence_from": 100,
            "sequence_to": 102,
            "expiry_date": frappe.utils.add_days(frappe.utils.today(), 30),
            "status": "Active",
        }).insert(ignore_permissions=True)

    def test_handout_is_sequential_unique_and_exhausts(self):
        seen = set()
        for expected in (100, 101, 102):
            encf, _ = get_next_encf(COMPANY, "31", "TesteCF")
            self.assertEqual(encf, f"E31{expected:010d}")
            self.assertNotIn(encf, seen)
            seen.add(encf)
        # Range exhausted -> loud failure, never a duplicate.
        self.assertEqual(
            frappe.db.get_value("ECF Sequence Range", self.range.name, "status"),
            "Exhausted",
        )
        with self.assertRaises(frappe.ValidationError):
            get_next_encf(COMPANY, "31", "TesteCF")

    def test_expired_range_not_used(self):
        frappe.db.set_value("ECF Sequence Range", self.range.name, "expiry_date",
                            frappe.utils.add_days(frappe.utils.today(), -1))
        with self.assertRaises(frappe.ValidationError):
            get_next_encf(COMPANY, "31", "TesteCF")

    def test_range_is_environment_scoped(self):
        # A TesteCF range must never serve production (eCF) numbers.
        with self.assertRaises(frappe.ValidationError):
            get_next_encf(COMPANY, "31", "eCF")


def _a_submitted_invoice():
    return _ensure_test_invoice()


class TestBuilder(FrappeTestCase):
    """Sales Invoice -> ECF JSON: per-type sections and header correctness."""

    def setUp(self):
        frappe.set_user("Administrator")
        self.si = _a_submitted_invoice()
        frappe.db.set_value("Company", COMPANY, "tax_id", "102320705")

    def test_type31_has_sequence_expiry_payment_terms_and_pagination(self):
        from dgii_ecf.ecf.builder import build_ecf_json

        expiry = frappe.utils.add_days(frappe.utils.today(), 200)
        doc = build_ecf_json(self.si, "E310000000001", "31", sequence_expiry=expiry)
        id_doc = doc["ECF"]["Encabezado"]["IdDoc"]
        self.assertIn("FechaVencimientoSecuencia", id_doc)
        self.assertIn("Paginacion", doc["ECF"])
        # Credit sale (due_date > posting_date) must carry TipoPago=2 + deadline.
        if frappe.utils.getdate(self.si.due_date) > frappe.utils.getdate(self.si.posting_date):
            self.assertEqual(id_doc["TipoPago"], "2")
            self.assertIn("FechaLimitePago", id_doc)
        else:
            self.assertEqual(id_doc["TipoPago"], "1")
        page = doc["ECF"]["Paginacion"]["Pagina"][0]
        self.assertEqual(page["MontoSubtotalPagina"],
                         doc["ECF"]["Encabezado"]["Totales"]["MontoTotal"])

    def test_type33_references_original_ecf_from_log(self):
        from dgii_ecf.ecf.builder import build_ecf_json

        frappe.db.delete("ECF Document Log", {"sales_invoice": self.si.name})
        frappe.get_doc({
            "doctype": "ECF Document Log", "company": COMPANY,
            "sales_invoice": self.si.name, "ecf_type": "31",
            "encf": "E319999999901", "status": "Aceptado",
        }).insert(ignore_permissions=True)

        nota = frappe.get_doc("Sales Invoice", self.si.name)  # same shape, in memory
        nota.is_debit_note = 1
        nota.return_against = self.si.name
        doc = build_ecf_json(nota, "E330000000001", "33")
        ref = doc["ECF"]["InformacionReferencia"]
        self.assertEqual(ref["NCFModificado"], "E319999999901")
        self.assertNotIn("Paginacion", doc["ECF"])

    def test_type33_without_source_ecf_fails_loud(self):
        from dgii_ecf.ecf.builder import build_ecf_json

        nota = frappe.get_doc("Sales Invoice", self.si.name)
        nota.is_debit_note = 1
        nota.return_against = None
        with self.assertRaises(frappe.ValidationError):
            build_ecf_json(nota, "E330000000002", "33")


class TestPolling(FrappeTestCase):
    """Scheduler poll: batch status updates the log and stops at terminal states."""

    def setUp(self):
        frappe.set_user("Administrator")
        self._ecf_enabled = frappe.conf.get("dgii_ecf_enabled")
        frappe.conf.dgii_ecf_enabled = 1
        _ensure_test_company()
        frappe.db.delete("ECF Document Log", {"encf": "E320000099999"})
        self.log = frappe.get_doc({
            "doctype": "ECF Document Log", "company": COMPANY,
            "ecf_type": "32", "encf": "E320000099999", "status": "RECIBIDO",
        }).insert(ignore_permissions=True)

    def tearDown(self):
        frappe.conf.dgii_ecf_enabled = self._ecf_enabled
        frappe.db.delete("ECF Document Log", {"encf": "E320000099999"})

    def test_poll_flips_status_to_aceptado(self):
        from dgii_ecf import tasks

        fake = MagicMock()
        fake.get_status_batch.return_value = [
            EcfResult(success=True, status="Aceptado", encf="E320000099999",
                      security_code="ABC123", qr_url="u", raw={}),
        ]
        with patch.object(tasks, "get_provider", return_value=fake):
            tasks.poll_pending_documents()
        self.assertEqual(
            frappe.db.get_value("ECF Document Log", self.log.name, "status"),
            "Aceptado",
        )


class TestFiscalHardening(FrappeTestCase):
    """Half-up money rounding, RNC cleaning, and the cancel guard."""

    def setUp(self):
        frappe.set_user("Administrator")
        self.si = _a_submitted_invoice()

    def test_money_rounds_half_up_regardless_of_site_rounding(self):
        from dgii_ecf.ecf.builder import _money

        self.assertEqual(_money(0.125), 0.13)   # banker's would give 0.12
        self.assertEqual(_money(2.675), 2.68)   # float repr trap: round() gives 2.67
        self.assertEqual(_money(None), 0.0)

    def test_pick_type_cleans_buyer_rnc(self):
        from dgii_ecf.ecf.builder import pick_ecf_type

        original = frappe.db.get_value("Customer", self.si.customer, "tax_id")
        try:
            frappe.db.set_value("Customer", self.si.customer, "tax_id", "1-02-32070-5")
            self.assertEqual(pick_ecf_type(self.si), "31")
            # Junk like N/A carries no digits -> consumer invoice, not a bogus 31.
            frappe.db.set_value("Customer", self.si.customer, "tax_id", "N/A")
            self.assertEqual(pick_ecf_type(self.si), "32")
        finally:
            frappe.db.set_value("Customer", self.si.customer, "tax_id", original)

    def test_comprador_rnc_is_bare_digits(self):
        from dgii_ecf.ecf.builder import _comprador

        original = frappe.db.get_value("Customer", self.si.customer, "tax_id")
        try:
            frappe.db.set_value("Customer", self.si.customer, "tax_id", "1-02-32070-5")
            self.assertEqual(_comprador(self.si)["RNCComprador"], "102320705")
        finally:
            frappe.db.set_value("Customer", self.si.customer, "tax_id", original)

    def test_cancel_blocked_while_ecf_accepted_or_in_flight(self):
        from dgii_ecf.events.sales_invoice import on_cancel

        frappe.db.delete("ECF Document Log", {"sales_invoice": self.si.name})
        frappe.get_doc({
            "doctype": "ECF Document Log", "company": COMPANY,
            "sales_invoice": self.si.name, "ecf_type": "31",
            "encf": "E319999999902", "status": "Aceptado",
        }).insert(ignore_permissions=True)
        try:
            with self.assertRaises(frappe.ValidationError):
                on_cancel(self.si)
            # Rejected e-CF does not block cancellation.
            frappe.db.set_value(
                "ECF Document Log", {"sales_invoice": self.si.name},
                "status", "Rechazado",
            )
            on_cancel(self.si)  # must not raise
        finally:
            frappe.db.delete("ECF Document Log", {"sales_invoice": self.si.name})


class TestCredentialResolution(FrappeTestCase):
    """Platform gateway login + per-company/per-environment API keys."""

    def setUp(self):
        frappe.set_user("Administrator")
        _ensure_test_company()
        gw = frappe.get_single("ECF Gateway Account")
        gw.email = "platform@invnt.io"
        gw.password = "platform-secret"
        gw.base_url = "https://ecf.api.mseller.app"
        gw.save(ignore_permissions=True)

        name = frappe.db.exists("ECF Provider Settings", {"company": COMPANY})
        self.settings = (
            frappe.get_doc("ECF Provider Settings", name)
            if name
            else frappe.new_doc("ECF Provider Settings", company=COMPANY)
        )
        self.settings.update({
            "enabled": 1, "provider_type": "MSeller", "environment": "TesteCF",
            "use_custom_credentials": 0, "email": None,
            "api_key_testecf": "key-test", "api_key_certecf": "key-cert",
            "api_key_ecf": None,
        })
        self.settings.save(ignore_permissions=True)

    def test_platform_login_and_env_key(self):
        email, password, base_url = self.settings.login_credentials()
        self.assertEqual(email, "platform@invnt.io")
        self.assertEqual(password, "platform-secret")
        self.assertEqual(self.settings.active_api_key(), "key-test")

    def test_environment_selects_matching_key(self):
        self.settings.environment = "CerteCF"
        self.assertEqual(self.settings.active_api_key(), "key-cert")
        # Production key is unset -> loud failure, never a cross-env key.
        self.settings.environment = "eCF"
        with self.assertRaises(frappe.ValidationError):
            self.settings.active_api_key()

    def test_enable_requires_key_for_selected_environment(self):
        self.settings.environment = "eCF"  # no production key configured
        with self.assertRaises(frappe.ValidationError):
            self.settings.save(ignore_permissions=True)

    def test_custom_credentials_override_platform(self):
        self.settings.use_custom_credentials = 1
        self.settings.email = "standalone@empresa.do"
        self.settings.password = "own-secret"
        self.settings.save(ignore_permissions=True)
        email, password, _base = self.settings.login_credentials()
        self.assertEqual(email, "standalone@empresa.do")
        self.assertEqual(password, "own-secret")
