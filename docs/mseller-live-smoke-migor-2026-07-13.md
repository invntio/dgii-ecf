# MSeller live smoke test — MIGOR EIRL

Date: 2026-07-13  
Site: `bohio.localhost`  
Environment: `TesteCF`

No API keys, passwords, tokens, signed XML, or customer personal data are included in this report.

## Result

The ERPNext-to-MSeller integration is operational through document creation,
payload construction, authentication, signing, asynchronous submission, response
persistence, status polling, QR/security-code capture, and retry idempotency.

The end-to-end fiscal result is not green yet. DGII returned `Aceptado
Condicional` with response code `75`: `Número de secuencia no autorizada.` The
remaining blocker is an eNCF sequence accepted for this TesteCF taxpayer/account.

## Evidence

| Artifact | Result |
| --- | --- |
| Submitted Sales Invoice | `ACC-SINV-2026-00010`, total DOP 1,475.00 |
| e-CF type | `32` |
| eNCF | `E320571508200` |
| ECF log | `ECF-LOG-00318` |
| MSeller receipt | Track ID, signed date, security code, and QR stored |
| Final status | `Aceptado Condicional` |
| DGII response | Code `75`, unauthorized sequence |
| Retry | Returned the existing log; no second send or sequence consumption |
| Sequence range | Advanced exactly once, to `571508200` |

An earlier dry-run attempt for draft invoice `ACC-SINV-2026-00009` unexpectedly
produced a real MSeller submission receipt for placeholder `E320000000000`.
MSeller therefore ignored `validate=true` for this gateway account. That document
also finished as `Aceptado Condicional` with response code `75` and is retained in
`ECF-LOG-00317` as an audit record.

## Code hardening

`MSellerProvider.send(..., validate=True)` now detects a submission receipt
(`internalTrackId`, `ecf`, or `qr_url`) and raises an explicit error instead of
reporting it as an ordinary validation failure. This avoids hiding accidental
sequence consumption when MSeller's validation-only feature is disabled for an
account.

A regression test covers the behavior. The complete `dgii_ecf` suite passes: 19
tests, 0 failures.

## External actions required for a fully accepted result

1. Obtain or identify an eNCF sequence accepted for MIGOR EIRL in TesteCF, then
   configure the corresponding `ECF Sequence Range` and retry with a new invoice.
2. Ask MSeller to enable validation-only (`validate=true`) for the gateway account,
   or continue treating validation calls as potentially consumptive.
3. Add MIGOR EIRL's real fiscal Company Address before certification/production;
   no address was present, so `DireccionEmisor` was omitted. Do not invent this
   value.

## Operational note

Bench workers were intentionally stopped during the test. The submitted invoice
was processed through the same public API manually. When workers restart, the
queued hook is safe: the existing live log makes the operation idempotent.
