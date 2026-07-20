# e-CF operations runbook

This runbook covers the durable delivery and reconciliation layer implemented by
`dgii_ecf`. MSeller signs and transmits to DGII. The application must not invent
or print a formal contingency state that MSeller has not reported.

## What operators see

Use **ECF Document Log** as the delivery source of truth.

| State | Meaning | Operator action |
| --- | --- | --- |
| `Pending` | The fiscal payload is durable and awaits a worker | Verify scheduler, Redis, and the `long` queue if it ages |
| `SUBMITTING` | A POST attempt started | Wait; after 10 minutes it is reconciled by eNCF |
| `UNCONFIRMED` | The POST outcome is ambiguous | Do not create another invoice/eNCF; reconciliation is automatic |
| `RECIBIDO`, `PROCESANDO` | The provider owns or is processing the document | Polling is automatic; investigate only after a stalled alert |
| `Aceptado`, `Aceptado Condicional` | Terminal fiscal success | No retry is permitted |
| `Rechazado`, `ERROR` | Automatic requests are blocked | Open the invoice and delivery log, correct the cause, then use **Retry e-CF Delivery** |

The **Action Required** list shortcut shows blocked rows. The provider settings
form shows whether the circuit is healthy, open, or running a recovery probe.
The **View Delivery Timeline** action opens the append-only audit events.

## Alert delivery

`dgii_ecf` emits a generic alert dictionary through the
`dgii_ecf_operational_alert_handlers` hook. It contains severity, company, eNCF,
fiscal log, source document, status, title, message, and a deterministic event
ID. A consumer is responsible for business recipients and channels.

Bohío's adapter currently resolves every enabled Condo Manager assigned through
a Company User Permission and sends:

| Severity | Channels |
| --- | --- |
| Critical: blocked document, provider outage, 60-minute stall | App, push, email |
| Warning: unconfirmed for 15 minutes | App, push |
| Info: recovery | App |

If no consumer delivers the alert, the standalone app creates a Frappe Desk
notification for enabled System Managers and Administrator. Alert failures never
roll back a fiscal state transition; undelivered blocked alerts are retried by
the five-minute scheduler.

## Safe retry procedure

1. Open the Sales Invoice and its ECF Document Log.
2. Read the normalized error and sanitized provider response.
3. Correct credentials, permissions, or business data as applicable. Do not edit
   `request_json`, change the eNCF, or create a replacement invoice merely to
   retry transport.
4. Prefer **Refresh e-CF Status** first; it only queries the provider.
5. Use **Retry e-CF Delivery** only for `UNCONFIRMED`, `ERROR`, or `Rechazado`.
6. The worker queries by eNCF before POSTing. It resends only after confirmed
   absence or when an authorized retry explicitly allows a correctable remote
   failure. Accepted documents remain protected even under a forced retry.

## Provider outage

After five connection/5xx failures within five minutes, the per-company circuit
opens for five minutes. Fiscal payloads remain in the database. One leased probe
is allowed after the pause; concurrent probes are rejected. A successful call,
or any HTTP response proving the provider is reachable, closes the availability
breaker. Document-level 4xx/429 handling still applies independently.

Do not disable fiscal validation or manually change statuses during an outage.
Restore connectivity and allow the scheduler to reconcile the durable queue.

## Audit verification

Application code cannot update or delete ECF Delivery Events, and a fiscal log
with events cannot be deleted. To verify the sequence and SHA-256 chain:

```bash
bench --site <site> execute dgii_ecf.delivery.verify_event_chain \
  --kwargs '{"log_name":"ECF-LOG-00001"}'
```

A valid result returns `valid: true`, its event count, and no errors. Treat a
hash, sequence, or parent-head mismatch as an audit incident; preserve database
and application backups before investigating.

## Health checks after recovery

1. Scheduler is enabled and workers consume the `long` queue.
2. Provider settings show a Closed circuit and a recent successful call.
3. `Pending`, stale `SUBMITTING`, and due `UNCONFIRMED` counts decrease.
4. Remote states progress to `Aceptado` or `Aceptado Condicional`.
5. Every retried document retains one eNCF and one `request_sha256` across its
   delivery timeline.
6. Managers receive at most one alert per unchanged failure signature and a
   recovery notice when appropriate.

## External dependencies still pending

- MSeller signed-XML download endpoint.
- Formal MSeller SLA and authoritative rate-limit/`Retry-After` contract.
- Provider-confirmed formal contingency state and instructions, if exposed.
- Production-like outage drill evidence.
- Optional database/WORM retention according to organizational policy.
