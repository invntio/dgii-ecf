const DGII_PRINT_FORMAT = "DGII e-CF Sales Invoice";

frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		set_regional_print_format(frm);
		set_ecf_field_visibility(frm);
		add_ecf_retry_button(frm);
	},
	company(frm) {
		set_regional_print_format(frm);
		set_ecf_field_visibility(frm);
	},
});

async function set_ecf_field_visibility(frm) {
	if (!frm.doc.company) return;
	const { message } = await frappe.db.get_value("Company", frm.doc.company, "country");
	frm.set_df_property(
		"dgii_ecf_requires_fiscal_credit",
		"hidden",
		message?.country !== "Dominican Republic"
	);
}

async function add_ecf_retry_button(frm) {
	if (frm.doc.docstatus !== 1) return;

	const { message } = await frappe.call({
		method: "dgii_ecf.api.get_sales_invoice_ecf_state",
		args: { sales_invoice: frm.doc.name },
	});
	if (!message?.can_retry) return;

	frm.add_custom_button(__("Retry e-CF"), async () => {
		await frappe.call({
			method: "dgii_ecf.api.retry_sales_invoice",
			args: { sales_invoice: frm.doc.name },
			freeze: true,
			freeze_message: __("Queueing e-CF generation..."),
		});
		frappe.show_alert({ message: __("e-CF generation was queued."), indicator: "green" });
		frm.reload_doc();
	}, __("Electronic Invoicing"));
}

async function set_regional_print_format(frm) {
	if (!frm.doc.company) return;

	const meta = frappe.get_meta("Sales Invoice");
	if (meta._dgii_original_print_format === undefined) {
		meta._dgii_original_print_format =
			meta.default_print_format === DGII_PRINT_FORMAT ? "" : meta.default_print_format || "";
	}

	const { message } = await frappe.db.get_value("Company", frm.doc.company, "country");
	meta.default_print_format =
		message?.country === "Dominican Republic"
			? DGII_PRINT_FORMAT
			: meta._dgii_original_print_format;
}
