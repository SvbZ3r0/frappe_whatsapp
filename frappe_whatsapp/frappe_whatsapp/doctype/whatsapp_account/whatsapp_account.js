// Copyright (c) 2025, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Account", {
	refresh(frm) {
		if (!frm.is_new()) {
			if (!frm.doc.flow_pending_public_key) {
				frm.add_custom_button(__("Stage New Flow Encryption Key"), () => {
					frappe.confirm(
						__(
							"Generate a pending key pair? The active key will remain in use until the pending key is registered and verified with Meta."
						),
						() =>
							frm.call({
								method: "generate_flow_encryption_keys",
								doc: frm.doc,
								freeze: true,
								freeze_message: __("Generating encryption keys..."),
								callback: () => frm.reload_doc(),
							})
					);
				}, __("Flows"));
			}

			if (frm.doc.flow_public_key || frm.doc.flow_pending_public_key) {
				const registering_pending_key = Boolean(frm.doc.flow_pending_public_key);
				const register_label = registering_pending_key
					? "Register Pending Flow Key"
					: "Register Flow Public Key";
				frm.add_custom_button(__(register_label), () => {
					frm.call({
						method: "register_flow_public_key",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Registering public key with Meta..."),
						callback: (r) => {
							if (!r.exc) {
								frappe.show_alert({
									message: registering_pending_key
										? __("Pending key registered; verify it to complete rotation")
										: __("Flow public key registered"),
									indicator: "green",
								});
								frm.reload_doc();
							}
						},
					});
				}, __("Flows"));

				if (frm.doc.flow_pending_key_status !== "Generated - Not Registered") {
					const verify_label = registering_pending_key
						? "Verify and Activate Pending Key"
						: "Verify Flow Public Key";
					frm.add_custom_button(__(verify_label), () => {
						frm.call({
							method: "verify_flow_public_key",
							doc: frm.doc,
							freeze: true,
							freeze_message: __("Verifying public key with Meta..."),
							callback: (r) => {
								if (r.exc) return;
								const verified = r.message?.verified;
								frappe.show_alert({
									message: verified
										? registering_pending_key
											? __(
													"Pending key verified and activated; the former key was retained for fallback decryption"
												)
											: __("Flow public key verified")
										: __("Meta did not verify this account's Flow public key"),
									indicator: verified ? "green" : "red",
								});
								frm.reload_doc();
							},
						});
					}, __("Flows"));
				}
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
