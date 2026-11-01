# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime, now_datetime

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_messaging_window.whatsapp_messaging_window import (
	MessagingWindowClosedError,
	assert_free_form_message_allowed,
	get_messaging_window,
)


class TestWhatsAppMessagingWindow(FrappeTestCase):
	account = "Messaging Window Test Account"
	phone = "15555550104"

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
					"phone_id": "messaging_window_test_phone",
					"business_id": "messaging_window_test_waba",
					"token": "test-token",
				}
			).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.delete("WhatsApp Message", {"whatsapp_account": self.account})
		frappe.db.delete("WhatsApp Messaging Window", {"whatsapp_account": self.account})
		super().tearDown()

	def make_incoming(self, message_timestamp=None):
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"from": self.phone,
				"message": "Hello",
				"message_id": "wamid.window-open",
				"message_timestamp": message_timestamp,
				"content_type": "text",
				"whatsapp_account": self.account,
			}
		).insert(ignore_permissions=True)

	def test_incoming_message_opens_window_and_free_form_send_is_allowed(self):
		incoming = self.make_incoming()
		window = get_messaging_window(self.account, self.phone)
		self.assertIsNotNone(window)
		self.assertEqual(window.last_incoming_message, incoming.name)
		self.assertEqual(window.status, "Open")
		self.assertAlmostEqual(
			(
				get_datetime(window.service_window_expires_on)
				- get_datetime(window.last_customer_message_on)
			).total_seconds(),
			24 * 60 * 60,
			delta=1,
		)

		with patch(
			"frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.make_post_request",
			return_value={"messages": [{"id": "wamid.window-outgoing"}]},
		) as post:
			outgoing = frappe.get_doc(
				{
					"doctype": "WhatsApp Message",
					"type": "Outgoing",
					"to": self.phone,
					"message": "Allowed reply",
					"content_type": "text",
					"whatsapp_account": self.account,
				}
			).insert(ignore_permissions=True)
		self.assertEqual(outgoing.status, "Success")
		post.assert_called_once()

	def test_closed_window_blocks_before_contacting_meta(self):
		self.make_incoming()
		window = get_messaging_window(self.account, self.phone)
		frappe.db.set_value(
			"WhatsApp Messaging Window",
			window.name,
			"service_window_expires_on",
			now_datetime() - timedelta(seconds=1),
		)

		with patch(
			"frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.make_post_request"
		) as post, self.assertRaises(MessagingWindowClosedError):
			frappe.get_doc(
				{
					"doctype": "WhatsApp Message",
					"type": "Outgoing",
					"to": self.phone,
					"message": "Too late",
					"content_type": "text",
					"whatsapp_account": self.account,
				}
			).insert(ignore_permissions=True)
		post.assert_not_called()

	def test_delayed_webhook_does_not_reopen_an_expired_meta_window(self):
		meta_received_on = now_datetime() - timedelta(hours=25)
		self.make_incoming(message_timestamp=meta_received_on)
		window = get_messaging_window(self.account, self.phone)
		self.assertEqual(window.status, "Closed")
		self.assertEqual(
			get_datetime(window.last_customer_message_on),
			get_datetime(meta_received_on),
		)
		with self.assertRaises(MessagingWindowClosedError):
			assert_free_form_message_allowed(self.account, self.phone)

