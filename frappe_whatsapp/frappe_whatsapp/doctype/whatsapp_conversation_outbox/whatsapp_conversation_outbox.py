# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class WhatsAppConversationOutbox(Document):
	@frappe.whitelist()
	def retry_delivery(self):
		"""Explicitly retry an ambiguous delivery after an operator accepts the risk."""
		frappe.only_for("System Manager")
		if self.status == "Sent":
			frappe.throw(_("A sent outbox entry cannot be retried"))
		self.status = "Pending"
		self.claimed_on = None
		self.last_error = None
		self.save(ignore_permissions=True)
		frappe.db.set_value(
			"WhatsApp Message",
			self.outgoing_message,
			"status",
			"Queued",
			update_modified=False,
		)
		frappe.enqueue(
			"frappe_whatsapp.conversation.deliver_outbox",
			outbox_name=self.name,
			queue="short",
			enqueue_after_commit=True,
			job_id=f"whatsapp-conversation-outbox-{self.name}",
			deduplicate=True,
		)
		return self.name
