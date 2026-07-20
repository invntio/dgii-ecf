"""Static contracts for the standalone app's Frappe module boundaries."""

from pathlib import Path

from frappe.tests import UnitTestCase

import dgii_ecf.hooks as hooks


APP_ROOT = Path(__file__).resolve().parents[1]


class TestFrappeStructure(UnitTestCase):
    def test_owned_doctype_javascript_is_colocated(self):
        module_root = APP_ROOT / "dgii_ecf" / "doctype"
        document_log = module_root / "ecf_document_log"
        provider_settings = module_root / "ecf_provider_settings"
        self.assertTrue((document_log / "ecf_document_log.js").is_file())
        self.assertTrue((document_log / "ecf_document_log_list.js").is_file())
        self.assertTrue(
            (provider_settings / "ecf_provider_settings.js").is_file()
        )
        self.assertNotIn("ECF Document Log", hooks.doctype_js)
        self.assertNotIn("ECF Provider Settings", hooks.doctype_js)

    def test_native_sales_invoice_stays_an_explicit_extension(self):
        self.assertEqual(
            hooks.doctype_js["Sales Invoice"], "public/js/sales_invoice.js"
        )
        self.assertIn("Sales Invoice", hooks.doc_events)

    def test_legacy_frappe_test_case_is_not_used(self):
        legacy_import = "from frappe.tests.utils import " + "Frappe" + "TestCase"
        offenders = []
        for path in APP_ROOT.rglob("test_*.py"):
            if legacy_import in path.read_text():
                offenders.append(str(path.relative_to(APP_ROOT)))
        self.assertFalse(offenders, offenders)
