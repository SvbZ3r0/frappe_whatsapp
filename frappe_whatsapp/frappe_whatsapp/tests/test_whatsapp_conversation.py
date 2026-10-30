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

	def setUp(self):
		self.cleanup_conversation_data()

	def tearDown(self):
		self.cleanup_conversation_data()
		super().tearDown()

	def cleanup_conversation_data(self):
		sessions = frappe.get_all(
			"WhatsApp Conversation Session",
			filters={"whatsapp_account": "Conversation Test Account"},
			pluck="name",
		)
		if sessions:
			frappe.db.delete(
				"WhatsApp Conversation Outbox",
				{"conversation_session": ["in", sessions]},
			)
			frappe.db.delete(
				"WhatsApp Message",
				{"conversation_session": ["in", sessions]},
			)
		frappe.db.delete(
			"WhatsApp Message",
			{"message_id": ["like", "conversation-%"]},
		)
		frappe.db.delete(
			"WhatsApp Conversation Session",
			{"whatsapp_account": "Conversation Test Account"},
		)
		for script_name in frappe.get_all(
			"WhatsApp Conversation Script",
			filters={"name": ["like", "Conversation%"]},
			pluck="name",
		):
			frappe.delete_doc(
				"WhatsApp Conversation Script",
				script_name,
				force=True,
				ignore_permissions=True,
			)

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

	def test_outgoing_action_is_durable_before_meta_delivery(self):
		from frappe_whatsapp import conversation
		from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import (
			WhatsAppMessage,
		)

		self.make_script("Conversation Durable Outbox Test").insert(ignore_permissions=True)
		message = self.make_incoming("start", "conversation-outbox")
		with patch.object(WhatsAppMessage, "send_outgoing") as direct_send, patch(
			"frappe.enqueue"
		) as enqueue:
			session_name = conversation.process_incoming_message(message.name)

		direct_send.assert_not_called()
		outbox = frappe.get_last_doc("WhatsApp Conversation Outbox")
		outgoing = frappe.get_doc("WhatsApp Message", outbox.outgoing_message)
		self.assertEqual(outbox.status, "Pending")
		self.assertEqual(outbox.conversation_session, session_name)
		self.assertEqual(outbox.incoming_message, message.name)
		self.assertEqual(outgoing.status, "Queued")
		self.assertEqual(outgoing.defer_send, 1)
		enqueue.assert_called_once()

		conversation.process_incoming_message(message.name)
		self.assertEqual(
			frappe.db.count(
				"WhatsApp Conversation Outbox",
				{"incoming_message": message.name},
			),
			1,
		)

	def test_ambiguous_delivery_is_not_automatically_retried(self):
		from frappe_whatsapp import conversation
		from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import (
			WhatsAppMessage,
		)

		self.make_script("Conversation Ambiguous Delivery Test").insert(ignore_permissions=True)
		message = self.make_incoming("start", "conversation-ambiguous")
		with patch("frappe.enqueue"):
			conversation.process_incoming_message(message.name)
		outbox = frappe.get_last_doc("WhatsApp Conversation Outbox")

		with patch.object(
			WhatsAppMessage,
			"send_outgoing",
			side_effect=ConnectionError("response lost"),
		) as send, patch.object(frappe.db, "commit"), patch.object(
			frappe.db, "rollback"
		), patch("frappe.log_error"):
			self.assertEqual(conversation.deliver_outbox(outbox.name), "Unknown")
			self.assertEqual(conversation.deliver_outbox(outbox.name), "Unknown")

		self.assertEqual(send.call_count, 1)
		outbox.reload()
		self.assertEqual(outbox.status, "Unknown")
		self.assertEqual(outbox.attempts, 1)

	def test_successful_outbox_delivery_persists_meta_message_id(self):
		from frappe_whatsapp import conversation
		from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import (
			WhatsAppMessage,
		)

		self.make_script("Conversation Successful Delivery Test").insert(ignore_permissions=True)
		message = self.make_incoming("start", "conversation-delivered")
		with patch("frappe.enqueue"):
			conversation.process_incoming_message(message.name)
		outbox = frappe.get_last_doc("WhatsApp Conversation Outbox")

		def accept_at_meta(outgoing):
			outgoing.message_id = "wamid.conversation-delivered"
			outgoing.status = "Success"

		with patch.object(
			WhatsAppMessage, "send_outgoing", autospec=True, side_effect=accept_at_meta
		), patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
			self.assertEqual(conversation.deliver_outbox(outbox.name), "Sent")

		outbox.reload()
		outgoing = frappe.get_doc("WhatsApp Message", outbox.outgoing_message)
		self.assertEqual(outbox.status, "Sent")
		self.assertEqual(outbox.attempts, 1)
		self.assertEqual(outgoing.status, "Success")
		self.assertEqual(outgoing.message_id, "wamid.conversation-delivered")

	def test_pending_outbox_sweeper_requeues_with_deduplication(self):
		from frappe_whatsapp.conversation import enqueue_pending_outbox

		with patch("frappe.get_all", return_value=["outbox-1", "outbox-2"]), patch(
			"frappe.enqueue"
		) as enqueue:
			enqueue_pending_outbox()

		self.assertEqual(enqueue.call_count, 2)
		self.assertTrue(all(call.kwargs["deduplicate"] for call in enqueue.call_args_list))
		self.assertEqual(
			enqueue.call_args_list[0].kwargs["job_id"],
			"whatsapp-conversation-outbox-outbox-1",
		)

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
