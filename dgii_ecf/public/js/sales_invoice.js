const DGII_PRINT_FORMAT = "DGII e-CF Sales Invoice";

frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		set_regional_print_format(frm);
		set_ecf_field_visibility(frm);
		add_ecf_actions(frm);
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

async function add_ecf_actions(frm) {
	if (frm.doc.docstatus !== 1) return;

	const { message } = await frappe.call({
		method: "dgii_ecf.api.get_sales_invoice_ecf_state",
		args: { sales_invoice: frm.doc.name },
	});
	if (message?.log) {
		show_ecf_operator_message(frm, message.presentation);
		frm.add_custom_button(__("Open e-CF Log"), () => {
			frappe.set_route("Form", "ECF Document Log", message.log.name);
		}, __("Electronic Invoicing"));
	}

	if (message?.can_refresh) {
		frm.add_custom_button(__("Refresh e-CF Status"), async () => {
			await frappe.call({
				method: "dgii_ecf.api.refresh_sales_invoice_ecf_status",
				args: { sales_invoice: frm.doc.name },
				freeze: true,
				freeze_message: __("Checking MSeller status..."),
			});
			frappe.show_alert({ message: __("e-CF status refreshed."), indicator: "blue" });
			frm.reload_doc();
		}, __("Electronic Invoicing"));
	}

	if (!message?.can_retry) return;

	frm.add_custom_button(__("Retry e-CF Delivery"), async () => {
		await frappe.call({
			method: "dgii_ecf.api.retry_sales_invoice",
			args: { sales_invoice: frm.doc.name },
			freeze: true,
			freeze_message: __("Reconciling e-CF before retry..."),
		});
		frappe.show_alert({ message: __("e-CF retry was queued safely."), indicator: "green" });
		frm.reload_doc();
	}, __("Electronic Invoicing"));
}

function show_ecf_operator_message(frm, presentation) {
	if (!presentation) return;
	const provider_detail = presentation.provider_messages?.[0];
	const detail = provider_detail
		? `<br><small>${frappe.utils.escape_html(provider_detail)}</small>`
		: "";
	frm.dashboard.set_headline_alert(
		`<strong>${frappe.utils.escape_html(presentation.title)}</strong><br>${frappe.utils.escape_html(presentation.message)}${detail}`,
		presentation.indicator || "orange"
	);
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
