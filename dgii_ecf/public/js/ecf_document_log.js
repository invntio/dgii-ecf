frappe.ui.form.on("ECF Document Log", {
	refresh(frm) {
		if (frm.doc.reference_doctype === "Sales Invoice" && frm.doc.reference_name) {
			frm.add_custom_button(__("Open Sales Invoice"), () => {
				frappe.set_route("Form", "Sales Invoice", frm.doc.reference_name);
			});
		}

		if (frm.doc.operator_action_required) {
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
