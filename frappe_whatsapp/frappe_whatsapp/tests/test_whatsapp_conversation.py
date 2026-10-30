# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestWhatsAppConversation(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("WhatsApp Account", "Conversation Test Account"):
			frappe.get_doc(
				{
					"doctype": "WhatsApp Account",
					"account_name": "Conversation Test Account",
					"url": "https://graph.facebook.com",
					"version": "v18.0",
					"phone_id": "conversation_test_phone_id",
					"business_id": "conversation_test_waba",
					"token": "test-token",
				}
			).insert(ignore_permissions=True)

	def make_script(self, name="Conversation Test Script"):
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Conversation Script",
				"script_name": name,
				"enabled": 1,
				"priority": 10,
				"whatsapp_account": "Conversation Test Account",
				"authoring_mode": "Structured",
				"version": 1,
				"availability_mode": "All Phone Numbers in WABA",
				"entry_match_type": "Exact",
				"entry_value": "start",
				"start_step": "MENU",
				"steps": [
					{
						"step_id": "MENU",
						"action_type": "Send Options",
						"message": "Choose an action",
						"options": json.dumps([{"id": "continue", "title": "Continue"}]),
						"wait_for_reply": 1,
					},
					{
						"step_id": "ASK_QUANTITY",
						"action_type": "Send Text",
						"message": "What quantity?",
						"wait_for_reply": 1,
					},
					{
						"step_id": "DONE",
						"action_type": "End",
						"wait_for_reply": 0,
					},
				],
				"transitions": [
					{
						"source_step": "MENU",
						"match_type": "Exact",
						"match_value": "continue",
						"destination_step": "ASK_QUANTITY",
						"priority": 10,
						"capture_as": "intent",
					},
					{
						"source_step": "ASK_QUANTITY",
						"match_type": "Regex",
						"match_value": "^[0-9]+$",
						"destination_step": "DONE",
						"priority": 10,
						"capture_as": "quantity",
					},
				],
			}
		)

	def make_incoming(self, text, message_id):
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"from": "15555550101",
				"message": text,
				"message_id": message_id,
				"content_type": "text",
				"whatsapp_account": "Conversation Test Account",
			}
		).insert(ignore_permissions=True)

	def test_processes_a_stateful_branched_conversation(self):
		from frappe_whatsapp import conversation

		script = self.make_script()
		script.insert(ignore_permissions=True)
		sent = []

		def record_send(session, content_type, message, **values):
			sent.append((content_type, message, values))
			return frappe._dict(name=f"sent-{len(sent)}")

		with patch.object(conversation, "send_message", side_effect=record_send):
			start = self.make_incoming("start", "conversation-start")
			session_name = conversation.process_incoming_message(start.name)
			session = frappe.get_doc("WhatsApp Conversation Session", session_name)
			self.assertEqual(session.current_step, "MENU")
			self.assertEqual(sent[0][0], "interactive")

			choice = self.make_incoming("continue", "conversation-choice")
			conversation.process_incoming_message(choice.name)
			session.reload()
			self.assertEqual(session.current_step, "ASK_QUANTITY")
			self.assertEqual(json.loads(session.context_json)["intent"], "continue")

			quantity = self.make_incoming("25", "conversation-quantity")
			conversation.process_incoming_message(quantity.name)
			session.reload()
			self.assertEqual(session.status, "Completed")
			self.assertEqual(json.loads(session.context_json)["quantity"], "25")
			self.assertEqual(quantity.reload().conversation_session, session.name)
			self.assertEqual(quantity.conversation_processed, 1)

	def test_processing_is_idempotent(self):
		from frappe_whatsapp import conversation

		self.make_script("Conversation Idempotency Test").insert(ignore_permissions=True)
		message = self.make_incoming("start", "conversation-idempotent")
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")) as send:
			first = conversation.process_incoming_message(message.name)
			second = conversation.process_incoming_message(message.name)

		self.assertEqual(first, second)
		send.assert_called_once()

	def test_json_and_tables_synchronize_with_json_priority(self):
		script = self.make_script("Conversation Authoring Sync")
		script.insert(ignore_permissions=True)

		script.steps[0].message = "Changed in tables"
		script.save(ignore_permissions=True)
		self.assertEqual(
			json.loads(script.script_json)["steps"][0]["message"],
			"Changed in tables",
		)

		script.steps[0].message = "Conflicting table value"
		value = json.loads(script.script_json)
		value["steps"][0]["message"] = "JSON wins"
		script.script_json = json.dumps(value)
		script.save(ignore_permissions=True)
		self.assertEqual(script.steps[0].message, "JSON wins")

	def test_handler_dispatch_requires_one_allowlisted_method(self):
		from frappe_whatsapp.conversation import dispatch_handler

		script = frappe._dict(name="Script")
		session = frappe._dict(
			name="Session",
			whatsapp_account="Account",
			phone_number="9199",
			current_step="ACTION",
		)
		handler = MagicMock(return_value={"context": {"created": True}})
		with patch("frappe.get_hooks", return_value={"update_record": ["app.handler"]}), patch(
			"frappe.get_attr", return_value=handler
		):
			result = dispatch_handler("update_record", script, session, None, {})

		self.assertEqual(result, {"context": {"created": True}})
		handler.assert_called_once()
