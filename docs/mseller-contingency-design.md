# Bohio <-> MSeller contingency design

Status: implemented

This design covers the operational boundary between Bohio and MSeller. MSeller
remains responsible for signing, transmitting to DGII, provider-side retries,
and its certified fiscal contingency plan. Bohio is responsible for durable
capture, safe delivery, status reconciliation, and clear operator visibility.

It deliberately does not implement a second DGII connector, OFV declarations,
local signing, or the official DGII contingency legend. Those features should
only be added if MSeller exposes a documented API contract that requires Bohio
to participate in them.

## Confirmed provider behaviour

- Documents can be queried by eNCF, preferably with the batch endpoint.
- `Aceptado` and `Aceptado Condicional` are terminal and must not be resent.
- Pending, queued, error, and rejected documents may be received again; MSeller
  retransmits and signs them again.
- MSeller assigns the signature timestamp. Bohio supplies a stable invoice
  issue date and leaves the signature timestamp empty.
- If DGII is unavailable, MSeller retries ten times over approximately twenty
  minutes and runs a daily recovery job for stuck documents.
- HTTP 429 responses do not include `Retry-After`.
- The `signedXml` response is currently a path; no public download endpoint is
  available.
- The Developer contract has Enterprise-class or higher request limits. The
  exact per-key limit remains configuration, not a hard-coded assumption.

## Goals

1. Never lose a submitted invoice before it is represented in the durable
   fiscal outbox.
2. Never consume another eNCF because of a delivery retry.
3. Never resend a terminally accepted document.
4. Do not blindly resend after an ambiguous network failure.
5. Preserve the original payload for audit and deterministic retries.
6. Recover automatically after worker, Redis, network, or provider outages.
7. Make every stalled or rejected document visible to operators.

## Non-goals

- Signing XML locally.
- Connecting directly to DGII.
- Reproducing MSeller's provider-side retry system.
- Accepting new transactions while Bohio's database/application is itself
  unavailable; disaster recovery for the Bohio service is a separate plan.
- Declaring entry or exit from fiscal contingency in OFV.
- Printing the official DGII contingency legend without a provider-confirmed
  contingency status.
- Mutating the fiscal contents of a submitted invoice during a retry.

## Failure boundaries

| Boundary | Example | Bohio action |
| --- | --- | --- |
| Before durable outbox | Database validation or sequence allocation fails | Abort Sales Invoice submission |
| After outbox, before enqueue | Redis is unavailable | Scheduler dispatches the persisted `Pending` row |
| During POST | Timeout, connection reset, worker death | Mark/recover as `UNCONFIRMED`; query by eNCF |
| Provider transient failure | HTTP 429 or 5xx | Back off, then query before any resend |
| Provider owns document | `RECIBIDO` or `PROCESANDO` | Poll only; do not resend |
| Provider terminal success | `Aceptado` or `Aceptado Condicional` | Stop permanently |
| Correctable remote failure | `Rechazado` or remote `ERROR` | Alert; allow an explicit retry |
| Local permanent failure | HTTP 400, persistent 401/403 | Stop and require operator correction |

## State machine

| State | Meaning | Allowed automatic transition |
| --- | --- | --- |
| `Pending` | Durable payload has not been sent | `SUBMITTING` |
| `SUBMITTING` | POST began and the attempt marker was committed | Response state, or `UNCONFIRMED` when stale |
| `UNCONFIRMED` | Outcome of the POST is unknown | Query by eNCF; resend only after confirmed absence |
| `RECIBIDO` | MSeller received the document | Poll to another remote state |
| `PROCESANDO` | MSeller/DGII is processing it | Poll to another remote state |
| `Aceptado` | Terminal fiscal success | None |
| `Aceptado Condicional` | Terminal fiscal success with observations | None |
| `Rechazado` | Remote terminal failure | Explicit retry after review |
| `ERROR` | Permanent/local or remote error | Explicit retry after correction |

Every transition must be monotonic with respect to terminal success: no code
path, including `force=True`, may move an accepted document out of a terminal
state.

## Ambiguous delivery algorithm

An HTTP timeout only proves that Bohio did not receive a response. It does not
prove that MSeller did not receive the document.

