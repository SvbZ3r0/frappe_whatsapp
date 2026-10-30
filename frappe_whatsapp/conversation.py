# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

"""Declarative, stateful WhatsApp conversation runtime."""

import hashlib
import json
import re

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_conversation_session.whatsapp_conversation_session import (
	make_active_session_key,
	normalize_phone_number,
)


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
		result = process_active_session(message, session)
	else:
		result = start_matching_session(message)

	mark_message_processed(message, result.name if result else None)
	return result.name if result else None


def get_active_session(whatsapp_account, phone_number, *, current_read=False):
	active_key = make_active_session_key(whatsapp_account, phone_number)
	name = frappe.db.get_value(
		"WhatsApp Conversation Session",
		{"active_session_key": active_key, "status": "Active"},
		"name",
		for_update=current_read,
	)
	if not name:
		return None
	session = frappe.get_doc("WhatsApp Conversation Session", name, for_update=True)
	if session.status == "Active" and session.active_session_key == active_key:
		return session
	return None


def process_active_session(message, session):
	"""Apply commands, limits and transitions to a locked active session."""
	script = frappe.get_doc("WhatsApp Conversation Script", session.conversation_script)
	if not script.enabled:
		record_session_activity(session, message)
		end_session(session, "Handed Off", _("Conversation script was disabled"))
		return session

	if matches_keyword(message.message, script.cancel_keywords):
		record_session_activity(session, message)
		end_session(session, "Cancelled", _("Customer used a cancel keyword"))
		return session
	if matches_keyword(message.message, script.restart_keywords):
		end_session(session, "Closed", _("Customer restarted the conversation"))
		return start_session(script, message)

	expiry_reason = get_session_expiry_reason(session, script)
	if expiry_reason:
		end_session(session, "Expired", expiry_reason)
		return start_matching_session(message, fallback=session)

	record_session_activity(session, message)
	transition = find_matching_transition(script, session.current_step, message)
	if transition:
		context = parse_context(session.context_json)
		if transition.capture_as:
			context[transition.capture_as] = message.message or ""
		session.context_json = json.dumps(context)
		execute_from_step(script, session, transition.destination_step, message)
		return session

	return apply_unmatched_policy(script, session, message)


def start_matching_session(message, fallback=None):
	script = find_entry_script(message)
	if not script:
		return fallback
	return start_session(script, message)


def start_session(script, message):
	"""Create one active session, recovering the winner of a concurrent insert."""
	savepoint = "whatsapp_conversation_session_create"
	frappe.db.savepoint(savepoint)
	try:
		session = frappe.get_doc(
			{
				"doctype": "WhatsApp Conversation Session",
				"conversation_script": script.name,
				"status": "Active",
				"whatsapp_account": message.whatsapp_account,
				"phone_number": normalize_phone_number(message.get("from")),
				"context_json": "{}",
				"started_on": now_datetime(),
				"last_activity_on": now_datetime(),
			}
		).insert(ignore_permissions=True)
		frappe.db.release_savepoint(savepoint)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		frappe.db.rollback(save_point=savepoint)
		# A locking/current read sees the transaction that won the unique-key race,
		# even when this transaction established an older repeatable-read snapshot.
		session = get_active_session(
			message.whatsapp_account,
			message.get("from"),
			current_read=True,
		)
		if not session:
			raise
		return process_active_session(message, session)

	record_session_activity(session, message)
	execute_from_step(script, session, script.start_step, message)
	return session


def apply_unmatched_policy(script, session, message):
	policy = script.unmatched_message_policy or "Retry"
	if policy == "Restart":
		end_session(session, "Closed", _("Restarted after an unmatched message"))
		return start_session(script, message)
	if policy == "Handoff":
		end_session(session, "Handed Off", _("Unmatched message required handoff"))
	elif policy == "Close":
		end_session(session, "Closed", _("Closed after an unmatched message"))
	else:
		session.save(ignore_permissions=True)
	return session


def record_session_activity(session, message):
	session.last_activity_on = now_datetime()
	session.last_incoming_message = message.name
	session.message_count = (session.message_count or 0) + 1


def end_session(session, status, reason):
	session.status = status
	session.ended_reason = reason
	session.save(ignore_permissions=True)


def matches_keyword(text, configured_keywords):
	value = (text or "").strip().casefold()
	keywords = {
		line.strip().casefold()
		for line in (configured_keywords or "").splitlines()
		if line.strip()
	}
	return bool(value and value in keywords)


