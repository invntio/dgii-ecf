# MSeller delivery resilience

The production-hardening design that extends this runbook is documented in
[Bohio <-> MSeller contingency design](mseller-contingency-design.md).

This app treats Frappe-to-MSeller delivery and MSeller-to-DGII processing as
separate failure boundaries.

## Provider behaviour confirmed by MSeller

- Query sent documents by eNCF, preferably through the batch endpoint.
- MSeller rejects a retransmission after `Aceptado` or `Aceptado Condicional`.
- Documents in pending, queued, error, or rejected states may be received again
  and MSeller will retransmit/re-sign them.
- MSeller applies the real signature timestamp.
- When DGII is unavailable, MSeller retries ten times over about twenty minutes
  and also runs a daily recovery job for stuck documents.
- The Free plan permits 5 requests/second with a burst of 10. Higher limits must
  be configured only for the contracted plan.
- A signed-XML download endpoint is not public yet.

## Local state machine

| State | Meaning | Automatic action |
|---|---|---|
| `Pending` | Durable outbox row, not sent yet | Dispatch |
| `SUBMITTING` | POST started and committed locally | Reconcile if stale for 10 minutes |
| `UNCONFIRMED` | Transport/provider result is uncertain | Query by eNCF before retransmission |
| `RECIBIDO` / `PROCESANDO` | MSeller owns processing | Poll only |
| `Aceptado` / `Aceptado Condicional` | Fiscal terminal success | Never retransmit |
| `Rechazado` | Correctable remote terminal failure | Manual retry after correction |
| `ERROR` | Authentication, validation, or unknown permanent failure | Manual intervention |

## Retry policy

Connection failures, HTTP 429, and HTTP 5xx use a 1, 2, 5, 15, 30, then
60-minute capped backoff. Before retransmitting an uncertain submission, the app
queries MSeller by eNCF. A retransmission occurs only after two `found=false`
responses separated by the grace interval, or when an authorized user explicitly
retries a remote failure that MSeller reports as error/rejected.

HTTP 400, persistent 401, and 403 responses are not automatically retried.

## Operational checks

1. Watch `ECF Document Log` rows in `UNCONFIRMED` or `ERROR`.
2. Investigate authentication and validation errors before pressing Retry.
3. Configure `API Requests per Second` for the customer's actual MSeller plan;
   leave it at 5 for Free.
4. Back up Frappe's database, including request/response JSON, tracking IDs,
   security codes, QR URLs, and signed XML paths.
5. Periodically test by disabling connectivity, restoring it, and confirming
   that the outbox reconciles without consuming another eNCF.
6. Review append-only `ECF Delivery Event` records for every attempt, response,
   reconciliation, alert, and state transition.
