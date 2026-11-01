# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from frappe_whatsapp.utils.phone import normalize_phone_number


class WhatsAppFlowSession(Document):
	pass


def make_session_key(whatsapp_account, flow_token):
	if not whatsapp_account or not flow_token:
		return None
	return hashlib.sha256(f"{whatsapp_account}\0{flow_token}".encode()).hexdigest()


def ensure_flow_session_for_launch(message):
	"""Create one durable Flow execution record after its launch token is persisted."""
	if (
		message.type != "Outgoing"
		or message.content_type != "flow"
		or not message.flow
		or not message.flow_token
		or not message.whatsapp_account
	):
		return None

	session_key = make_session_key(message.whatsapp_account, message.flow_token)
	existing = frappe.db.get_value(
		"WhatsApp Flow Session", {"session_key": session_key}, "name"
	)
	if existing:
		return existing

	savepoint = "whatsapp_flow_session_create"
	frappe.db.savepoint(savepoint)
	try:
		session = frappe.get_doc(
			{
				"doctype": "WhatsApp Flow Session",
				"flow": message.flow,
				"status": "Active",
				"whatsapp_account": message.whatsapp_account,
				"phone_number": normalize_phone_number(message.to),
				"flow_token": message.flow_token,
				"session_key": session_key,
				"outgoing_message": message.name,
				"started_on": message.creation or now_datetime(),
				"last_activity_on": message.creation or now_datetime(),
			}
		).insert(ignore_permissions=True)
		frappe.db.release_savepoint(savepoint)
		return session.name
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		frappe.db.rollback(save_point=savepoint)
		return frappe.db.get_value(
			"WhatsApp Flow Session", {"session_key": session_key}, "name"
		)


def get_flow_session(whatsapp_account, flow_token, *, for_update=False):
	name = frappe.db.get_value(
		"WhatsApp Flow Session",
		{"session_key": make_session_key(whatsapp_account, flow_token)},
		"name",
		for_update=for_update,
	)
	return frappe.get_doc("WhatsApp Flow Session", name) if name else None


def complete_flow_session(whatsapp_account, flow_token, completion_message):
	session = get_flow_session(whatsapp_account, flow_token, for_update=True)
	if not session:
		return None
	frappe.db.set_value(
		"WhatsApp Flow Session",
		session.name,
		{
			"status": "Completed",
			"completion_message": completion_message,
			"last_activity_on": now_datetime(),
			"completed_on": now_datetime(),
		},
		update_modified=False,
	)
	return session.name

