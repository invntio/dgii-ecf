"""Failure-boundary tests for the durable MSeller delivery workflow."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from dgii_ecf.api import (
    _classify_failure,
    reconcile_ecf_log,
    send_ecf_log,
)
from dgii_ecf.ecf.builder import build_ecf_json
from dgii_ecf.delivery import apply_transition, manager_users, sanitized_json
from dgii_ecf.mseller.client import (
    MSellerAuthError,
    MSellerClient,
    MSellerConnectionError,
    MSellerHTTPError,
)
from dgii_ecf.providers.base import EcfResult
from dgii_ecf.provider_health import (
    ProviderCircuitOpen,
    before_provider_call,
    record_provider_failure,
    record_provider_success,
)
from dgii_ecf.tests.test_dgii_ecf import COMPANY, _ensure_test_invoice


class TestDeliveryResilience(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.invoice = _ensure_test_invoice()
        self.encf = "E320000066661"
        frappe.db.delete("ECF Delivery Event", {"encf": self.encf})
        frappe.db.delete("ECF Document Log", {"encf": self.encf})

    def tearDown(self):
        frappe.db.delete("ECF Delivery Event", {"encf": self.encf})
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

    def test_first_not_found_check_does_not_resend(self):
        log = self._log(status="UNCONFIRMED")
        provider = MagicMock()
        provider.get_status_batch.return_value = [
            EcfResult(success=False, encf=self.encf, raw={"found": False})
        ]
        with patch("dgii_ecf.api.get_provider", return_value=provider):
            result = reconcile_ecf_log(log.name)

        provider.send.assert_not_called()
        self.assertEqual(result.status, "UNCONFIRMED")
        self.assertEqual(result.not_found_count, 1)
        self.assertIsNotNone(result.next_retry_at)

    def test_second_spaced_not_found_resends_exact_payload_and_same_encf(self):
        log = self._log(status="UNCONFIRMED")
        frappe.db.set_value(
            "ECF Document Log",
            log.name,
            {
                "not_found_count": 1,
                "last_status_checked_at": add_to_date(now_datetime(), minutes=-3),
            },
            update_modified=False,
        )
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

        provider.send.assert_called_once_with(frappe.parse_json(log.request_json))
        self.assertEqual(provider.send.call_args.args[0]["ECF"]["Encabezado"]["IdDoc"]["eNCF"], self.encf)
        self.assertEqual(result.status, "RECIBIDO")
        self.assertEqual(result.internal_track_id, "track-1")
        self.assertEqual(result.attempt_count, 1)

    def test_force_never_resends_accepted_document(self):
        log = self._log(status="Aceptado")
        provider = MagicMock()
        with patch("dgii_ecf.api.get_provider", return_value=provider):
            result = send_ecf_log(log.name, force=True)
        provider.send.assert_not_called()
        self.assertEqual(result.status, "Aceptado")

    def test_refresh_does_not_unblock_or_resend_negative_status(self):
        log = self._log(status="Rechazado")
        provider = MagicMock()
        provider.get_status_batch.return_value = [
            EcfResult(success=False, encf=self.encf, raw={"found": False})
        ]
        with (
            patch("dgii_ecf.api.get_provider", return_value=provider),
            patch("dgii_ecf.delivery._create_notifications"),
        ):
            result = reconcile_ecf_log(
                log.name,
                resend_if_missing=False,
                resend_remote_failure=False,
            )

        provider.send.assert_not_called()
        self.assertEqual(result.status, "Rechazado")
        self.assertTrue(result.operator_action_required)
        self.assertIsNone(result.next_retry_at)

    def test_blocked_transition_notifies_once_and_stops_scheduling(self):
        log = self._log(status="PROCESANDO")
        response = {"message": "invalid totals"}
        with patch("dgii_ecf.delivery._create_notifications") as notifications:
            first = apply_transition(
                log.name,
                {"status": "Rechazado", "next_status_check_at": now_datetime()},
                "Batch Status",
                response=response,
            )
            second = apply_transition(
                log.name,
                {"status": "Rechazado"},
                "Batch Status",
                response=response,
            )

        notifications.assert_called_once()
        self.assertTrue(first.operator_action_required)
        self.assertIsNone(first.next_status_check_at)
        self.assertIsNone(first.next_retry_at)
        self.assertEqual(second.last_alert_signature, first.last_alert_signature)
        self.assertEqual(
            frappe.db.count("ECF Delivery Event", {"ecf_document_log": log.name}),
            2,
        )

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

    def test_notification_payload_redacts_credentials(self):
        rendered = sanitized_json(
            {"status": "ERROR", "token": "secret", "nested": {"api_key": "key"}}
        )
        self.assertNotIn("secret", rendered)
        self.assertNotIn('"key"', rendered)
        self.assertEqual(rendered.count("[REDACTED]"), 2)

    def test_manager_recipients_are_company_scoped_and_role_checked(self):
        with (
            patch(
                "dgii_ecf.delivery.frappe.get_all",
                side_effect=[["manager@example.com", "other@example.com"], ["manager@example.com"]],
            ),
            patch("dgii_ecf.delivery.frappe.db.get_value", return_value=1),
        ):
            self.assertEqual(manager_users(COMPANY), ["manager@example.com"])


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

    def test_only_due_in_flight_rows_are_polled(self):
        from dgii_ecf import tasks

        due = frappe._dict(
            name="ECF-DUE",
            company=COMPANY,
            encf="E320000000001",
            status="PROCESANDO",
            next_status_check_at=add_to_date(now_datetime(), minutes=-1),
        )
        future = frappe._dict(
            name="ECF-FUTURE",
            company=COMPANY,
            encf="E320000000002",
            status="PROCESANDO",
            next_status_check_at=add_to_date(now_datetime(), minutes=10),
        )
        provider = MagicMock()
        provider.get_status_batch.return_value = []
        with (
            patch.object(tasks, "is_enabled", return_value=True),
            patch.object(tasks.frappe, "get_all", return_value=[due, future]),
            patch.object(tasks, "get_provider", return_value=provider),
            patch.object(tasks, "record_not_found") as not_found,
            patch.object(tasks, "provider_call", side_effect=lambda company, cb: cb()),
        ):
            tasks.poll_pending_documents()

        provider.get_status_batch.assert_called_once_with([due.encf])
        not_found.assert_called_once_with(due.name)

    def test_stalled_unconfirmed_document_alerts_once_per_scheduler_run(self):
        from dgii_ecf import tasks

        row = frappe._dict(
            name="ECF-STALLED",
            company=COMPANY,
            encf="E320000000003",
            status="UNCONFIRMED",
            reference_doctype="Sales Invoice",
            reference_name="ACC-SINV-STALLED",
            request_json="{}",
            request_sha256="hash",
            attempt_count=1,
            first_unconfirmed_at=add_to_date(now_datetime(), minutes=-16),
            last_remote_status_at=None,
            last_alert_signature=None,
        )
        with (
            patch.object(tasks, "is_enabled", return_value=True),
            patch.object(tasks.frappe, "get_all", return_value=[row]),
            patch.object(tasks, "notify_stalled") as notify,
        ):
            tasks.alert_stalled_documents()

        notify.assert_called_once_with(
            row,
            level="Warning",
            reason="delivery has been unconfirmed for at least 15 minutes",
        )


class TestProviderCircuitBreaker(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.settings = frappe.db.exists("ECF Provider Settings", {"company": COMPANY})
        if not self.settings:
            self.settings = frappe.get_doc(
                {
                    "doctype": "ECF Provider Settings",
                    "company": COMPANY,
                    "enabled": 0,
                    "provider_type": "MSeller",
                    "environment": "TesteCF",
                    "rate_limit_per_second": 5,
                }
            ).insert(ignore_permissions=True).name
        self._reset()

    def tearDown(self):
        self._reset()

    def _reset(self):
        frappe.db.set_value(
            "ECF Provider Settings",
            self.settings,
            {
                "circuit_state": "Closed",
                "circuit_open_until": None,
                "consecutive_failures": 0,
                "failure_window_started_at": None,
            },
            update_modified=False,
        )

    def test_circuit_opens_after_five_provider_failures_and_closes_on_probe(self):
        with patch("dgii_ecf.provider_health.notify_provider_outage") as notify:
            for _ in range(5):
                record_provider_failure(COMPANY, MSellerConnectionError("offline"))

        state = frappe.db.get_value(
            "ECF Provider Settings",
            self.settings,
            ["circuit_state", "consecutive_failures", "circuit_open_until"],
            as_dict=True,
        )
        self.assertEqual(state.circuit_state, "Open")
        self.assertEqual(state.consecutive_failures, 5)
        self.assertTrue(state.circuit_open_until)
        notify.assert_called_once_with(COMPANY)
        with self.assertRaises(ProviderCircuitOpen):
            before_provider_call(COMPANY)

        frappe.db.set_value(
            "ECF Provider Settings",
            self.settings,
            "circuit_open_until",
            add_to_date(now_datetime(), minutes=-1),
            update_modified=False,
        )
        before_provider_call(COMPANY)
        self.assertEqual(
            frappe.db.get_value("ECF Provider Settings", self.settings, "circuit_state"),
            "Half Open",
        )
        record_provider_success(COMPANY)
        self.assertEqual(
            frappe.db.get_value("ECF Provider Settings", self.settings, "circuit_state"),
            "Closed",
        )