1. Persist `UNCONFIRMED`, `first_unconfirmed_at`, and `next_retry_at`.
2. After the normal backoff, query MSeller's batch endpoint by eNCF.
3. If found:
   - adopt the remote state and metadata;
   - never POST when accepted or conditionally accepted;
   - poll only while pending/queued/received/processing;
   - expose manual retry for error/rejected.
4. If not found, record one absence observation and wait at least two additional
   minutes.
5. Query a second time. Resend the original stored payload only if both queries
   report `found=false` and are separated by the grace interval.
6. If the resend is also ambiguous, return to step 1. Do not allocate another
   eNCF.

The two-observation rule protects against an undocumented delay between MSeller
accepting a POST and exposing it through document queries. MSeller's duplicate
behaviour is the final safety net, not the primary idempotency mechanism.

## Payload and re-signing policy

- `request_json` is immutable after the first outbox insert.
- Store `request_sha256` and include it in every delivery event.
- Automatic and manual delivery retries send that exact payload.
- `FechaEmision` remains the Sales Invoice posting date.
- `FechaHoraFirma` remains empty in the request; MSeller assigns it on every
  signing attempt.
- Certificate renewal may therefore re-sign the same fiscal payload safely.
- A business-content correction is not a retry. It must use the applicable
  credit/debit note or a new document according to the fiscal workflow.

## Scheduling

### Delivery recovery

- Run every minute.
- Dispatch `Pending` rows.
- Treat `SUBMITTING` older than ten minutes as ambiguous.
- For connection failures, 429, and 5xx, use capped backoff of 1, 2, 5, 15, 30,
  then 60 minutes.
- Authentication and validation failures require intervention.

### Status synchronization

- Select only rows whose `next_status_check_at` is due.
- Group by company/API key and query batches of at most 100 eNCFs.
- Suggested cadence after MSeller receives a document: 1, 2, 5, 15, 30, then
  every 60 minutes.
- Stop polling terminal states.
- Keep the request-rate limiter configurable per provider settings. Do not
  encode the Developer contract limit in source code.

### Polling eligibility

Polling and delivery recovery use explicit, disjoint state sets:

| Policy | States | Behaviour |
| --- | --- | --- |
| Dispatch | `Pending` | Send the durable payload |
| Reconcile | `SUBMITTING`, `UNCONFIRMED` | Query by eNCF and apply the ambiguous-delivery algorithm |
| Poll | `RECIBIDO`, `PROCESANDO` | Include in due batch status queries |
| Positive terminal | `Aceptado`, `Aceptado Condicional` | Never query or resend again |
| Blocked / action required | `Rechazado`, `ERROR` | Clear scheduled checks, notify operators, and wait for an explicit action |

HTTP 429, connection errors, and 5xx responses are transient delivery outcomes,
not blocked fiscal states. They remain `UNCONFIRMED` and follow backoff plus
reconciliation. HTTP 400, persistent 401/403, remote `Rechazado`, and permanent
remote `ERROR` enter the blocked/action-required policy.

## Data model additions

Add the following fields to `ECF Document Log`:

| Field | Purpose |
| --- | --- |
| `request_sha256` | Proves that retries used the original fiscal payload |
| `first_unconfirmed_at` | Measures the duration of an ambiguous delivery |
| `not_found_count` | Requires two separated absence observations before resend |
| `last_status_checked_at` | Audit and adaptive polling |
| `next_status_check_at` | Due-based batch selection |
| `last_remote_status_at` | Detects documents stuck without provider progress |
| `operator_action_required` | Powers list filters and dashboards |
| `alert_level` | `None`, `Warning`, or `Critical` without overloading fiscal status |

Add an append-only `ECF Delivery Event` DocType:

- company, eNCF, log reference, and Sales Invoice reference;
- event timestamp and event type;
- status before and after;
- attempt number and request hash;
- HTTP status and normalized error kind;
- sanitized provider response;
- actor (`Scheduler`, worker, or user).

The event log must never contain API keys, passwords, bearer tokens, or complete
HTTP headers.

## Alerts and operator experience

| Condition | Notification |
| --- | --- |
| `UNCONFIRMED` for 15 minutes | Warning to Condo Manager/System Manager |
| No remote progress for 60 minutes | Critical alert with eNCF and last provider response |
| `Rechazado` | Immediate one-time rejection notification |
| Authentication/authorization error | Immediate configuration alert |
| Recovery after an alert | Resolution event; optional low-noise notification |

When a document enters `Rechazado` or `ERROR`:

