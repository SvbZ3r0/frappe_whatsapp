# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow_session.whatsapp_flow_session import (
	ensure_flow_session_for_launch,
	get_flow_session,
)


class FlowExchangeReplayUnavailableError(Exception):
	pass


class FlowExchangeInProgressError(Exception):
	pass


class WhatsAppFlowExchange(Document):
	pass


def begin_exchange(context, flow_doc, request_fingerprint):
	"""Return a new exchange or its cached response for an exact Meta retry."""
	session = get_flow_session(
		context["whatsapp_account"], context["flow_token"], for_update=True
	)
	if not session:
		launch_name = frappe.db.get_value(
			"WhatsApp Message",
			{
				"type": "Outgoing",
				"content_type": "flow",
				"whatsapp_account": context["whatsapp_account"],
				"flow_token": context["flow_token"],
			},
			"name",
		)
		if launch_name:
			ensure_flow_session_for_launch(frappe.get_doc("WhatsApp Message", launch_name))
			session = get_flow_session(
				context["whatsapp_account"], context["flow_token"], for_update=True
			)
	if not session:
		raise FlowExchangeReplayUnavailableError("Flow session could not be established")

	existing_name = frappe.db.get_value(
		"WhatsApp Flow Exchange",
		{"request_fingerprint": request_fingerprint},
		"name",
		for_update=True,
	)
	if existing_name:
		existing = frappe.get_doc("WhatsApp Flow Exchange", existing_name)
		if existing.status == "Completed" and existing.response_json:
			return session, existing, json.loads(existing.response_json)
		if existing.status == "Processing":
			raise FlowExchangeInProgressError("Flow exchange is already processing")
		raise FlowExchangeReplayUnavailableError("Failed Flow exchange is not automatically replayed")

	retention_days = frappe.db.get_value(
		"WhatsApp Account", context["whatsapp_account"], "flow_exchange_retention_days"
	) or 7
	exchange = frappe.get_doc(
		{
			"doctype": "WhatsApp Flow Exchange",
			"flow_session": session.name,
			"status": "Processing",
			"action": context["action"],
			"screen": context.get("screen"),
			"request_fingerprint": request_fingerprint,
			"handler_key": flow_doc.handler_key,
			"started_on": now_datetime(),
			"response_expires_on": add_to_date(now_datetime(), days=retention_days),
		}
	).insert(ignore_permissions=True)
	return session, exchange, None


def complete_exchange(session, exchange, response):
	now = now_datetime()
	frappe.db.set_value(
		"WhatsApp Flow Exchange",
		exchange.name,
		{
			"status": "Completed",
			"response_screen": response.get("screen"),
			"response_json": json.dumps(response, separators=(",", ":")),
			"completed_on": now,
			"last_error": None,
		},
		update_modified=False,
	)
	frappe.db.set_value(
		"WhatsApp Flow Session",
		session.name,
		{
			"current_screen": response.get("screen"),
			"last_activity_on": now,
			"exchange_count": (session.exchange_count or 0) + 1,
		},
		update_modified=False,
	)


def fail_exchange(session, exchange, exc):
	frappe.db.set_value(
		"WhatsApp Flow Exchange",
		exchange.name,
		{"status": "Failed", "completed_on": now_datetime(), "last_error": type(exc).__name__},
		update_modified=False,
	)
	frappe.db.set_value(
		"WhatsApp Flow Session",
		session.name,
		{"last_activity_on": now_datetime()},
		update_modified=False,
	)


def purge_expired_exchange_responses():
	"""Remove cached handler responses after their idempotency retention period."""
	for name in frappe.get_all(
		"WhatsApp Flow Exchange",
		filters={"response_expires_on": ("<=", now_datetime()), "response_json": ("!=", "")},
		pluck="name",
		limit=500,
	):
		frappe.db.set_value(
			"WhatsApp Flow Exchange", name, "response_json", None, update_modified=False
		)
