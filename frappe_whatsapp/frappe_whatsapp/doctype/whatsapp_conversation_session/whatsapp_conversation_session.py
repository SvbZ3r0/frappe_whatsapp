# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import hashlib
import json
import re

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
		self.phone_number = normalize_phone_number(self.phone_number)
		if not self.phone_number:
			frappe.throw(_("Phone Number must contain digits"))
		self.active_session_key = (
			make_active_session_key(self.whatsapp_account, self.phone_number)
			if self.status == "Active"
			else None
		)


def normalize_phone_number(phone_number):
	return re.sub(r"\D", "", phone_number or "")


def make_active_session_key(whatsapp_account, phone_number):
	phone_number = normalize_phone_number(phone_number)
	if not whatsapp_account or not phone_number:
		return None
	return hashlib.sha256(f"{whatsapp_account}\0{phone_number}".encode()).hexdigest()

