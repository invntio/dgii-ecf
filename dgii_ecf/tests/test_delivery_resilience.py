"""Failure-boundary tests for the durable MSeller delivery workflow."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from dgii_ecf.api import (
    _classify_failure,
    _record_failure,
    reconcile_ecf_log,
    send_ecf_log,
)
from dgii_ecf.ecf.builder import build_ecf_json
from dgii_ecf.alerts import dispatch_operational_alert
from dgii_ecf.delivery import (
    apply_transition,
    apply_remote_result,
    ecf_operator_presentation,
    record_event,
    sanitized_json,
    verify_event_chain,
)
from dgii_ecf.event_types import OUTBOX_PREPARED, REMOTE_STATUS
from dgii_ecf.install import backfill_delivery_event_integrity
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


class TestDeliveryResilience(IntegrationTestCase):
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

    def test_rate_limit_failure_uses_local_backoff_without_retry_after(self):
        log = self._log()
        provider = MagicMock()
        provider.send.side_effect = MSellerHTTPError(429, {"message": "slow down"})

        with patch("dgii_ecf.api.get_provider", return_value=provider):
            result = send_ecf_log(log.name)

        self.assertEqual(result.status, "UNCONFIRMED")
        self.assertEqual(result.error_kind, "Rate Limit")
        self.assertEqual(result.last_http_status, 429)
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

    def test_force_never_resends_conditionally_accepted_document(self):
        log = self._log(status="Aceptado Condicional")
        provider = MagicMock()
        with patch("dgii_ecf.api.get_provider", return_value=provider):
            result = send_ecf_log(log.name, force=True)
        provider.send.assert_not_called()
        self.assertEqual(result.status, "Aceptado Condicional")

    def test_refresh_does_not_unblock_or_resend_negative_status(self):
        log = self._log(status="Rechazado")
        provider = MagicMock()
        provider.get_status_batch.return_value = [
            EcfResult(success=False, encf=self.encf, raw={"found": False})
        ]
        with (
            patch("dgii_ecf.api.get_provider", return_value=provider),
            patch(
                "dgii_ecf.delivery.dispatch_operational_alert",
                return_value={"delivered": True},
            ),
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
        with patch(
            "dgii_ecf.delivery.dispatch_operational_alert",
            return_value={"delivered": True},
        ) as notifications:
            first = apply_transition(
                log.name,
                {"status": "Rechazado", "next_status_check_at": now_datetime()},
                REMOTE_STATUS,
                response=response,
            )
            second = apply_transition(
                log.name,
                {"status": "Rechazado"},
                REMOTE_STATUS,
                response=response,
            )

        notifications.assert_called_once()
        self.assertTrue(first.operator_action_required)
        self.assertIsNone(first.next_status_check_at)
        self.assertIsNone(first.next_retry_at)
        self.assertEqual(second.last_alert_signature, first.last_alert_signature)
        self.assertEqual(
            frappe.db.count("ECF Delivery Event", {"ecf_document_log": log.name}),
            4,
        )

    def test_dgii_code_004_is_presented_as_temporary_without_hiding_rejection(self):
        log = self._log(status="PROCESANDO")
        raw = {
            "data": {
                "dgiiResponse": [
                    frappe.as_json(
                        {
                            "estado": "Rechazado",
                            "mensajes": [
                                {
                                    "codigo": "004",
                                    "valor": "Ha ocurrido un error validando en eCF, favor intentar nueva vez.",
                                }
                            ],
                            "encf": None,
                            "secuenciaUtilizada": False,
                        }
                    )
                ]
            }
        }
        result = EcfResult(
            success=True,
            status="Rechazado",
            encf=self.encf,
            raw=raw,
        )
        with patch(
            "dgii_ecf.delivery.dispatch_operational_alert",
            return_value={"delivered": True},
        ):
            updated = apply_remote_result(log.name, result)

        self.assertEqual(updated.status, "Rechazado")
        self.assertEqual(updated.error_kind, "Provider")
        presentation = ecf_operator_presentation(updated)
        self.assertEqual(presentation["kind"], "temporary_validation_error")
        self.assertEqual(presentation["indicator"], "orange")
        self.assertFalse(presentation["sequence_used"])
        self.assertIn("004", presentation["codes"])

    def test_omitted_batch_item_is_not_treated_as_not_found(self):
        log = self._log(status="UNCONFIRMED")
        provider = MagicMock()
        provider.get_status_batch.return_value = []

        with patch("dgii_ecf.api.get_provider", return_value=provider):
            result = reconcile_ecf_log(log.name)

        provider.send.assert_not_called()
        self.assertEqual(result.not_found_count, 0)
        self.assertEqual(result.status, "UNCONFIRMED")
        self.assertIsNotNone(result.next_retry_at)

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
            {
                "status": "ERROR",
                "token": "secret",
                "nested": {
                    "api_key": "key",
                    "client-secret": "client-value",
                    "id_token": "identity-value",
                },
                "message": "Authorization: Bearer abc.def.ghi",
            }
        )
        self.assertNotIn('"secret"', rendered)
        self.assertNotIn('"key"', rendered)
        self.assertNotIn("client-value", rendered)
        self.assertNotIn("identity-value", rendered)
        self.assertNotIn("abc.def.ghi", rendered)
        self.assertGreaterEqual(rendered.count("[REDACTED]"), 5)

    def test_persisted_provider_error_redacts_quoted_secret_values(self):
        log = self._log(status="SUBMITTING")
        error = MSellerHTTPError(
            400,
            {"client_secret": "never-persist-this", "message": "invalid"},
        )
        self.assertNotIn("never-persist-this", str(error))
        self.assertNotIn("never-persist-this", frappe.as_json(error.payload))

        with patch(
            "dgii_ecf.delivery.dispatch_operational_alert",
            return_value={"delivered": True},
        ):
            _record_failure(log.name, error, 1)

        refreshed = frappe.get_doc("ECF Document Log", log.name)
        self.assertNotIn("never-persist-this", refreshed.error or "")
        self.assertNotIn("never-persist-this", refreshed.response_json or "")
        self.assertIn("[REDACTED]", refreshed.error)

    def test_large_structured_provider_response_remains_valid_json(self):
        serialized = sanitized_json({"message": "x" * 20000})

        parsed = frappe.parse_json(serialized)
        self.assertTrue(parsed["truncated"])
        self.assertLessEqual(len(serialized), 10000)

    def test_operational_alert_uses_consumer_hook_without_business_roles(self):
        handler = MagicMock(return_value={"delivered": True})
        with (
            patch("dgii_ecf.alerts.frappe.get_hooks", return_value=["consumer.handler"]),
            patch("dgii_ecf.alerts.frappe.get_attr", return_value=handler),
            patch("dgii_ecf.alerts._default_desk_handler") as fallback,
        ):
            result = dispatch_operational_alert(
                {"event_id": "evt-1", "company": COMPANY, "title": "Alert"}
            )

        self.assertTrue(result["delivered"])
        handler.assert_called_once()
        fallback.assert_not_called()

    def test_operational_alert_handler_failure_uses_safe_fallback(self):
        handler = MagicMock(side_effect=RuntimeError("consumer unavailable"))
        with (
            patch("dgii_ecf.alerts.frappe.get_hooks", return_value=["consumer.handler"]),
            patch("dgii_ecf.alerts.frappe.get_attr", return_value=handler),
            patch(
                "dgii_ecf.alerts._default_desk_handler",
                return_value={"delivered": True, "handler": "fallback"},
            ) as fallback,
            patch("dgii_ecf.alerts.frappe.log_error") as log_error,
        ):
            result = dispatch_operational_alert(
                {"event_id": "evt-2", "company": COMPANY, "title": "Alert"}
            )

        self.assertTrue(result["delivered"])
        self.assertTrue(result["used_fallback"])
        fallback.assert_called_once()
        log_error.assert_called_once()

    def test_delivery_events_are_chained_and_tampering_is_detected(self):
        log = self._log()
        first = record_event(log, OUTBOX_PREPARED, status_after="Pending")
        self.assertRegex(log.name, r"^ECF-LOG-\d{5}$")
        self.assertRegex(first.name, r"^ECF-EVENT-\d{5}$")
        apply_transition(
            log.name,
            {"status": "SUBMITTING", "attempt_count": 1},
            REMOTE_STATUS,
        )

        verification = verify_event_chain(log.name)
        self.assertTrue(verification["valid"], verification["errors"])
        self.assertEqual(verification["event_count"], 2)
        hashes = set(
            frappe.get_all(
                "ECF Delivery Event",
                filters={"ecf_document_log": log.name},
                pluck="request_sha256",
            )
        )
        self.assertEqual(hashes, {log.request_sha256})

        frappe.db.set_value(
            "ECF Delivery Event",
            first.name,
            "response_json",
            '{"tampered": true}',
            update_modified=False,
        )
        self.assertFalse(verify_event_chain(log.name)["valid"])

    def test_integrity_backfill_never_repairs_an_initialized_chain(self):
        log = self._log()
        event = record_event(log, OUTBOX_PREPARED, status_after="Pending")
        original_hash = event.event_hash
        original_head = log.last_event_hash
        frappe.db.set_value(
            "ECF Delivery Event",
            event.name,
            "response_json",
            '{"tampered": true}',
            update_modified=False,
        )

        backfill_delivery_event_integrity()

        self.assertFalse(verify_event_chain(log.name)["valid"])
        self.assertEqual(
            frappe.db.get_value("ECF Delivery Event", event.name, "event_hash"),
            original_hash,
        )
        self.assertEqual(
            frappe.db.get_value("ECF Document Log", log.name, "last_event_hash"),
            original_head,
        )

    def test_integrity_backfill_initializes_legacy_unanchored_history(self):
        log = self._log()
        event = record_event(log, OUTBOX_PREPARED, status_after="Pending")
        frappe.db.set_value(
            "ECF Delivery Event",
            event.name,
            {
                "event_sequence": 0,
                "event_type": "Outbox Prepared",
                "previous_event_hash": None,
                "event_hash": None,
            },
            update_modified=False,
        )
        frappe.db.set_value(
            "ECF Document Log",
            log.name,
            {"event_count": 0, "last_event_hash": None},
            update_modified=False,
        )

        backfill_delivery_event_integrity()

        self.assertTrue(verify_event_chain(log.name)["valid"])
        migrated = frappe.db.get_value(
            "ECF Delivery Event",
            event.name,
            ["event_sequence", "event_type", "previous_event_hash", "event_hash"],
            as_dict=True,
        )
        self.assertEqual(migrated.event_sequence, 1)
        self.assertEqual(migrated.event_type, OUTBOX_PREPARED)
        self.assertEqual(migrated.previous_event_hash, "0" * 64)
        self.assertTrue(migrated.event_hash)

    def test_delivery_event_rejects_normal_insert_update_and_delete(self):
        log = self._log()
        event = record_event(log, OUTBOX_PREPARED, status_after="Pending")

        unauthorized = frappe.copy_doc(event)
        unauthorized.name = None
        unauthorized.flags.from_delivery_service = False
        with self.assertRaises(frappe.ValidationError):
            unauthorized.insert(ignore_permissions=True)
        with self.assertRaises(frappe.ValidationError):
            event.save(ignore_permissions=True)
        with self.assertRaises(frappe.ValidationError):
            event.delete(ignore_permissions=True)
        with self.assertRaises(frappe.ValidationError):
            log.delete(ignore_permissions=True)

    def test_same_remote_status_preserves_stalled_alert_deduplication(self):
        log = self._log(status="PROCESANDO")
        frappe.db.set_value(
            "ECF Document Log",
            log.name,
            {"alert_level": "Critical", "last_alert_signature": "alert-1"},
            update_modified=False,
        )

        result = apply_transition(
            log.name,
            {"status": "PROCESANDO"},
            REMOTE_STATUS,
        )

        self.assertEqual(result.alert_level, "Critical")
        self.assertEqual(result.last_alert_signature, "alert-1")


class TestOutboxDispatcher(IntegrationTestCase):
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

    def test_stale_submitting_worker_is_reconciled_before_resend(self):
        from dgii_ecf import tasks

        row = frappe._dict(
            name="ECF-LOG-STALE",
            reference_name="ACC-SINV-STALE",
            status="SUBMITTING",
            last_attempt_at=add_to_date(now_datetime(), minutes=-11),
            next_retry_at=None,
        )
        with (
            patch.object(tasks, "is_enabled", return_value=True),
            patch.object(tasks.frappe, "get_all", return_value=[row]),
            patch.object(tasks, "apply_transition") as transition,
            patch.object(tasks.frappe, "enqueue") as enqueue,
        ):
            tasks.retry_failed_documents()

        transition.assert_called_once()
        enqueue.assert_called_once()
        self.assertEqual(
            enqueue.call_args.args[0], "dgii_ecf.api.reconcile_ecf_log"
        )

    def test_batch_polling_is_grouped_by_company(self):
        from dgii_ecf import tasks

        rows = [
            frappe._dict(
                name="ECF-A",
                company="Company A",
                encf="E320000000010",
                status="PROCESANDO",
                next_status_check_at=now_datetime(),
            ),
            frappe._dict(
                name="ECF-B",
                company="Company B",
                encf="E320000000020",
                status="PROCESANDO",
                next_status_check_at=now_datetime(),
            ),
        ]
        providers = {"Company A": MagicMock(), "Company B": MagicMock()}
        for company, provider in providers.items():
            row = next(item for item in rows if item.company == company)
            provider.get_status_batch.return_value = [
                EcfResult(
                    success=True,
                    status="PROCESANDO",
                    encf=row.encf,
                    raw={"found": True},
                )
            ]
        with (
            patch.object(tasks, "is_enabled", return_value=True),
            patch.object(tasks.frappe.db, "sql", return_value=rows),
            patch.object(
                tasks,
                "get_provider",
                side_effect=lambda company: providers[company],
            ),
            patch.object(tasks, "apply_remote_result") as apply_result,
            patch.object(tasks, "provider_call", side_effect=lambda company, cb: cb()),
        ):
            tasks.poll_pending_documents()

        providers["Company A"].get_status_batch.assert_called_once_with(
            ["E320000000010"]
        )
        providers["Company B"].get_status_batch.assert_called_once_with(
            ["E320000000020"]
        )
        self.assertEqual(apply_result.call_count, 2)

    def test_only_due_in_flight_rows_are_polled(self):
        from dgii_ecf import tasks

        due = frappe._dict(
            name="ECF-DUE",
            company=COMPANY,
            encf="E320000000001",
            status="PROCESANDO",
            next_status_check_at=add_to_date(now_datetime(), minutes=-1),
        )
        provider = MagicMock()
        provider.get_status_batch.return_value = []
        with (
            patch.object(tasks, "is_enabled", return_value=True),
            patch.object(tasks.frappe.db, "sql", return_value=[due]) as query,
            patch.object(tasks, "get_provider", return_value=provider),
            patch.object(tasks, "record_not_found") as not_found,
            patch.object(tasks, "apply_transition") as transition,
            patch.object(tasks, "provider_call", side_effect=lambda company, cb: cb()),
        ):
            tasks.poll_pending_documents()

        provider.get_status_batch.assert_called_once_with([due.encf])
        not_found.assert_not_called()
        transition.assert_called_once()
        self.assertTrue(
            any(
                "next_status_check_at <=" in str(call.args[0])
                for call in query.call_args_list
                if call.args
            )
        )

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
            patch.object(tasks.frappe, "get_all", side_effect=[[row], []]),
            patch.object(tasks, "notify_stalled") as notify,
        ):
            tasks.alert_stalled_documents()

        notify.assert_called_once_with(
            row,
            level="Warning",
            reason="delivery has been unconfirmed for at least 15 minutes",
        )


class TestProviderCircuitBreaker(IntegrationTestCase):
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
        frappe.cache.delete_value(f"dgii_ecf:provider_probe:{COMPANY}")
        frappe.db.set_value(
            "ECF Provider Settings",
            self.settings,
            {
                "circuit_state": "Closed",
                "circuit_open_until": None,
                "circuit_probe_expires_at": None,
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

    def test_expired_half_open_probe_lease_is_reclaimed(self):
        frappe.db.set_value(
            "ECF Provider Settings",
            self.settings,
            {
                "circuit_state": "Half Open",
                "circuit_probe_expires_at": add_to_date(now_datetime(), minutes=-1),
            },
            update_modified=False,
        )

        before_provider_call(COMPANY)

        lease = frappe.db.get_value(
            "ECF Provider Settings", self.settings, "circuit_probe_expires_at"
        )
        self.assertGreater(frappe.utils.get_datetime(lease), now_datetime())

    def test_failed_half_open_probe_reopens_without_duplicate_outage_alert(self):
        frappe.db.set_value(
            "ECF Provider Settings",
            self.settings,
            {
                "circuit_state": "Half Open",
                "circuit_probe_expires_at": add_to_date(now_datetime(), minutes=1),
                "consecutive_failures": 1,
            },
            update_modified=False,
        )
        with patch("dgii_ecf.provider_health.notify_provider_outage") as notify:
            record_provider_failure(COMPANY, MSellerConnectionError("still offline"))

        state = frappe.db.get_value(
            "ECF Provider Settings",
            self.settings,
            ["circuit_state", "circuit_open_until", "circuit_probe_expires_at"],
            as_dict=True,
        )
        self.assertEqual(state.circuit_state, "Open")
        self.assertIsNotNone(state.circuit_open_until)
        self.assertIsNone(state.circuit_probe_expires_at)
        notify.assert_not_called()

    def test_reachable_rate_limit_response_closes_half_open_breaker(self):
        frappe.db.set_value(
            "ECF Provider Settings",
            self.settings,
            {
                "circuit_state": "Half Open",
                "circuit_probe_expires_at": add_to_date(now_datetime(), minutes=1),
                "consecutive_failures": 5,
            },
            update_modified=False,
        )

        record_provider_failure(COMPANY, MSellerHTTPError(429, {"error": "slow"}))

        state = frappe.db.get_value(
            "ECF Provider Settings",
            self.settings,
            ["circuit_state", "consecutive_failures", "circuit_probe_expires_at"],
            as_dict=True,
        )
        self.assertEqual(state.circuit_state, "Closed")
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(state.circuit_probe_expires_at)