1. set `operator_action_required = 1`;
2. clear `next_status_check_at` and `next_retry_at` so schedulers stop requesting
   that document;
3. create one notification for every active Condo Manager assigned to the
   document company, and one for System Managers only when the problem is a
   platform/global credential failure;
4. include Sales Invoice, eNCF, normalized cause, last provider message, and
   direct actions to open the invoice and e-CF log;
5. deduplicate by e-CF log + blocked status + provider-response hash, so the
   same failure never generates repeated notifications;
6. preserve the blocked state until an authorized user chooses Retry after
   correcting the underlying problem.

An explicit retry clears `operator_action_required`, records the user/action in
`ECF Delivery Event`, and reconciles by eNCF before deciding whether another POST
is necessary. If the document returns to the same blocked state with a different
provider response, a new notification is allowed.

Sales Invoice actions:

- `Refresh e-CF status`: always safe; performs a query only.
- `Retry e-CF delivery`: shown only for `UNCONFIRMED`, `ERROR`, or `Rechazado`;
  it must reconcile before POSTing.
- `Open e-CF log`: shows attempts, remote state, and operator guidance.
- No retry button for accepted, conditionally accepted, received, or processing
  documents.

Print behaviour remains intentionally distinct from fiscal contingency:

- draft: neutral preview;
- pending/unconfirmed: eNCF and fiscal payload persisted locally, awaiting the
  provider/DGII outcome;
- rejected/error: invalid until corrected;
- accepted: normal fiscal representation.

Do not show the official DGII contingency legend unless MSeller later exposes a
documented, authoritative contingency flag or status.

## Provider health and circuit breaker

Track health per company/API key without changing invoice fiscal states:

- open the breaker after five consecutive connection/5xx failures in five
  minutes;
- pause new POST attempts for five minutes while retaining every outbox row;
- allow one probe after the pause;
- close after a successful provider call;
- display provider health separately from document status.

The breaker reduces load during an outage. It must not block database commits,
sequence allocation, or later scheduler recovery.

## Signed XML

Continue storing `signedXml` as provider metadata. When MSeller publishes a
download endpoint:

1. download only after terminal acceptance;
2. save as a private Frappe `File` linked to the `ECF Document Log`;
3. verify content type and calculate SHA-256;
4. retain the provider path and local file checksum;
5. retry download independently from fiscal delivery status.

An XML backup failure must never cause an accepted document to be resent.

## Required tests

1. Timeout after MSeller accepted: query adopts the remote document, zero second
   POSTs.
2. First `found=false`: no resend.
3. Second separated `found=false`: resend exact payload and same eNCF.
4. Accepted and conditionally accepted: no automatic or forced resend.
5. Pending/queued: poll only.
6. Rejected/error: notification once and manual retry available.
7. 429 without `Retry-After`: calculated backoff.
8. Redis unavailable after commit: scheduler recovers `Pending`.
9. Worker death in `SUBMITTING`: stale reconciliation.
10. Circuit breaker opens, probes, and closes without losing outbox rows.
11. Event history records every transition with the same request hash.
12. Company/API-key isolation for batching, throttling, and health state.
13. End-to-end outage drill: disconnect, submit, restore, reconcile, accept,
    and confirm one eNCF and one fiscal payload.

## Delivery phases

### Phase 1 — safe ambiguity handling

- Add request hash, absence observations, and delivery events.
- Require two separated `found=false` responses before resend.
- Centralize terminal-state protection and rejection notifications.

### Phase 2 — adaptive synchronization and monitoring

- Add due-based batch polling.
- Add aging alerts and provider health/circuit breaker.
- Add manager-facing operational filters.

### Phase 3 — operator workflow and drills

- Improve invoice actions and event timeline.
- Add an automated outage/recovery integration test.
- Publish a runbook for support and condominium managers.

## Definition of done

- No failure scenario allocates a second eNCF for the same Sales Invoice.
- No accepted document can be retransmitted.
- An ambiguous POST is never resent after only one absence observation.
- Every delivery attempt and status transition is auditable.
- Operators receive actionable, deduplicated alerts for stalled and rejected
  documents.
- Restarting Frappe, Redis, or workers does not lose or strand a fiscal payload.
- Bohio relies on MSeller for signing, DGII transport, and formal fiscal
  contingency, with no duplicated regulatory implementation.
