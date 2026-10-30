# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

"""Declarative, stateful WhatsApp conversation runtime."""

import json
import re

import frappe
from frappe import _
from frappe.utils import now_datetime


MAX_AUTOMATIC_STEPS = 20


def schedule_incoming_message(doc, event=None):
	"""Queue an incoming message after its webhook transaction commits."""
	if doc.type != "Incoming" or doc.conversation_processed:
		return
	if frappe.flags.in_test or frappe.flags.in_migrate or frappe.flags.in_install:
		return
	frappe.enqueue(
		"frappe_whatsapp.conversation.process_incoming_message",
		message_name=doc.name,
		queue="short",
		enqueue_after_commit=True,
		job_id=f"whatsapp-conversation-{doc.name}",
		deduplicate=True,
	)


def process_incoming_message(message_name):
	"""Advance or start one conversation for an incoming WhatsApp message."""
	message = frappe.get_doc("WhatsApp Message", message_name, for_update=True)
	if message.type != "Incoming":
		return None
	if message.conversation_processed:
		return message.conversation_session
	if not message.whatsapp_account or not message.get("from"):
		mark_message_processed(message)
		return None

	session = get_active_session(message.whatsapp_account, message.get("from"))
	if session:
		script = frappe.get_doc("WhatsApp Conversation Script", session.conversation_script)
		if not script.enabled:
			session.status = "Handed Off"
			session.save(ignore_permissions=True)
			mark_message_processed(message, session.name)
			return session.name
		transition = find_matching_transition(script, session.current_step, message)
		if not transition:
			mark_message_processed(message, session.name)
			return session.name
		context = parse_context(session.context_json)
		if transition.capture_as:
			context[transition.capture_as] = message.message or ""
		session.context_json = json.dumps(context)
		execute_from_step(script, session, transition.destination_step, message)
	else:
		script = find_entry_script(message)
		if not script:
			mark_message_processed(message)
			return None
		session = frappe.get_doc(
			{
				"doctype": "WhatsApp Conversation Session",
				"conversation_script": script.name,
				"status": "Active",
				"whatsapp_account": message.whatsapp_account,
				"phone_number": message.get("from"),
				"context_json": "{}",
				"started_on": now_datetime(),
				"last_activity_on": now_datetime(),
			}
		).insert(ignore_permissions=True)
		execute_from_step(script, session, script.start_step, message)

	mark_message_processed(message, session.name)
	return session.name


def get_active_session(whatsapp_account, phone_number):
	rows = frappe.get_all(
		"WhatsApp Conversation Session",
		filters={
			"whatsapp_account": whatsapp_account,
			"phone_number": phone_number,
			"status": "Active",
		},
		pluck="name",
		order_by="last_activity_on desc, creation desc",
		limit=1,
	)
	return frappe.get_doc("WhatsApp Conversation Session", rows[0], for_update=True) if rows else None


def find_entry_script(message):
	for name in frappe.get_all(
		"WhatsApp Conversation Script",
		filters={"enabled": 1},
		pluck="name",
		order_by="priority asc, creation asc",
	):
		script = frappe.get_doc("WhatsApp Conversation Script", name)
		if script.is_available_on(message.whatsapp_account) and message_matches(
			message, script.entry_match_type, script.entry_value
		):
			return script
	return None


def find_matching_transition(script, source_step, message):
	rows = [row for row in script.transitions if row.source_step == source_step]
	rows.sort(key=lambda row: (row.match_type == "Default", row.priority or 10, row.idx))
	for row in rows:
		if row.match_type == "Default" or message_matches(message, row.match_type, row.match_value):
			return row
	return None


def message_matches(message, match_type, match_value=None):
	text = message.message or ""
	value = match_value or ""
	if match_type in ("Any", "Default"):
		return True
	if match_type == "Content Type":
		return (message.content_type or "").casefold() == value.casefold()
	if match_type == "Exact":
		return text.strip().casefold() == value.strip().casefold()
	if match_type == "Contains":
		return value.casefold() in text.casefold()
	if match_type == "Regex":
		return bool(re.search(value, text, flags=re.IGNORECASE))
	return False


