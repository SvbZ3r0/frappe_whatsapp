# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class WhatsAppConversationSession(Document):
	def before_insert(self):
		self.started_on = self.started_on or now_datetime()
		self.last_activity_on = self.last_activity_on or self.started_on

	def validate(self):
		try:
			context = json.loads(self.context_json or "{}")
		except (TypeError, json.JSONDecodeError) as exc:
			frappe.throw(_("Conversation context is invalid JSON: {0}").format(str(exc)))
		if not isinstance(context, dict):
			frappe.throw(_("Conversation context must be a JSON object"))
		if self.status == "Active":
			filters = {
				"whatsapp_account": self.whatsapp_account,
				"phone_number": self.phone_number,
				"status": "Active",
			}
			if self.name:
				filters["name"] = ["!=", self.name]
			if frappe.db.exists("WhatsApp Conversation Session", filters):
				frappe.throw(_("This phone number already has an active conversation session on the account"))

