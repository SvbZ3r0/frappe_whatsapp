// Copyright (c) 2025, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Conversation Outbox", {
	refresh(frm) {
		if (!["Sending", "Unknown"].includes(frm.doc.status)) return;
		frm.add_custom_button(__("Retry Delivery"), () => {
			frappe.confirm(
				__("Meta may already have accepted this message. Retrying can send a duplicate. Continue?"),
				() =>
					frm.call({
						method: "retry_delivery",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Queueing explicit retry..."),
						callback: () => frm.reload_doc(),
					})
			);
		});
	},
});
