// Copyright (c) 2025, Navari and contributors
// For license information, please see license.txt

// Copied from the Utility Service Request, never edited on the Contract; Is Active stays editable
const LOCKED_ROW_FIELDS = [
	"utility_property",
	"item_code",
	"start_date",
	"end_date",
	"contract_length_months",
	"adjustment_rule",
	"insurance",
	"lease_status",
	"vacated_on",
];

frappe.ui.form.on("Contract", {
	refresh: function (frm) {
		lock_properties(frm);
		set_is_active_readonly(frm);
	},
});

// Child handlers are shared by every form with this child table, including the
// Utility Service Request, so each one returns early outside a Contract.
frappe.ui.form.on("Contract Utility Property Item", {
	is_active: function (frm, cdt, cdn) {
		if (frm.doctype !== "Contract") return;

		let row = locals[cdt][cdn];
		if (frm.doc.docstatus === 1 && !row.__islocal && row.is_active) {
			frappe.msgprint("You cannot activate a property once the contract is submitted.");
			frappe.model.set_value(cdt, cdn, "is_active", 0);
			return;
		}

		if (frm.doc.docstatus === 1 && !row.is_active && frm.doc.utility_service_request) {
			ask_vacated_on(frm, row);
		}
	},

	form_render: function (frm, cdt, cdn) {
		if (frm.doctype !== "Contract") return;

		let row = locals[cdt][cdn];
		if (frm.doc.docstatus === 1 && !row.is_active) {
			frm.fields_dict.properties.grid.grid_rows_by_docname[cdn].toggle_editable(
				"is_active",
				false
			);
		} else {
			frm.fields_dict.properties.grid.grid_rows_by_docname[cdn].toggle_editable(
				"is_active",
				true
			);
		}
	},
});

// Ending a lease early on the Contract: the tenant's leaving date makes the row
// Lease Status Invalid, here and (on save) on its Utility Service Request. The
// month vacated in is still billed; the invoice may bill the occupied days only.
function ask_vacated_on(frm, row) {
	const dialog = new frappe.ui.Dialog({
		title: __("End lease of {0}", [row.utility_property]),
		fields: [
			{
				fieldname: "vacated_on",
				fieldtype: "Date",
				label: __("Vacated On"),
				reqd: 1,
				default: frappe.datetime.get_today(),
				description: __(
					"The month of this date is billed in full. To bill only the days occupied, tick Bill Occupied Days Only on that invoice and attach the signed approval.",
				),
			},
		],
		primary_action_label: __("End Lease"),
		primary_action(values) {
			if (row.start_date && values.vacated_on < row.start_date) {
				frappe.msgprint(__("The tenant cannot vacate before the lease starts on {0}.", [frappe.datetime.str_to_user(row.start_date)]));
				return;
			}
			frappe.model.set_value(row.doctype, row.name, "lease_status", "Invalid");
			frappe.model.set_value(row.doctype, row.name, "vacated_on", values.vacated_on);
			dialog.hide();
			frappe.show_alert({
				message: __("Save the Contract to end the lease. Its Utility Service Request is updated too."),
				indicator: "orange",
			});
		},
	});
	// Closing without a date keeps the lease running
	dialog.onhide = () => {
		if (!row.vacated_on) {
			// Set directly: through set_value the Is Active handler would refuse reactivating
			row.is_active = 1;
			frm.refresh_field("properties");
		}
	};
	dialog.show();
}

// The rows and the request link come from the Utility Service Request through
// create_contract, and so do the dates of a Contract linked to one. Only this
// form's docfields change, so the request's own table stays editable.
function lock_properties(frm) {
	const grid = frm.fields_dict.properties.grid;

	frm.set_df_property("utility_service_request", "read_only", 1);
	if (frm.doc.utility_service_request) {
		frm.set_df_property("start_date", "read_only", 1);
		frm.set_df_property("end_date", "read_only", 1);
	}
	// Booleans, not 1: the grid passes these to jQuery toggleClass, which toggles on a number
	frm.set_df_property("properties", "cannot_add_rows", true);
	frm.set_df_property("properties", "cannot_delete_rows", true);
	LOCKED_ROW_FIELDS.forEach((fieldname) => grid.update_docfield_property(fieldname, "read_only", 1));

	// The grid is drawn before refresh runs, so redraw it to hide Add and Delete
	frm.refresh_field("properties");
}

function set_is_active_readonly(frm) {
	if (frm.doc.docstatus === 1) {
		(frm.doc.properties || []).forEach((row) => {
			let grid_row = frm.fields_dict.properties.grid.grid_rows_by_docname[row.name];
			if (grid_row) {
				grid_row.toggle_editable("is_active", row.is_active ? true : false);
			}
		});
	}
}
