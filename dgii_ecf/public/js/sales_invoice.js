const DGII_PRINT_FORMAT = "DGII e-CF Sales Invoice";

frappe.ui.form.on("Sales Invoice", {
	refresh: set_regional_print_format,
	company: set_regional_print_format,
});

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
