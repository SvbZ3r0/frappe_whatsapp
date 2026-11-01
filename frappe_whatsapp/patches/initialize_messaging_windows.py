# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_messaging_window.whatsapp_messaging_window import (
	record_incoming_message,
)
from frappe_whatsapp.utils.phone import normalize_phone_number


def execute():
	"""Backfill one customer-service window from each account/recipient's latest inbound."""
	seen = set()
	for row in frappe.get_all(
		"WhatsApp Message",
		filters={"type": "Incoming", "whatsapp_account": ("is", "set")},
		fields=["name", "whatsapp_account", "from", "creation", "message_timestamp"],
		order_by="creation desc",
	):
		key = (row.whatsapp_account, normalize_phone_number(row.get("from")))
		if not key[1] or key in seen:
			continue
		seen.add(key)
		row.type = "Incoming"
		record_incoming_message(row)