def execute_from_step(script, session, step_id, incoming_message=None):
	"""Execute actions until a step waits, hands off, ends, or reaches the safety cap."""
	steps = {step.step_id: step for step in script.steps}
	current_id = step_id
	for _ in range(MAX_AUTOMATIC_STEPS):
		step = steps.get(current_id)
		if not step:
			frappe.throw(_("Conversation step does not exist: {0}").format(current_id))
		session.current_step = current_id
		session.last_activity_on = now_datetime()
		context = parse_context(session.context_json)

		if step.action_type == "Send Text":
			send_message(session, "text", render_text(step.message, context))
		elif step.action_type == "Send Options":
			send_message(
				session,
				"interactive",
				render_text(step.message, context),
				buttons=step.options or "[]",
			)
		elif step.action_type == "Launch Flow":
			send_message(
				session,
				"flow",
				render_text(step.message, context),
				flow=step.flow,
				flow_action=step.flow_action or "navigate",
			)
		elif step.action_type == "Run Handler":
			result = dispatch_handler(step.handler_key, script, session, incoming_message, context)
			context.update(result.get("context") or {})
			session.context_json = json.dumps(context)
			if result.get("status") in ("Completed", "Handed Off", "Failed"):
				session.status = result["status"]
				session.save(ignore_permissions=True)
				return
			if result.get("next_step"):
				current_id = result["next_step"]
				continue
		elif step.action_type == "Handoff":
			session.status = "Handed Off"
			session.save(ignore_permissions=True)
			return
		elif step.action_type == "End":
			session.status = "Completed"
			session.save(ignore_permissions=True)
			return

		session.save(ignore_permissions=True)
		if step.wait_for_reply:
			return
		automatic = find_automatic_transition(script, current_id)
		if not automatic:
			return
		current_id = automatic.destination_step

	frappe.throw(_("Conversation exceeded the automatic step safety limit"))


def find_automatic_transition(script, source_step):
	rows = [
		row
		for row in script.transitions
		if row.source_step == source_step and row.match_type in ("Default", "Any")
	]
	rows.sort(key=lambda row: (row.match_type != "Default", row.priority or 10, row.idx))
	return rows[0] if rows else None


def dispatch_handler(handler_key, script, session, incoming_message, context):
	handlers = frappe.get_hooks("whatsapp_conversation_handlers") or {}
	configured = handlers.get(handler_key) if isinstance(handlers, dict) else None
	if isinstance(configured, str):
		configured = [configured]
	if not configured or len(configured) != 1:
		frappe.throw(_("Conversation handler key must resolve to exactly one installed-app method"))
	try:
		result = frappe.get_attr(configured[0])(
			{
				"script": script.name,
				"session": session.name,
				"whatsapp_account": session.whatsapp_account,
				"phone_number": session.phone_number,
				"current_step": session.current_step,
				"incoming_message": incoming_message.name if incoming_message else None,
				"context": dict(context),
			}
		)
	except Exception:
		frappe.log_error(
			message=(
				f"script={script.name}; session={session.name}; "
				f"step={session.current_step}; handler_key={handler_key}"
			),
			title="WhatsApp Conversation Handler Error",
		)
		frappe.throw(_("Conversation action failed"))
	if result is None:
		return {}
	if not isinstance(result, dict):
		frappe.throw(_("Conversation handlers must return a dictionary or None"))
	return result


def send_message(session, content_type, message, **values):
	payload = {
		"doctype": "WhatsApp Message",
		"type": "Outgoing",
		"to": session.phone_number,
		"whatsapp_account": session.whatsapp_account,
		"content_type": content_type,
		"message": message or "",
		"conversation_session": session.name,
	}
	payload.update(values)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def render_text(template, context):
	"""Replace simple {{ variable }} placeholders without evaluating code."""
	return re.sub(
		r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
		lambda match: str(context.get(match.group(1), "")),
		template or "",
	)


def parse_context(value):
	try:
		context = json.loads(value or "{}")
	except (TypeError, json.JSONDecodeError):
		context = {}
	return context if isinstance(context, dict) else {}


def mark_message_processed(message, session_name=None):
	message.db_set(
		{
			"conversation_processed": 1,
			"conversation_session": session_name,
		},
		update_modified=False,
	)
