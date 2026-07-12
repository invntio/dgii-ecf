# DGII e-CF for Frappe

**Provider-agnostic electronic invoicing (e-CF / e-NCF) for Frappe and
ERPNext — Dominican Republic (DGII).**

This app turns any ERPNext `Sales Invoice` into a DGII-authorized electronic
fiscal receipt (e-CF), through a pluggable gateway layer: the fiscal engine
(eNCF sequence ranges, document builder, async submission, status polling,
audit log) is provider-independent, and each gateway is one `EcfProvider`
subclass. Multi-company (multi-tenant) by design.

## Features
- e-CF types 31, 32, 33, 34 (crédito fiscal, consumo, nota de débito, nota de crédito)
- Concurrency-safe eNCF sequence ranges, per company + environment (Test/Cert/Prod)
- Async submission (never blocks invoice posting), retry with eNCF reuse
- Status polling with batch queries; rejection notifications
- Full per-document audit trail (`ECF Document Log`)
- Platform gateway account + per-company API keys (SaaS/multi-tenant model),
  or per-company credentials (standalone model)
- Cancel guard: invoices with an accepted/in-flight e-CF cannot be cancelled
- i18n: English sources, Spanish translations included

## Supported providers

| Provider | Website | API docs | Status |
|----------|---------|----------|--------|
| [MSeller e-CF](https://mseller.app) | https://ecf.mseller.app | https://docs.ecf.mseller.app/docs | ✅ Supported |

Adding a provider = implementing one class (`dgii_ecf/providers/base.py`
→ `EcfProvider`) and registering it. PRs welcome.

## Installation
```bash
bench get-app https://gitlab.com/invntio/dgii-ecf
bench --site your-site install-app dgii_ecf
```

## Configuration
1. **ECF Gateway Account** (System Manager): the platform's gateway login.
2. **ECF Provider Settings** (one per company): environment + the company's
   per-environment API keys. Optional per-company login override.
3. **ECF Sequence Range**: the DGII-authorized eNCF ranges.

### Extension points (hooks)
- `ecf_debit_note_flags`: extra Sales Invoice fieldnames that mark a debit
  note (type 33). Default: `is_debit_note`.
- `ecf_source_invoice_fields`: extra fieldnames pointing at the original
  invoice for notes 33/34. Default: `return_against`.

## License
MIT
