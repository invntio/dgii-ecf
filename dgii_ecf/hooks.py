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
        "dgii_ecf.printing.get_billing_property_print_data",
        "dgii_ecf.printing.qr_svg_data_uri",
    ]
}

doctype_js = {"Sales Invoice": "public/js/sales_invoice.js"}

doc_events = {
    "Sales Invoice": {
        "before_validate": "dgii_ecf.events.sales_invoice.set_print_language",
        "before_submit": "dgii_ecf.readiness.validate_sales_invoice_readiness",
        "on_submit": "dgii_ecf.events.sales_invoice.on_submit",
        "on_cancel": "dgii_ecf.events.sales_invoice.on_cancel",
    }
}

scheduler_events = {
    "cron": {
        "*/15 * * * *": ["dgii_ecf.tasks.poll_pending_documents"],
        "* * * * *": ["dgii_ecf.tasks.retry_failed_documents"],
    },
    "daily": ["dgii_ecf.tasks.expire_ranges"],
}

override_doctype_dashboards = {
    "Sales Invoice": "dgii_ecf.dashboard.sales_invoice_dashboard",
}
