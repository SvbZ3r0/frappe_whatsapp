# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_conversation_session.whatsapp_conversation_session import (
	make_active_session_key,
	normalize_phone_number,
)


def execute():
	"""Backfill unique active keys and close any legacy normalized duplicates."""
	claimed_keys = set()
	for row in frappe.get_all(
		"WhatsApp Conversation Session",
		filters={"status": "Active"},
		fields=["name", "whatsapp_account", "phone_number"],
		order_by="last_activity_on desc, creation desc",
	):
		phone_number = normalize_phone_number(row.phone_number)
		active_key = make_active_session_key(row.whatsapp_account, phone_number)
		if not active_key or active_key in claimed_keys:
			frappe.db.set_value(
				"WhatsApp Conversation Session",
				row.name,
				{
					"status": "Closed",
					"active_session_key": None,
					"ended_reason": "Duplicate active session closed during migration",
				},
				update_modified=False,
			)
			continue
		claimed_keys.add(active_key)
		frappe.db.set_value(
			"WhatsApp Conversation Session",
			row.name,
			{"phone_number": phone_number, "active_session_key": active_key},
			update_modified=False,
		)
