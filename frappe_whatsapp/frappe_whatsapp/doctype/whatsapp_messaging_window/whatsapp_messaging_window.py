# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, get_datetime, now_datetime

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_conversation_session.whatsapp_conversation_session import (
	normalize_phone_number,
)


SERVICE_WINDOW_HOURS = 24


class MessagingWindowClosedError(frappe.ValidationError):
	pass


class WhatsAppMessagingWindow(Document):
	def validate(self):
		self.phone_number = normalize_phone_number(self.phone_number)
		self.window_key = make_window_key(self.whatsapp_account, self.phone_number)
		self.status = "Open" if is_window_open(self, now_datetime()) else "Closed"


def make_window_key(whatsapp_account, phone_number):
	phone_number = normalize_phone_number(phone_number)
	if not whatsapp_account or not phone_number:
		return None
	return hashlib.sha256(f"{whatsapp_account}\0{phone_number}".encode()).hexdigest()


def record_incoming_message(message):
	"""Open or extend the customer-service window from an inbound user message."""
	if message.type != "Incoming" or not message.whatsapp_account or not message.get("from"):
		return None

	phone_number = normalize_phone_number(message.get("from"))
	window_key = make_window_key(message.whatsapp_account, phone_number)
	received_on = get_datetime(message.get("message_timestamp") or message.creation or now_datetime())
	expires_on = add_to_date(received_on, hours=SERVICE_WINDOW_HOURS)
	values = {
		"last_incoming_message": message.name,
		"last_customer_message_on": received_on,
		"service_window_expires_on": expires_on,
		"status": "Open",
	}
	name = frappe.db.get_value(
		"WhatsApp Messaging Window", {"window_key": window_key}, "name", for_update=True
	)
	if name:
		frappe.db.set_value(
			"WhatsApp Messaging Window", name, values, update_modified=False
		)
		return name

	savepoint = "whatsapp_messaging_window_create"
	frappe.db.savepoint(savepoint)
	try:
		window = frappe.get_doc(
			{
				"doctype": "WhatsApp Messaging Window",
				"whatsapp_account": message.whatsapp_account,
				"phone_number": phone_number,
				"window_key": window_key,
				**values,
			}
		).insert(ignore_permissions=True)
		frappe.db.release_savepoint(savepoint)
		return window.name
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		frappe.db.rollback(save_point=savepoint)
		name = frappe.db.get_value(
			"WhatsApp Messaging Window", {"window_key": window_key}, "name", for_update=True
		)
		if not name:
			raise
		frappe.db.set_value(
			"WhatsApp Messaging Window", name, values, update_modified=False
		)
		return name


def get_messaging_window(whatsapp_account, phone_number, *, for_update=False):
	window_key = make_window_key(whatsapp_account, phone_number)
	if not window_key:
		return None
	name = frappe.db.get_value(
		"WhatsApp Messaging Window",
		{"window_key": window_key},
		"name",
		for_update=for_update,
	)
	return frappe.get_doc("WhatsApp Messaging Window", name) if name else None


def is_window_open(window, at_time=None):
	return bool(
		window
		and window.service_window_expires_on
		and get_datetime(window.service_window_expires_on) > get_datetime(at_time or now_datetime())
	)


def assert_free_form_message_allowed(whatsapp_account, phone_number, at_time=None):
	window = get_messaging_window(whatsapp_account, phone_number)
	if is_window_open(window, at_time):
		return window
	if window and window.status != "Closed":
		frappe.db.set_value(
			"WhatsApp Messaging Window", window.name, "status", "Closed", update_modified=False
		)
	raise MessagingWindowClosedError(
		_(
			"The WhatsApp 24-hour customer-service window is closed. "
			"Send an approved template and wait for the customer to reply before sending this message."
		)
	)


def close_expired_messaging_windows():
	"""Keep the displayed status aligned with the timestamp-based policy check."""
	frappe.db.set_value(
		"WhatsApp Messaging Window",
		{"status": "Open", "service_window_expires_on": ("<=", now_datetime())},
		"status",
		"Closed",
		update_modified=False,
	)

