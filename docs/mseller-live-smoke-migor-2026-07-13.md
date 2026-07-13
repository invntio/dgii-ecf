# MSeller live smoke test — MIGOR EIRL

Date: 2026-07-13  
Site: `bohio.localhost`  
Environment: `TesteCF`

No API keys, passwords, tokens, signed XML, or customer personal data are included in this report.

## Result

The ERPNext-to-MSeller integration is operational through document creation,
payload construction, authentication, signing, asynchronous submission, response
persistence, status polling, QR/security-code capture, and retry idempotency.

The end-to-end submission is green in DGII's public TesteCF verifier. Both tested
eNCFs appear there as `Aceptado`. MSeller's status endpoint nevertheless reports
the same documents as `Aceptado Condicional` with response code `75`: `Número de
secuencia no autorizada.` This provider/DGII status discrepancy must remain
visible; the integration does not overwrite the provider response with inferred
state.

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
| DGII public verifier | `Aceptado` |
| Retry | Returned the existing log; no second send or sequence consumption |
| Sequence range after first send | Advanced exactly once, to `571508200` |

A second submitted Sales Invoice, `ACC-SINV-2026-00011`, used the next sequential
eNCF `E320571508201`. MSeller signed and persisted it normally. Its API again
returned `Aceptado Condicional` / code `75`, while DGII's public TesteCF verifier
returned `Aceptado`. This confirms that the configured range
`571508200–571508299` is usable and that the mismatch is reproducible rather than
an isolated sequence result. Retrying the second invoice also returned its
existing log: the range remained at `571508201` and no duplicate was created.

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

## Follow-up actions

1. Ask MSeller why its query endpoint retains `Aceptado Condicional` / code `75`
   when DGII's public TesteCF verifier reports the same eNCFs as `Aceptado`.
2. Ask MSeller to enable validation-only (`validate=true`) for the gateway account,
   or continue treating validation calls as potentially consumptive.
3. Add MIGOR EIRL's real fiscal Company Address before certification/production;
   no address was present, so `DireccionEmisor` was omitted. Do not invent this
   value.

## Operational note

Bench workers were intentionally stopped during the test. The submitted invoice
was processed through the same public API manually. When workers restart, the
queued hook is safe: the existing live log makes the operation idempotent.
