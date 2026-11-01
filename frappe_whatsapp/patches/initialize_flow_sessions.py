# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow_session.whatsapp_flow_session import (
	ensure_flow_session_for_launch,
)


def execute():
	"""Backfill durable sessions for previously launched, tokenized Flows."""
	for name in frappe.get_all(
		"WhatsApp Message",
		filters={
			"type": "Outgoing",
			"content_type": "flow",
			"flow_token": ("is", "set"),
			"flow": ("is", "set"),
		},
		pluck="name",
	):
		ensure_flow_session_for_launch(frappe.get_doc("WhatsApp Message", name))

