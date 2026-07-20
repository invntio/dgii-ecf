// Frappe loads this list controller by convention beside the owned DocType.
frappe.listview_settings["ECF Document Log"] = {
	add_fields: ["status", "operator_action_required", "alert_level"],

	onload(listview) {
		listview.page.add_inner_button(__("Action Required"), () => {
			frappe.set_route("List", "ECF Document Log", {
				operator_action_required: 1,
			});
		});
	},

	get_indicator(doc) {
		if (doc.operator_action_required) {
			return [__("Action Required"), "red", "operator_action_required,=,1"];
		}
		if (doc.alert_level === "Warning") {
			return [__("Warning"), "orange", "alert_level,=,Warning"];
		}
		if (["Aceptado", "Aceptado Condicional"].includes(doc.status)) {
			return [__(doc.status), "green", `status,=,${doc.status}`];
		}
		if (["RECIBIDO", "PROCESANDO", "UNCONFIRMED"].includes(doc.status)) {
			return [__(doc.status), "blue", `status,=,${doc.status}`];
		}
		return [__(doc.status || "Pending"), "gray", `status,=,${doc.status}`];
	},
};
