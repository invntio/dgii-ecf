// Frappe loads this controller by convention beside the owned DocType.
frappe.ui.form.on("ECF Document Log", {
	async refresh(frm) {
		frm.add_custom_button(__("View Delivery Timeline"), () => {
			frappe.set_route("List", "ECF Delivery Event", {
				ecf_document_log: frm.doc.name,
			});
		});

		if (frm.doc.reference_doctype === "Sales Invoice" && frm.doc.reference_name) {
			frm.add_custom_button(__("Open Sales Invoice"), () => {
				frappe.set_route("Form", "Sales Invoice", frm.doc.reference_name);
			});
		}

		const { message } = await frappe.call({
			method: "dgii_ecf.api.get_ecf_log_operator_state",
			args: { ecf_log: frm.doc.name },
		});
		if (message?.presentation) {
			const presentation = message.presentation;
			const provider_detail = presentation.provider_messages?.[0];
			const detail = provider_detail
				? `<br><small>${frappe.utils.escape_html(provider_detail)}</small>`
				: "";
			frm.dashboard.set_headline_alert(
				`<strong>${frappe.utils.escape_html(presentation.title)}</strong><br>${frappe.utils.escape_html(presentation.message)}${detail}`,
				presentation.indicator || "orange"
			);
		} else if (frm.doc.operator_action_required) {
			frm.dashboard.set_headline_alert(
				__("This e-CF is blocked and requires an operator action. Review the provider response before retrying."),
				"red"
			);
		} else if (["UNCONFIRMED", "RECIBIDO", "PROCESANDO"].includes(frm.doc.status)) {
			frm.dashboard.set_headline_alert(
				__("Delivery is being reconciled automatically. Do not create another fiscal document."),
				"orange"
			);
		}
	},
});
