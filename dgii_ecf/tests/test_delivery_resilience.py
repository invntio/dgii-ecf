"""Failure-boundary tests for the durable MSeller delivery workflow."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from dgii_ecf.api import (
    _classify_failure,
    reconcile_ecf_log,
    send_ecf_log,
)
from dgii_ecf.ecf.builder import build_ecf_json
from dgii_ecf.mseller.client import (
    MSellerAuthError,
    MSellerClient,
    MSellerConnectionError,
    MSellerHTTPError,
)
from dgii_ecf.providers.base import EcfResult
from dgii_ecf.tests.test_dgii_ecf import COMPANY, _ensure_test_invoice


class TestDeliveryResilience(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.invoice = _ensure_test_invoice()
        self.encf = "E320000066661"
        frappe.db.delete("ECF Document Log", {"encf": self.encf})

    def tearDown(self):
        frappe.db.delete("ECF Document Log", {"encf": self.encf})

    def _log(self, status="Pending"):
        payload = {
            "ECF": {
                "Encabezado": {
                    "IdDoc": {"TipoeCF": "32", "eNCF": self.encf},
                    "Emisor": {"RNCEmisor": "102320705"},
                }
            }
        }
        return frappe.get_doc(
            {
                "doctype": "ECF Document Log",
                "company": COMPANY,
                "direction": "Issued",
                "reference_doctype": "Sales Invoice",
                "reference_name": self.invoice.name,
                "ecf_type": "32",
                "encf": self.encf,
                "status": status,
                "request_json": frappe.as_json(payload),
            }
        ).insert(ignore_permissions=True)

    def test_builder_uses_invoice_date_and_leaves_signing_to_mseller(self):
        payload = build_ecf_json(self.invoice, self.encf, "32")

        self.assertEqual(
            payload["ECF"]["Encabezado"]["Emisor"]["FechaEmision"],
            frappe.utils.getdate(self.invoice.posting_date).strftime("%d-%m-%Y"),
        )
        self.assertEqual(payload["ECF"]["FechaHoraFirma"], "")

    def test_connection_failure_becomes_unconfirmed_with_backoff(self):
        log = self._log()
        provider = MagicMock()
        provider.send.side_effect = MSellerConnectionError("timeout")

        with patch("dgii_ecf.api.get_provider", return_value=provider):
            result = send_ecf_log(log.name)

        self.assertEqual(result.status, "UNCONFIRMED")
        self.assertEqual(result.error_kind, "Connection")
        self.assertEqual(result.attempt_count, 1)
        self.assertIsNotNone(result.next_retry_at)

    def test_reconcile_adopts_remote_document_without_second_send(self):
        log = self._log(status="UNCONFIRMED")
        provider = MagicMock()
        provider.get_status_batch.return_value = [
            EcfResult(
                success=True,
                status="Aceptado",
                encf=self.encf,
                security_code="ABC123",
                signed_xml_path="rnc/documents/ecf.xml",
                raw={"found": True},
            )
        ]

        with patch("dgii_ecf.api.get_provider", return_value=provider):
            result = reconcile_ecf_log(log.name)

        provider.send.assert_not_called()
        self.assertEqual(result.status, "Aceptado")
        self.assertEqual(result.security_code, "ABC123")
        self.assertEqual(result.signed_xml_path, "rnc/documents/ecf.xml")

    def test_reconcile_resends_when_mseller_confirms_missing(self):
        log = self._log(status="UNCONFIRMED")
        provider = MagicMock()
        provider.get_status_batch.return_value = [
            EcfResult(success=False, encf=self.encf, raw={"found": False})
        ]
        provider.send.return_value = EcfResult(
            success=True,
            status="RECIBIDO",
            encf=self.encf,
            track_id="track-1",
            raw={"internalTrackId": "track-1"},
        )

        with patch("dgii_ecf.api.get_provider", return_value=provider):
            result = reconcile_ecf_log(log.name)

        provider.send.assert_called_once()
        self.assertEqual(result.status, "RECIBIDO")
        self.assertEqual(result.internal_track_id, "track-1")
        self.assertEqual(result.attempt_count, 1)

    def test_only_transient_failures_are_scheduled(self):
        self.assertEqual(
            _classify_failure(MSellerAuthError("bad key", 403)),
            ("ERROR", "Authentication", 403, False),
        )
        self.assertEqual(
            _classify_failure(MSellerHTTPError(400, {})),
            ("ERROR", "Validation", 400, False),
        )
        self.assertEqual(
            _classify_failure(MSellerHTTPError(429, {})),
            ("UNCONFIRMED", "Rate Limit", 429, True),
        )
        self.assertEqual(
            _classify_failure(MSellerHTTPError(503, {})),
            ("UNCONFIRMED", "Provider", 503, True),
        )

    def test_status_query_classifies_persistent_401_as_authentication(self):
        client = MSellerClient(
            environment="TesteCF",
            email="test@example.com",
            password="secret",
            api_key="key",
            token="cached-token",
        )
        response = MagicMock(status_code=401)
        response.json.return_value = {"message": "expired"}
        with (
            patch.object(client, "authenticate", return_value="refreshed-token"),
            patch("dgii_ecf.mseller.client.requests.request", return_value=response),
        ):
            with self.assertRaises(MSellerAuthError) as error:
                client.get_status_batch([self.encf])

        self.assertEqual(error.exception.status_code, 401)


class TestOutboxDispatcher(FrappeTestCase):
    def test_pending_outbox_is_recovered_if_enqueue_after_commit_was_lost(self):
        from dgii_ecf import tasks

        row = frappe._dict(
            name="ECF-LOG-RECOVERY",
            reference_name="ACC-SINV-RECOVERY",
            status="Pending",
            last_attempt_at=None,
            next_retry_at=None,
        )
        with (
            patch.object(tasks, "is_enabled", return_value=True),
            patch.object(tasks.frappe, "get_all", return_value=[row]),
            patch.object(tasks.frappe, "enqueue") as enqueue,
        ):
            tasks.retry_failed_documents()

        enqueue.assert_called_once_with(
            "dgii_ecf.api.send_ecf_log",
            queue="long",
            job_id="ecf-submit-ACC-SINV-RECOVERY",
            deduplicate=True,
            ecf_log="ECF-LOG-RECOVERY",
        )
