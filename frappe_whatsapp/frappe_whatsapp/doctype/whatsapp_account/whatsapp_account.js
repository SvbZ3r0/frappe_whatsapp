// Copyright (c) 2025, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Account", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Generate Flow Encryption Keys"), () => {
				frappe.confirm(
					__("Generate a new key pair? Registering it will replace the key Meta uses for this phone number."),
					() => frm.call({
						method: "generate_flow_encryption_keys",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Generating encryption keys..."),
						callback: () => frm.reload_doc(),
					})
				);
			}, __("Flows"));

			if (frm.doc.flow_public_key) {
				frm.add_custom_button(__("Register Flow Public Key"), () => {
					frm.call({
						method: "register_flow_public_key",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Registering public key with Meta..."),
						callback: (r) => {
							if (!r.exc) frappe.show_alert({message: __("Flow public key registered"), indicator: "green"});
						},
					});
				}, __("Flows"));
			}

			frm.add_custom_button(__("Subscribe App to Webhooks"), () => {
				frappe.confirm(
					__("Subscribe this app to webhooks for WhatsApp Business Account {0}?", [
						frm.doc.business_id || frm.doc.account_name,
					]),
					() => {
						frm.call({
							doc: frm.doc,
							method: "subscribe_app",
							freeze: true,
							freeze_message: __("Subscribing app to webhooks..."),
							callback: (r) => {
								if (!r.exc) {
									frappe.show_alert({
										message: __("App subscribed to webhooks"),
										indicator: "green",
									});
								}
							},
						});
					}
				);
			});
		}
	},
});