def get_session_expiry_reason(session, script, at_time=None):
	now = get_datetime(at_time or now_datetime())
	if script.idle_timeout_minutes and session.last_activity_on:
		idle_seconds = (now - get_datetime(session.last_activity_on)).total_seconds()
		if idle_seconds >= script.idle_timeout_minutes * 60:
			return _("Idle timeout reached")
	if script.maximum_lifetime_minutes and session.started_on:
		lifetime_seconds = (now - get_datetime(session.started_on)).total_seconds()
		if lifetime_seconds >= script.maximum_lifetime_minutes * 60:
			return _("Maximum session lifetime reached")
	if script.maximum_message_count and (session.message_count or 0) >= script.maximum_message_count:
		return _("Maximum session message count reached")
	return None


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
	for execution_sequence in range(MAX_AUTOMATIC_STEPS):
		step = steps.get(current_id)
		if not step:
			frappe.throw(_("Conversation step does not exist: {0}").format(current_id))
		session.current_step = current_id
		session.last_activity_on = now_datetime()
		context = parse_context(session.context_json)

		if step.action_type == "Send Text":
			send_message(
				session,
				"text",
				render_text(step.message, context),
				incoming_message=incoming_message,
				step_id=current_id,
				sequence=execution_sequence,
			)
		elif step.action_type == "Send Options":
			send_message(
				session,
				"interactive",
				render_text(step.message, context),
				incoming_message=incoming_message,
				step_id=current_id,
				sequence=execution_sequence,
				buttons=step.options or "[]",
			)
		elif step.action_type == "Launch Flow":
			send_message(
				session,
				"flow",
				render_text(step.message, context),
				incoming_message=incoming_message,
				step_id=current_id,
				sequence=execution_sequence,
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


def send_message(
	session,
	content_type,
	message,
	*,
	incoming_message,
	step_id,
	sequence,
	**values,
):
	"""Persist a deferred outgoing message and durable execution record."""
	if not incoming_message:
		frappe.throw(_("Conversation sends require an originating incoming message"))
	execution_key = hashlib.sha256(
		f"{incoming_message.name}|{session.name}|{step_id}|{sequence}".encode()
	).hexdigest()
	existing = frappe.db.get_value(
		"WhatsApp Conversation Outbox",
		{"execution_key": execution_key},
		["name", "outgoing_message"],
		as_dict=True,
	)
	if existing:
		return frappe.get_doc("WhatsApp Message", existing.outgoing_message)

	payload = {
		"doctype": "WhatsApp Message",
		"type": "Outgoing",
		"to": session.phone_number,
		"whatsapp_account": session.whatsapp_account,
		"content_type": content_type,
		"message": message or "",
		"conversation_session": session.name,
		"defer_send": 1,
	}
	payload.update(values)
	outgoing = frappe.get_doc(payload).insert(ignore_permissions=True)
	outbox = frappe.get_doc(
		{
			"doctype": "WhatsApp Conversation Outbox",
			"execution_key": execution_key,
			"status": "Pending",
			"conversation_session": session.name,
			"incoming_message": incoming_message.name,
			"step_id": step_id,
			"outgoing_message": outgoing.name,
		}
	).insert(ignore_permissions=True)
	frappe.enqueue(
		"frappe_whatsapp.conversation.deliver_outbox",
		outbox_name=outbox.name,
		queue="short",
		enqueue_after_commit=True,
		job_id=f"whatsapp-conversation-outbox-{outbox.name}",
		deduplicate=True,
	)
	return outgoing


def deliver_outbox(outbox_name):
	"""Claim one durable outgoing action and contact Meta at most once automatically."""
	outbox = frappe.get_doc("WhatsApp Conversation Outbox", outbox_name, for_update=True)
	if outbox.status != "Pending":
		return outbox.status

	outbox.status = "Sending"
	outbox.claimed_on = now_datetime()
	outbox.attempts = (outbox.attempts or 0) + 1
	outbox.save(ignore_permissions=True)
	frappe.db.set_value(
		"WhatsApp Message",
		outbox.outgoing_message,
		"status",
		"Sending",
		update_modified=False,
	)
	# The claim must survive a worker crash after Meta accepts the request.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durable pre-send claim prevents automatic replay

	try:
		outgoing = frappe.get_doc("WhatsApp Message", outbox.outgoing_message)
		outgoing.defer_send = 0
		outgoing.send_outgoing()
		frappe.db.set_value(
			"WhatsApp Message",
			outgoing.name,
			{"message_id": outgoing.message_id, "status": outgoing.status},
			update_modified=False,
		)
		frappe.db.set_value(
			"WhatsApp Conversation Outbox",
			outbox.name,
			{
				"status": "Sent",
				"sent_on": now_datetime(),
				"last_error": None,
			},
			update_modified=False,
		)
		return "Sent"
	except Exception as exc:
		# Delivery outcome may be ambiguous after any network exception. Never
		# auto-replay it; an operator can explicitly accept that risk and retry.
		frappe.db.rollback()
		frappe.db.set_value(
			"WhatsApp Conversation Outbox",
			outbox.name,
			{
				"status": "Unknown",
				"last_error": type(exc).__name__,
			},
			update_modified=False,
		)
		frappe.db.set_value(
			"WhatsApp Message",
			outbox.outgoing_message,
			"status",
			"Unknown",
			update_modified=False,
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- preserve ambiguous outcome before worker exits
		frappe.log_error(
			message=f"outbox={outbox.name}; outgoing_message={outbox.outgoing_message}",
			title="WhatsApp Conversation Delivery Outcome Unknown",
		)
		return "Unknown"


def enqueue_pending_outbox():
	"""Recover durable Pending records if their original enqueue was interrupted."""
	for outbox_name in frappe.get_all(
		"WhatsApp Conversation Outbox",
		filters={"status": "Pending"},
		pluck="name",
		order_by="creation asc",
		limit=100,
	):
		frappe.enqueue(
			"frappe_whatsapp.conversation.deliver_outbox",
			outbox_name=outbox_name,
			queue="short",
			job_id=f"whatsapp-conversation-outbox-{outbox_name}",
			deduplicate=True,
		)


def expire_stale_sessions():
	"""Close active sessions whose configured idle or lifetime limit elapsed."""
	scripts = {}
	for session_name in frappe.get_all(
		"WhatsApp Conversation Session",
		filters={"status": "Active"},
		pluck="name",
		order_by="last_activity_on asc",
	):
		session = frappe.get_doc("WhatsApp Conversation Session", session_name, for_update=True)
		if session.status != "Active":
			continue
		if session.conversation_script not in scripts:
			scripts[session.conversation_script] = frappe.get_doc(
				"WhatsApp Conversation Script", session.conversation_script
			)
		reason = get_session_expiry_reason(session, scripts[session.conversation_script])
		if reason:
			end_session(session, "Expired", reason)


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
