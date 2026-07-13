app_name = "dgii_ecf"
app_title = "DGII ECF"
app_publisher = "Invntio"
app_description = "Provider-agnostic DGII e-CF electronic invoicing for Frappe/ERPNext (Dominican Republic)"
app_email = "support@invntio.com"
app_license = "MIT"

required_apps = ["erpnext"]

after_install = "dgii_ecf.install.ensure_module_owner"
after_migrate = "dgii_ecf.install.ensure_module_owner"

fixtures = [
    {
        "doctype": "Print Format",
        "filters": [["name", "=", "DGII e-CF Sales Invoice"]],
    }
]

jinja = {
    "methods": [
        "dgii_ecf.printing.get_ecf_print_data",
        "dgii_ecf.printing.qr_svg_data_uri",
    ]
}

doc_events = {
    "Sales Invoice": {
        "on_submit": "dgii_ecf.events.sales_invoice.on_submit",
        "on_cancel": "dgii_ecf.events.sales_invoice.on_cancel",
    }
}

scheduler_events = {
    "cron": {
        "*/15 * * * *": ["dgii_ecf.tasks.poll_pending_documents"],
    },
    "daily": ["dgii_ecf.tasks.expire_ranges"],
}

override_doctype_dashboards = {
    "Sales Invoice": "dgii_ecf.dashboard.sales_invoice_dashboard",
}

# Extension points consumed by the builder (see README):
# ecf_debit_note_flags / ecf_source_invoice_fields — other apps append here.
