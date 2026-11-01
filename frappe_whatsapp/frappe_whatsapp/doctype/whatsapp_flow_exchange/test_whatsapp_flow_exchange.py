# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow_exchange.whatsapp_flow_exchange import (
	begin_exchange,
	complete_exchange,
)
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow_session.whatsapp_flow_session import (
	ensure_flow_session_for_launch,
)


class TestWhatsAppFlowExchange(FrappeTestCase):
	account = "Flow Exchange Test Account"
	flow_name = "Flow Exchange Test Flow"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("WhatsApp Account", cls.account):
			frappe.get_doc(
				{
					"doctype": "WhatsApp Account",
					"account_name": cls.account,
					"url": "https://graph.facebook.com",
					"version": "v18.0",
					"phone_id": "flow_exchange_test_phone",
					"business_id": "flow_exchange_test_waba",
					"token": "test-token",
				}
			).insert(ignore_permissions=True)

	def setUp(self):
		if not frappe.db.exists("WhatsApp Flow", self.flow_name):
			frappe.get_doc(
				{
					"doctype": "WhatsApp Flow",
					"flow_name": self.flow_name,
					"whatsapp_account": self.account,
					"category": "OTHER",
					"handler_key": "exchange_test",
					"flow_json_version": "7.3",
					"data_api_version": "3.0",
					"screens": [{"screen_id": "START", "screen_title": "Start", "terminal": 1}],
				}
			).insert(ignore_permissions=True)

	def tearDown(self):
		sessions = frappe.get_all(
			"WhatsApp Flow Session",
			filters={"whatsapp_account": self.account},
			pluck="name",
		)
		if sessions:
			frappe.db.delete("WhatsApp Flow Exchange", {"flow_session": ("in", sessions)})
		frappe.db.delete("WhatsApp Flow Session", {"whatsapp_account": self.account})
		frappe.db.delete("WhatsApp Message", {"whatsapp_account": self.account})
		if frappe.db.exists("WhatsApp Flow", self.flow_name):
			frappe.delete_doc("WhatsApp Flow", self.flow_name, force=True, ignore_permissions=True)
		super().tearDown()

	def make_launch(self):
		message = frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Outgoing",
				"to": "15555550105",
				"message": "Open",
				"content_type": "flow",
				"flow": self.flow_name,
				"flow_token": "flow-exchange-token",
				"whatsapp_account": self.account,
			}
		)
		message.flags.ignore_validate = True
		message.db_insert()
		ensure_flow_session_for_launch(message)
		return message

	def test_exact_retry_reuses_completed_handler_response(self):
		self.make_launch()
		flow = frappe.get_doc("WhatsApp Flow", self.flow_name)
		context = {
			"flow": self.flow_name,
			"whatsapp_account": self.account,
			"action": "data_exchange",
			"screen": "START",
			"flow_token": "flow-exchange-token",
			"data": {"private_value": "not-copied"},
		}
		session, exchange, cached = begin_exchange(context, flow, "request-sha256")
		self.assertIsNone(cached)
		response = {"version": "3.0", "screen": "DONE", "data": {"ok": True}}
		complete_exchange(session, exchange, response)

		session, replay, cached = begin_exchange(context, flow, "request-sha256")
		self.assertEqual(replay.name, exchange.name)
		self.assertEqual(cached, response)
		self.assertFalse(replay.meta.has_field("request_json"))
		self.assertNotIn("not-copied", replay.as_json())
		session.reload()
		self.assertEqual(session.exchange_count, 1)
		self.assertEqual(session.current_screen, "DONE")

