// Frappe loads this controller by convention beside the owned DocType.
frappe.ui.form.on("ECF Provider Settings", {
	refresh(frm) {
		if (frm.doc.circuit_state === "Open") {
			frm.dashboard.set_headline_alert(
				__("Provider calls are paused until {0}. Persisted e-CFs remain safe and will be reconciled automatically.", [
					frm.doc.circuit_open_until || __("the recovery probe"),
				]),
				"red"
			);
		} else if (frm.doc.circuit_state === "Half Open") {
			frm.dashboard.set_headline_alert(
				__("A provider recovery probe is in progress."),
				"orange"
			);
		} else if (frm.doc.last_provider_success_at) {
			frm.dashboard.set_headline_alert(
				__("Provider connection is healthy. Last success: {0}", [
					frm.doc.last_provider_success_at,
				]),
				"green"
			);
		}

		frm.add_custom_button(__("View Action Required e-CFs"), () => {
			frappe.set_route("List", "ECF Document Log", {
				company: frm.doc.company,
				operator_action_required: 1,
			});
		});
	},
});
