// Copyright (c) 2025, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Conversation Script", {
	refresh(frm) {
		set_available_account_query(frm);
	},

	whatsapp_account(frm) {
		frm.clear_table("available_accounts");
		frm.refresh_field("available_accounts");
		set_available_account_query(frm);
	},

	authoring_mode(frm) {
		frm.__authoring_mode_changed = true;
		frappe.show_alert({
			message: __("The edited representation updates the other on save; JSON wins conflicts."),
			indicator: "blue",
		});
	},

	after_save(frm) {
		if (!frm.__authoring_mode_changed) return;
		frm.__authoring_mode_changed = false;
		frm.reload_doc();
	},
});

function set_available_account_query(frm) {
	if (!frm.doc.whatsapp_account) return;
	frappe.db.get_value("WhatsApp Account", frm.doc.whatsapp_account, "business_id").then((r) => {
		const business_id = r.message && r.message.business_id;
		if (!business_id) return;
		frm.set_query("whatsapp_account", "available_accounts", () => ({
			filters: { business_id },
		}));
	});
}
