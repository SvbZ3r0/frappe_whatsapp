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
		for flow_name in frappe.get_all(
			"WhatsApp Flow",
			filters={"name": ["like", "Conversation%"]},
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Flow", flow_name, force=True, ignore_permissions=True)

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

	def make_incoming(self, text, message_id, phone_number="15555550101"):
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"from": phone_number,
				"message": text,
				"message_id": message_id,
				"content_type": "text",
				"whatsapp_account": "Conversation Test Account",
			}
		).insert(ignore_permissions=True)

	def make_flow(self, name="Conversation Test Flow"):
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Flow",
				"flow_name": name,
				"whatsapp_account": "Conversation Test Account",
				"category": "OTHER",
				"flow_json_version": "7.3",
				"data_api_version": "3.0",
				"screens": [
					{
						"screen_id": "WELCOME",
						"screen_title": "Welcome",
						"terminal": 1,
					}
				],
				"fields": [
					{
						"screen": "WELCOME",
						"field_name": "heading",
						"field_type": "TextHeading",
						"label": "Welcome",
						"enabled": 1,
					}
				],
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

	def test_launches_flow_with_configured_action(self):
		from frappe_whatsapp import conversation

		flow = self.make_flow()
		script = self.make_script("Conversation Flow Launch Test")
		script.start_step = "FLOW"
		script.set(
			"steps",
			[
				{
					"step_id": "FLOW",
					"action_type": "Launch Flow",
					"message": "Complete this form",
					"flow": flow.name,
					"flow_action": "data_exchange",
					"wait_for_reply": 1,
				},
				{"step_id": "DONE", "action_type": "End", "wait_for_reply": 0},
			],
		)
		script.set(
			"transitions",
			[
				{
					"source_step": "FLOW",
					"match_type": "Any",
					"destination_step": "DONE",
					"priority": 10,
				}
			],
		)
		script.insert(ignore_permissions=True)

		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")) as send:
			message = self.make_incoming("start", "conversation-flow-launch")
			conversation.process_incoming_message(message.name)

		self.assertEqual(send.call_args.args[1:3], ("flow", "Complete this form"))
		self.assertEqual(send.call_args.kwargs["flow"], flow.name)
		self.assertEqual(send.call_args.kwargs["flow_action"], "data_exchange")

	def test_handoff_step_ends_automation(self):
		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Step Handoff Test")
		script.start_step = "HANDOFF"
		script.set(
			"steps",
			[{"step_id": "HANDOFF", "action_type": "Handoff", "wait_for_reply": 0}],
		)
		script.set("transitions", [])
		script.insert(ignore_permissions=True)

		message = self.make_incoming("start", "conversation-step-handoff")
		session_name = conversation.process_incoming_message(message.name)

		session = frappe.get_doc("WhatsApp Conversation Session", session_name)
		self.assertEqual(session.status, "Handed Off")
		self.assertEqual(session.last_incoming_message, message.name)

	def test_conflicting_entry_scripts_use_lowest_priority(self):
		from frappe_whatsapp import conversation

		slower = self.make_script("Conversation Conflict Priority 20")
		slower.priority = 20
		slower.insert(ignore_permissions=True)
		winner = self.make_script("Conversation Conflict Priority 5")
		winner.priority = 5
		winner.insert(ignore_permissions=True)

		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			message = self.make_incoming("start", "conversation-conflicting-entry")
			session_name = conversation.process_incoming_message(message.name)

		self.assertEqual(
			frappe.db.get_value("WhatsApp Conversation Session", session_name, "conversation_script"),
			winner.name,
		)

	def test_restart_and_cancel_keywords_control_the_session(self):
		from frappe_whatsapp import conversation

		self.make_script("Conversation Command Test").insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			first_message = self.make_incoming("start", "conversation-command-start")
			first_name = conversation.process_incoming_message(first_message.name)

			restart = self.make_incoming("START OVER", "conversation-command-restart")
			second_name = conversation.process_incoming_message(restart.name)

			self.assertNotEqual(first_name, second_name)
			first = frappe.get_doc("WhatsApp Conversation Session", first_name)
			second = frappe.get_doc("WhatsApp Conversation Session", second_name)
			self.assertEqual(first.status, "Closed")
			self.assertIn("restarted", first.ended_reason.lower())
			self.assertEqual(second.status, "Active")
			self.assertEqual(second.message_count, 1)

			cancel = self.make_incoming("cancel", "conversation-command-cancel")
			self.assertEqual(conversation.process_incoming_message(cancel.name), second_name)

		second.reload()
		self.assertEqual(second.status, "Cancelled")
		self.assertEqual(second.message_count, 2)

	def test_unmatched_policy_can_handoff_instead_of_absorbing_messages(self):
		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Unmatched Handoff Test")
		script.unmatched_message_policy = "Handoff"
		script.insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			start = self.make_incoming("start", "conversation-handoff-start")
			session_name = conversation.process_incoming_message(start.name)
			unmatched = self.make_incoming("not a valid choice", "conversation-handoff-unmatched")
			conversation.process_incoming_message(unmatched.name)

		session = frappe.get_doc("WhatsApp Conversation Session", session_name)
		self.assertEqual(session.status, "Handed Off")
		self.assertIn("unmatched", session.ended_reason.lower())
		self.assertEqual(unmatched.reload().conversation_session, session_name)

	def test_unmatched_retry_is_bounded_and_records_activity(self):
		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Unmatched Retry Test")
		script.insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			start = self.make_incoming("start", "conversation-retry-start")
			session_name = conversation.process_incoming_message(start.name)
			unmatched = self.make_incoming("not valid", "conversation-retry-unmatched")
			conversation.process_incoming_message(unmatched.name)

		session = frappe.get_doc("WhatsApp Conversation Session", session_name)
		self.assertEqual(session.status, "Active")
		self.assertEqual(session.message_count, 2)
		self.assertEqual(session.last_incoming_message, unmatched.name)
		self.assertGreater(script.maximum_lifetime_minutes, 0)
		self.assertGreater(script.maximum_message_count, 0)

	def test_unmatched_policy_can_restart_the_session(self):
		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Unmatched Restart Test")
		script.unmatched_message_policy = "Restart"
		script.insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			start = self.make_incoming("start", "conversation-policy-restart-start")
			first_name = conversation.process_incoming_message(start.name)
			unmatched = self.make_incoming("try again", "conversation-policy-restart-unmatched")
			second_name = conversation.process_incoming_message(unmatched.name)

		self.assertNotEqual(first_name, second_name)
		self.assertEqual(frappe.db.get_value("WhatsApp Conversation Session", first_name, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("WhatsApp Conversation Session", second_name, "status"), "Active")

	def test_unmatched_policy_can_close_the_session(self):
		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Unmatched Close Test")
		script.unmatched_message_policy = "Close"
		script.insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			start = self.make_incoming("start", "conversation-policy-close-start")
			session_name = conversation.process_incoming_message(start.name)
			unmatched = self.make_incoming("invalid", "conversation-policy-close-unmatched")
			conversation.process_incoming_message(unmatched.name)

		self.assertEqual(frappe.db.get_value("WhatsApp Conversation Session", session_name, "status"), "Closed")

	def test_idle_session_expires_before_it_can_consume_another_answer(self):
		from frappe.utils import add_to_date, now_datetime

		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Idle Expiry Test")
		script.idle_timeout_minutes = 1
		script.insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			start = self.make_incoming("start", "conversation-expiry-start")
			session_name = conversation.process_incoming_message(start.name)
			frappe.db.set_value(
				"WhatsApp Conversation Session",
				session_name,
				"last_activity_on",
				add_to_date(now_datetime(), minutes=-2),
				update_modified=False,
			)
			late_answer = self.make_incoming("continue", "conversation-expiry-answer")
			conversation.process_incoming_message(late_answer.name)

		session = frappe.get_doc("WhatsApp Conversation Session", session_name)
		self.assertEqual(session.status, "Expired")
		self.assertIn("idle", session.ended_reason.lower())
		self.assertEqual(session.message_count, 1)

	def test_active_session_key_is_unique_for_normalized_phone_number(self):
		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Unique Session Test")
		script.insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			start = self.make_incoming("start", "conversation-unique-start")
			conversation.process_incoming_message(start.name)

		duplicate = frappe.get_doc(
			{
				"doctype": "WhatsApp Conversation Session",
				"conversation_script": script.name,
				"status": "Active",
				"whatsapp_account": "Conversation Test Account",
				"phone_number": "+1 555 555 0101",
				"context_json": "{}",
			}
		)
		with self.assertRaises((frappe.DuplicateEntryError, frappe.UniqueValidationError)):
			duplicate.insert(ignore_permissions=True)

	def test_concurrent_session_loser_recovers_with_current_read(self):
		from frappe_whatsapp import conversation

		candidate = MagicMock()
		candidate.insert.side_effect = frappe.UniqueValidationError
		winner = frappe._dict(name="winning-session")
		processed = frappe._dict(name="processed-winning-session")
		message = frappe._dict(
			name="incoming-race",
			whatsapp_account="Conversation Test Account",
			**{"from": "+1 555 555 0101"},
		)
		script = frappe._dict(name="Conversation Race Test", start_step="MENU")

		with patch("frappe.get_doc", return_value=candidate), patch.object(
			frappe.db, "savepoint"
		), patch.object(frappe.db, "rollback") as rollback, patch.object(
			conversation, "get_active_session", return_value=winner
		) as get_active, patch.object(
			conversation, "process_active_session", return_value=processed
		) as process_active:
			result = conversation.start_session(script, message)

		rollback.assert_called_once_with(save_point="whatsapp_conversation_session_create")
		get_active.assert_called_once_with(
			"Conversation Test Account",
			"+1 555 555 0101",
			current_read=True,
		)
		process_active.assert_called_once_with(message, winner)
		self.assertEqual(result, processed)

	def test_message_limit_expires_session(self):
		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Message Limit Test")
		script.maximum_message_count = 1
		script.insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			start = self.make_incoming("start", "conversation-limit-start")
			session_name = conversation.process_incoming_message(start.name)
			answer = self.make_incoming("continue", "conversation-limit-answer")
			conversation.process_incoming_message(answer.name)

		session = frappe.get_doc("WhatsApp Conversation Session", session_name)
		self.assertEqual(session.status, "Expired")
		self.assertIn("message count", session.ended_reason.lower())

	def test_maximum_lifetime_expires_session(self):
		from frappe.utils import add_to_date, now_datetime

		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Lifetime Test")
		script.idle_timeout_minutes = 0
		script.maximum_lifetime_minutes = 1
		script.insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			start = self.make_incoming("start", "conversation-lifetime-start")
			session_name = conversation.process_incoming_message(start.name)
			frappe.db.set_value(
				"WhatsApp Conversation Session",
				session_name,
				"started_on",
				add_to_date(now_datetime(), minutes=-2),
				update_modified=False,
			)
			answer = self.make_incoming("continue", "conversation-lifetime-answer")
			conversation.process_incoming_message(answer.name)

		session = frappe.get_doc("WhatsApp Conversation Session", session_name)
		self.assertEqual(session.status, "Expired")
		self.assertIn("lifetime", session.ended_reason.lower())

	def test_idle_expiry_sweeper_closes_abandoned_sessions(self):
		from frappe.utils import add_to_date, now_datetime

		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Expiry Sweep Test")
		script.idle_timeout_minutes = 1
		script.insert(ignore_permissions=True)
		with patch.object(conversation, "send_message", return_value=frappe._dict(name="sent")):
			start = self.make_incoming("start", "conversation-sweep-start")
			session_name = conversation.process_incoming_message(start.name)
		frappe.db.set_value(
			"WhatsApp Conversation Session",
			session_name,
			"last_activity_on",
			add_to_date(now_datetime(), minutes=-2),
			update_modified=False,
		)

		conversation.expire_stale_sessions()

		session = frappe.get_doc("WhatsApp Conversation Session", session_name)
		self.assertEqual(session.status, "Expired")

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

	def test_flow_delivery_persists_generated_token_for_endpoint_correlation(self):
		from frappe_whatsapp import conversation
		from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import (
			WhatsAppMessage,
		)
		from frappe_whatsapp.utils.webhook import get_outgoing_flow_message

		flow = self.make_flow("Conversation Deferred Token Flow")
		script = self.make_script("Conversation Deferred Flow Token Test")
		script.start_step = "FLOW"
		script.set(
			"steps",
			[
				{
					"step_id": "FLOW",
					"action_type": "Launch Flow",
					"message": "Complete this form",
					"flow": flow.name,
					"flow_action": "data_exchange",
					"wait_for_reply": 1,
				},
				{"step_id": "DONE", "action_type": "End", "wait_for_reply": 0},
			],
		)
		script.set(
			"transitions",
			[
				{
					"source_step": "FLOW",
					"match_type": "Any",
					"destination_step": "DONE",
					"priority": 10,
				}
			],
		)
		script.insert(ignore_permissions=True)
		message = self.make_incoming("start", "conversation-flow-token")
		with patch("frappe.enqueue"):
			conversation.process_incoming_message(message.name)
		outbox = frappe.get_last_doc("WhatsApp Conversation Outbox")

		def accept_flow_at_meta(outgoing):
			outgoing.message_id = "wamid.conversation-flow-token"
			outgoing.flow_token = "generated-flow-token"
			outgoing.status = "Success"

		with patch.object(
			WhatsAppMessage,
			"send_outgoing",
			autospec=True,
			side_effect=accept_flow_at_meta,
		), patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
			self.assertEqual(conversation.deliver_outbox(outbox.name), "Sent")

		outgoing = frappe.get_doc("WhatsApp Message", outbox.outgoing_message)
		self.assertEqual(outgoing.flow_token, "generated-flow-token")
		self.assertEqual(outgoing.defer_send, 0)
		origin = get_outgoing_flow_message(
			"generated-flow-token",
			"Conversation Test Account",
		)
		self.assertIsNotNone(origin)
		self.assertEqual(origin.name, outgoing.name)
		self.assertEqual(origin.flow, flow.name)

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
		script.idle_timeout_minutes = 30
		script.save(ignore_permissions=True)
		self.assertEqual(
			json.loads(script.script_json)["steps"][0]["message"],
			"Changed in tables",
		)
		self.assertEqual(json.loads(script.script_json)["session"]["idle_timeout_minutes"], 30)

		script.steps[0].message = "Conflicting table value"
		value = json.loads(script.script_json)
		value["steps"][0]["message"] = "JSON wins"
		value["session"]["unmatched_message_policy"] = "Close"
		script.script_json = json.dumps(value)
		script.save(ignore_permissions=True)
		self.assertEqual(script.steps[0].message, "JSON wins")
		self.assertEqual(script.unmatched_message_policy, "Close")

	def test_option_shape_and_whatsapp_limits_are_validated(self):
		invalid_options = (
			([{"id": "same", "title": "One"}, {"id": "same", "title": "Two"}], "unique"),
			([{"id": "x", "title": "x" * 21}], "title"),
			([{"id": "x" * 257, "title": "Valid"}], "ID"),
			([{"id": "x", "title": "Valid", "description": "Ignored"}], "Descriptions"),
			([{"id": "x", "title": "Valid"}, "not-an-object"], "object"),
		)
		for index, (options, expected) in enumerate(invalid_options):
			with self.subTest(expected=expected):
				script = self.make_script(f"Conversation Invalid Option {index}")
				script.steps[0].options = json.dumps(options)
				with self.assertRaisesRegex(frappe.ValidationError, expected):
					script.insert(ignore_permissions=True)

		list_script = self.make_script("Conversation Invalid List Description")
		list_script.steps[0].options = json.dumps(
			[
				{"id": f"option-{index}", "title": f"Option {index}", "description": "x" * 73}
				for index in range(4)
			]
		)
		with self.assertRaisesRegex(frappe.ValidationError, "description"):
			list_script.insert(ignore_permissions=True)

	def test_message_length_limits_are_validated(self):
		options_script = self.make_script("Conversation Long Interactive Body")
		options_script.steps[0].message = "x" * 1025
		with self.assertRaisesRegex(frappe.ValidationError, "1024"):
			options_script.insert(ignore_permissions=True)

		text_script = self.make_script("Conversation Long Text Body")
		text_script.steps[1].message = "x" * 4097
		with self.assertRaisesRegex(frappe.ValidationError, "4096"):
			text_script.insert(ignore_permissions=True)

	def test_regex_matching_has_length_and_runtime_bounds(self):
		from frappe_whatsapp import conversation

		script = self.make_script("Conversation Long Regex")
		script.transitions[1].match_value = "a" * 513
		with self.assertRaisesRegex(frappe.ValidationError, "512"):
			script.insert(ignore_permissions=True)

		message = frappe._dict(message="a" * 2000 + "!", content_type="text")
		self.assertFalse(conversation.message_matches(message, "Regex", r"^(a+)+$"))
		with patch.object(conversation.safe_regex, "search", side_effect=TimeoutError) as search:
			self.assertFalse(conversation.message_matches(message, "Regex", r"^a+$"))
		self.assertEqual(search.call_args.kwargs["timeout"], 0.05)

	def test_handler_dispatch_requires_one_allowlisted_method(self):
		from frappe_whatsapp.conversation import dispatch_handler

		script = frappe._dict(name="Script", steps=[frappe._dict(step_id="NEXT")])
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

	def test_handler_response_contract_is_validated(self):
		from frappe_whatsapp.conversation import dispatch_handler

		script = frappe._dict(
			name="Script",
			steps=[frappe._dict(step_id="ACTION"), frappe._dict(step_id="NEXT")],
		)
		session = frappe._dict(
			name="Session",
			whatsapp_account="Account",
			phone_number="9199",
			current_step="ACTION",
		)
		invalid_results = (
			({"context": []}, "context must be a dictionary"),
			({"context": {"value": object()}}, "JSON serializable"),
			({"next_step": "MISSING"}, "unknown next step"),
			({"status": "Active"}, "invalid terminal status"),
			({"status": "Completed", "next_step": "NEXT"}, "both status and next_step"),
			({"typo": True}, "unsupported keys"),
		)
		for result, expected in invalid_results:
			with self.subTest(expected=expected), patch(
				"frappe.get_hooks", return_value={"action": ["app.handler"]}
			), patch("frappe.get_attr", return_value=MagicMock(return_value=result)):
				with self.assertRaisesRegex(frappe.ValidationError, expected):
					dispatch_handler("action", script, session, None, {})

		for result in (
			{"next_step": "NEXT"},
			{"status": "Completed"},
			{"context": {"ok": True}},
		):
			with self.subTest(valid=result), patch(
				"frappe.get_hooks", return_value={"action": ["app.handler"]}
			), patch("frappe.get_attr", return_value=MagicMock(return_value=result)):
				self.assertEqual(dispatch_handler("action", script, session, None, {}), result)
