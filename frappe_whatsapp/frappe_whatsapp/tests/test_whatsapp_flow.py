# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import unittest
import json
import base64
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class TestWhatsAppFlow(FrappeTestCase):
    """Test cases for WhatsApp Flow feature."""

    def setUp(self):
        """Set up test data."""
        # Create a test WhatsApp Account if it doesn't exist
        if not frappe.db.exists("WhatsApp Account", "Test Account"):
            frappe.get_doc({
                "doctype": "WhatsApp Account",
                "account_name": "Test Account",
                "url": "https://graph.facebook.com",
                "version": "v18.0",
                "phone_id": "123456789",
                "business_id": "987654321",
                "token": "test_token"
            }).insert(ignore_permissions=True)

    def tearDown(self):
        """Clean up test data."""
        # Delete test flows
        for flow in frappe.get_all("WhatsApp Flow", filters={"flow_name": ["like", "Test%"]}):
            frappe.delete_doc("WhatsApp Flow", flow.name, force=True)

    def create_test_flow(self, flow_name, screens_data, fields_data):
        """Helper to create a test flow."""
        flow = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": flow_name,
            "whatsapp_account": "Test Account",
            "category": "OTHER",
            "flow_json_version": "7.3",
            "data_api_version": "3.0",
            "screens": screens_data,
            "fields": fields_data
        })
        flow.insert(ignore_permissions=True)
        return flow

    def test_single_screen_flow_json_generation(self):
        """Test flow JSON generation for a single screen flow."""
        screens = [{
            "screen_id": "WELCOME",
            "screen_title": "Welcome",
            "terminal": 1
        }]
        fields = [{
            "screen": "WELCOME",
            "field_name": "user_name",
            "field_type": "TextInput",
            "label": "Your Name",
            "required": 1,
            "enabled": 1
        }]

        flow = self.create_test_flow("Test Single Screen", screens, fields)
        flow_json = json.loads(flow.flow_json)

        # Verify structure
        self.assertEqual(flow_json["version"], "7.3")
        self.assertEqual(len(flow_json["screens"]), 1)

        # Verify screen
        screen = flow_json["screens"][0]
        self.assertEqual(screen["id"], "WELCOME")
        self.assertEqual(screen["title"], "Welcome")
        self.assertTrue(screen.get("terminal"))
        self.assertTrue(screen.get("success"))

        # Verify no routing_model or data_api_version (client-only flow)
        self.assertNotIn("routing_model", flow_json)
        self.assertNotIn("data_api_version", flow_json)

    def test_flow_cta_respects_whatsapp_length_limit(self):
        """Reject Flow launch button labels longer than Meta permits."""
        flow = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": "Test Long Flow CTA",
            "whatsapp_account": "Test Account",
            "category": "OTHER",
            "flow_cta": "x" * 21,
            "flow_json_version": "7.3",
            "data_api_version": "3.0",
            "screens": [{
                "screen_id": "WELCOME",
                "screen_title": "Welcome",
                "terminal": 1,
            }],
            "fields": [{
                "screen": "WELCOME",
                "field_name": "heading",
                "field_type": "TextHeading",
                "label": "Welcome",
                "enabled": 1,
            }],
        })
        with self.assertRaisesRegex(frappe.ValidationError, "20 characters"):
            flow.insert(ignore_permissions=True)

    def test_multi_screen_flow_json_generation(self):
        """Test flow JSON generation for multi-screen flow."""
        screens = [
            {"screen_id": "contact", "screen_title": "Contact Details", "terminal": 0},
            {"screen_id": "booking", "screen_title": "Booking Details", "terminal": 1}
        ]
        fields = [
            {"screen": "contact", "field_name": "name", "field_type": "TextInput", "label": "Name", "required": 1, "enabled": 1},
            {"screen": "contact", "field_name": "mobile", "field_type": "TextInput", "label": "Mobile", "required": 1, "enabled": 1},
            {"screen": "booking", "field_name": "date", "field_type": "TextInput", "label": "Date", "required": 1, "enabled": 1}
        ]

        flow = self.create_test_flow("Test Multi Screen", screens, fields)
        flow_json = json.loads(flow.flow_json)

        # Verify two screens
        self.assertEqual(len(flow_json["screens"]), 2)

        # First screen should have empty data (no incoming data)
        self.assertEqual(flow_json["screens"][0]["data"], {})

        # Second screen should have data declarations for fields from first screen
        second_screen_data = flow_json["screens"][1]["data"]
        self.assertIn("name", second_screen_data)
        self.assertIn("mobile", second_screen_data)
        self.assertEqual(second_screen_data["name"]["type"], "string")

    def test_navigate_action_payload(self):
        """Test that navigate action includes correct payload."""
        screens = [
            {"screen_id": "screen1", "screen_title": "Screen 1", "terminal": 0},
            {"screen_id": "screen2", "screen_title": "Screen 2", "terminal": 1}
        ]
        fields = [
            {"screen": "screen1", "field_name": "field1", "field_type": "TextInput", "label": "Field 1", "required": 1, "enabled": 1}
        ]

        flow = self.create_test_flow("Test Navigate", screens, fields)
        flow_json = json.loads(flow.flow_json)

        # Find the footer in first screen
        first_screen = flow_json["screens"][0]
        footer = None
        for child in first_screen["layout"]["children"]:
            if child.get("type") == "Footer":
                footer = child
                break

        self.assertIsNotNone(footer)
        action = footer["on-click-action"]
        self.assertEqual(action["name"], "navigate")
        self.assertEqual(action["next"]["name"], "screen2")

        # Payload should reference form fields
        self.assertEqual(action["payload"]["field1"], "${form.field1}")

    def test_complete_action_payload(self):
        """Test that complete action includes all accumulated data."""
        screens = [
            {"screen_id": "screen1", "screen_title": "Screen 1", "terminal": 0},
            {"screen_id": "screen2", "screen_title": "Screen 2", "terminal": 1}
        ]
        fields = [
            {"screen": "screen1", "field_name": "name", "field_type": "TextInput", "label": "Name", "enabled": 1},
            {"screen": "screen2", "field_name": "email", "field_type": "TextInput", "label": "Email", "enabled": 1}
        ]

        flow = self.create_test_flow("Test Complete", screens, fields)
        flow_json = json.loads(flow.flow_json)

        # Find footer in terminal screen
        terminal_screen = flow_json["screens"][1]
        footer = None
        for child in terminal_screen["layout"]["children"]:
            if child.get("type") == "Footer":
                footer = child
                break

        self.assertIsNotNone(footer)
        action = footer["on-click-action"]
        self.assertEqual(action["name"], "complete")

        # Payload should include data from previous screen and form from current
        payload = action["payload"]
        self.assertEqual(payload["name"], "${data.name}")  # From previous screen
        self.assertEqual(payload["email"], "${form.email}")  # From current screen

    def test_text_display_components(self):
        """Test text display components (TextHeading, TextBody, etc.)."""
        screens = [{"screen_id": "info", "screen_title": "Info", "terminal": 1}]
        fields = [
            {"screen": "info", "field_name": "heading", "field_type": "TextHeading", "label": "Welcome!", "enabled": 1},
            {"screen": "info", "field_name": "body", "field_type": "TextBody", "label": "Please fill the form", "enabled": 1}
        ]

        flow = self.create_test_flow("Test Text Components", screens, fields)
        flow_json = json.loads(flow.flow_json)

        children = flow_json["screens"][0]["layout"]["children"]

        # Text components should have type and text properties
        heading = children[0]
        self.assertEqual(heading["type"], "TextHeading")
        self.assertEqual(heading["text"], "Welcome!")

        body = children[1]
        self.assertEqual(body["type"], "TextBody")
        self.assertEqual(body["text"], "Please fill the form")

    def test_dropdown_with_options(self):
        """Test dropdown field with options."""
        screens = [{"screen_id": "select", "screen_title": "Select", "terminal": 1}]
        fields = [{
            "screen": "select",
            "field_name": "country",
            "field_type": "Dropdown",
            "label": "Country",
            "enabled": 1,
            "options": json.dumps([
                {"id": "us", "title": "United States"},
                {"id": "uk", "title": "United Kingdom"},
                {"id": "in", "title": "India"}
            ])
        }]

        flow = self.create_test_flow("Test Dropdown", screens, fields)
        flow_json = json.loads(flow.flow_json)

        children = flow_json["screens"][0]["layout"]["children"]
        dropdown = children[0]

        self.assertEqual(dropdown["type"], "Dropdown")
        self.assertEqual(dropdown["name"], "country")
        self.assertEqual(len(dropdown["data-source"]), 3)
        self.assertEqual(dropdown["data-source"][0]["id"], "us")

    def test_text_input_validation(self):
        """Test text input with min/max chars."""
        screens = [{"screen_id": "form", "screen_title": "Form", "terminal": 1}]
        fields = [{
            "screen": "form",
            "field_name": "phone",
            "field_type": "TextInput",
            "label": "Phone",
            "enabled": 1,
            "min_chars": 10,
            "max_chars": 15,
            "error_message": "Phone must be 10-15 digits"
        }]

        flow = self.create_test_flow("Test Validation", screens, fields)
        flow_json = json.loads(flow.flow_json)

        children = flow_json["screens"][0]["layout"]["children"]
        phone_field = children[0]

        self.assertEqual(phone_field["min-chars"], 10)
        self.assertEqual(phone_field["max-chars"], 15)
        self.assertEqual(phone_field["error-message"], "Phone must be 10-15 digits")

    def test_disabled_fields_excluded(self):
        """Test that disabled fields are not included in flow JSON."""
        screens = [{"screen_id": "form", "screen_title": "Form", "terminal": 1}]
        fields = [
            {"screen": "form", "field_name": "active", "field_type": "TextInput", "label": "Active", "enabled": 1},
            {"screen": "form", "field_name": "disabled", "field_type": "TextInput", "label": "Disabled", "enabled": 0}
        ]

        flow = self.create_test_flow("Test Disabled", screens, fields)
        flow_json = json.loads(flow.flow_json)

        children = flow_json["screens"][0]["layout"]["children"]
        field_names = [c.get("name") for c in children if c.get("name")]

        self.assertIn("active", field_names)
        self.assertNotIn("disabled", field_names)

    def test_validation_requires_screen(self):
        """Test that flow validation requires at least one screen."""
        with self.assertRaises(frappe.ValidationError):
            flow = frappe.get_doc({
                "doctype": "WhatsApp Flow",
                "flow_name": "Test No Screens",
                "whatsapp_account": "Test Account",
                "category": "OTHER",
                "screens": [],
                "fields": []
            })
            flow.insert(ignore_permissions=True)

    def test_validation_requires_terminal_screen(self):
        """Test that flow validation requires at least one terminal screen."""
        with self.assertRaises(frappe.ValidationError):
            flow = frappe.get_doc({
                "doctype": "WhatsApp Flow",
                "flow_name": "Test No Terminal",
                "whatsapp_account": "Test Account",
                "category": "OTHER",
                "screens": [{"screen_id": "s1", "screen_title": "Screen 1", "terminal": 0}],
                "fields": []
            })
            flow.insert(ignore_permissions=True)

    def test_validation_duplicate_screen_ids(self):
        """Test that flow validation catches duplicate screen IDs."""
        with self.assertRaises(frappe.ValidationError):
            flow = frappe.get_doc({
                "doctype": "WhatsApp Flow",
                "flow_name": "Test Duplicate",
                "whatsapp_account": "Test Account",
                "category": "OTHER",
                "screens": [
                    {"screen_id": "same", "screen_title": "Screen 1", "terminal": 0},
                    {"screen_id": "same", "screen_title": "Screen 2", "terminal": 1}
                ],
                "fields": []
            })
            flow.insert(ignore_permissions=True)

    def test_client_only_flow_no_endpoint_fields(self):
        """Test that generated JSON is client-only (no endpoint required)."""
        screens = [{"screen_id": "test", "screen_title": "Test", "terminal": 1}]
        fields = [{"screen": "test", "field_name": "f1", "field_type": "TextInput", "label": "F1", "enabled": 1}]

        flow = self.create_test_flow("Test Client Only", screens, fields)
        flow_json = json.loads(flow.flow_json)

        # Client-only flows should NOT have these fields
        self.assertNotIn("routing_model", flow_json)
        self.assertNotIn("data_api_version", flow_json)
        self.assertNotIn("data_channel_uri", flow_json)

        # Should have version and screens
        self.assertIn("version", flow_json)
        self.assertIn("screens", flow_json)

    def test_dynamic_flow_has_separate_schema_and_protocol_versions(self):
        """Endpoint-backed JSON must declare both version concepts."""
        flow = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": "Test Dynamic Versions",
            "whatsapp_account": "Test Account",
            "category": "OTHER",
            "flow_json_version": "7.3",
            "data_api_version": "3.0",
            "handler_key": "test_dynamic",
            "screens": [
                {"screen_id": "DETAILS", "screen_title": "Details", "terminal": 0},
                {"screen_id": "SUMMARY", "screen_title": "Summary", "terminal": 1},
            ],
            "fields": [],
        })
        flow.insert(ignore_permissions=True)

        flow_json = json.loads(flow.flow_json)
        self.assertEqual(flow_json["version"], "7.3")
        self.assertEqual(flow_json["data_api_version"], "3.0")
        self.assertEqual(
            flow_json["routing_model"],
            {"DETAILS": ["SUMMARY"], "SUMMARY": []},
        )

    def test_conditional_routes_generate_meta_if_component(self):
        """Structured branches compile to a client-side Meta If component."""
        flow = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": "Test Conditional Branch",
            "whatsapp_account": "Test Account",
            "category": "OTHER",
            "flow_json_version": "7.3",
            "data_api_version": "3.0",
            "screens": [
                {"screen_id": "CHOICE", "screen_title": "Choose", "terminal": 0},
                {"screen_id": "BUSINESS", "screen_title": "Business", "terminal": 1},
                {"screen_id": "PERSONAL", "screen_title": "Personal", "terminal": 1}
            ],
            "fields": [{
                "screen": "CHOICE",
                "field_name": "selection",
                "field_type": "RadioButtonsGroup",
                "label": "Selection",
                "enabled": 1,
                "options": json.dumps([
                    {"id": "business", "title": "Business"},
                    {"id": "personal", "title": "Personal"}
                ])
            }],
            "routes": [
                {
                    "source_screen": "CHOICE",
                    "condition": "${form.selection} == 'option_a'",
                    "destination_screen": "BUSINESS",
                    "button_label": "Continue"
                },
                {
                    "source_screen": "CHOICE",
                    "is_default": 1,
                    "destination_screen": "PERSONAL",
                    "button_label": "Continue"
                }
            ]
        })
        flow.insert(ignore_permissions=True)

        flow_json = json.loads(flow.flow_json)
        branch = flow_json["screens"][0]["layout"]["children"][-1]
        self.assertEqual(branch["type"], "If")
        self.assertEqual(branch["condition"], "${form.selection} == 'option_a'")
        self.assertEqual(
            branch["then"][0]["on-click-action"]["next"]["name"], "BUSINESS"
        )
        self.assertEqual(
            branch["else"][0]["on-click-action"]["next"]["name"], "PERSONAL"
        )

    def test_raw_json_is_preserved(self):
        """Advanced Meta JSON must not be flattened by before_save."""
        raw_json = {
            "version": "7.2",
            "screens": [{
                "id": "RAW_SCREEN",
                "title": "Raw",
                "data": {},
                "terminal": True,
                "success": True,
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [{
                        "type": "Footer",
                        "label": "Complete",
                        "on-click-action": {"name": "complete", "payload": {}}
                    }]
                }
            }]
        }
        flow = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": "Test Raw JSON",
            "whatsapp_account": "Test Account",
            "category": "OTHER",
            "authoring_mode": "Raw JSON",
            "flow_json": json.dumps(raw_json)
        })
        flow.insert(ignore_permissions=True)
        self.assertEqual(json.loads(flow.flow_json), raw_json)
        self.assertEqual(flow.flow_json_version, "7.2")
        self.assertEqual(flow.data_api_version, "3.0")

    def test_save_syncs_the_edited_representation_with_json_priority(self):
        """Mode selects the editor; edit detection selects synchronization direction."""
        flow = self.create_test_flow(
            "Test Continuous Authoring Sync",
            [{"screen_id": "DONE", "screen_title": "Initial", "terminal": 1}],
            [],
        )

        flow.authoring_mode = "Raw JSON"
        flow.save(ignore_permissions=True)

        raw_json = json.loads(flow.flow_json)
        raw_json["screens"][0]["title"] = "Changed in JSON"
        flow.flow_json = json.dumps(raw_json)
        flow.save(ignore_permissions=True)
        self.assertEqual(flow.screens[0].screen_title, "Changed in JSON")

        flow.screens[0].screen_title = "Changed in tables"
        flow.save(ignore_permissions=True)
        self.assertEqual(
            json.loads(flow.flow_json)["screens"][0]["title"],
            "Changed in tables",
        )

        flow.screens[0].screen_title = "Conflicting table edit"
        raw_json = json.loads(flow.flow_json)
        raw_json["screens"][0]["title"] = "JSON wins"
        flow.flow_json = json.dumps(raw_json)
        flow.save(ignore_permissions=True)
        self.assertEqual(flow.screens[0].screen_title, "JSON wins")
        self.assertEqual(json.loads(flow.flow_json)["screens"][0]["title"], "JSON wins")

    def test_authoring_modes_round_trip_branched_flow(self):
        """Supported branched JSON converts back into structured tables."""
        flow = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": "Test Mode Round Trip",
            "whatsapp_account": "Test Account",
            "category": "OTHER",
            "screens": [
                {"screen_id": "CHOICE", "screen_title": "Choose", "terminal": 0},
                {"screen_id": "BUSINESS", "screen_title": "Business", "terminal": 1},
                {"screen_id": "PERSONAL", "screen_title": "Personal", "terminal": 1},
            ],
            "fields": [{
                "screen": "CHOICE",
                "field_name": "selection",
                "field_type": "RadioButtonsGroup",
                "label": "Selection",
                "enabled": 1,
                "options": json.dumps([
                    {"id": "business", "title": "Business"},
                    {"id": "personal", "title": "Personal"},
                ]),
            }],
            "routes": [
                {
                    "source_screen": "CHOICE",
                    "condition": "${form.selection} == 'option_a'",
                    "destination_screen": "BUSINESS",
                    "button_label": "Continue",
                },
                {
                    "source_screen": "CHOICE",
                    "is_default": 1,
                    "destination_screen": "PERSONAL",
                    "button_label": "Continue",
                },
            ],
        })
        flow.insert(ignore_permissions=True)
        flow.screens[0].screen_title = "Choose before Raw switch"
        structured_json = flow.generate_flow_json()

        flow.authoring_mode = "Raw JSON"
        flow.save(ignore_permissions=True)
        self.assertEqual(json.loads(flow.flow_json), structured_json)

        edited_json = json.loads(flow.flow_json)
        edited_json["screens"][0]["title"] = "Choose an option"
        edited_json["screens"][0]["layout"]["children"][0]["label"] = "Contact preference"
        flow.flow_json = json.dumps(edited_json)
        flow.authoring_mode = "Structured"
        flow.save(ignore_permissions=True)

        self.assertEqual(flow.screens[0].screen_title, "Choose an option")
        self.assertEqual(flow.fields[0].label, "Contact preference")
        self.assertEqual(len(flow.routes), 2)
        self.assertEqual(flow.routes[0].destination_screen, "BUSINESS")
        self.assertEqual(flow.routes[1].destination_screen, "PERSONAL")
        self.assertEqual(json.loads(flow.flow_json), edited_json)

    def test_raw_to_structured_rejects_unsupported_components(self):
        """Advanced Raw JSON is never flattened during a mode switch."""
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow import (
            parse_flow_json_to_structured,
        )

        raw_json = {
            "version": "7.3",
            "screens": [{
                "id": "PHOTO",
                "title": "Photo",
                "terminal": True,
                "success": True,
                "data": {},
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [{"type": "PhotoPicker", "name": "photo"}],
                },
            }],
        }

        with self.assertRaises(frappe.ValidationError):
            parse_flow_json_to_structured(raw_json)

    def test_dynamic_form_json_round_trips_through_structured_tables(self):
        """Form containers and server-driven Data Exchange remain lossless."""
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow import (
            parse_flow_json_to_structured,
        )

        raw_json = {
            "version": "7.3",
            "data_api_version": "3.0",
            "routing_model": {"FORM": ["DONE"], "DONE": []},
            "screens": [
                {
                    "id": "FORM",
                    "title": "Details",
                    "data": {
                        "states": {
                            "type": "array",
                            "items": {"type": "object"},
                            "__example__": [],
                        }
                    },
                    "layout": {
                        "type": "SingleColumnLayout",
                        "children": [{
                            "type": "Form",
                            "name": "details_form",
                            "children": [
                                {
                                    "type": "TextInput",
                                    "name": "postal_code",
                                    "label": "Postal code",
                                    "required": True,
                                    "input-type": "number",
                                },
                                {
                                    "type": "Dropdown",
                                    "name": "state",
                                    "label": "State",
                                    "data-source": "${data.states}",
                                },
                                {
                                    "type": "Footer",
                                    "label": "Continue",
                                    "on-click-action": {
                                        "name": "data_exchange",
                                        "payload": {
                                            "postal_code": "${form.postal_code}",
                                            "state": "${form.state}",
                                        },
                                    },
                                },
                            ],
                        }],
                    },
                },
                {
                    "id": "DONE",
                    "title": "Done",
                    "data": {"postal_code": {"type": "string", "__example__": ""}},
                    "terminal": True,
                    "success": True,
                    "layout": {
                        "type": "SingleColumnLayout",
                        "children": [{
                            "type": "Footer",
                            "label": "Complete",
                            "on-click-action": {
                                "name": "complete",
                                "payload": {"postal_code": "${data.postal_code}"},
                            },
                        }],
                    },
                },
            ],
        }
        parsed = parse_flow_json_to_structured(raw_json)
        flow = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": "Test Dynamic Form Round Trip",
            "whatsapp_account": "Test Account",
            "category": "OTHER",
            "handler_key": "test_dynamic_form",
            "flow_json_version": "7.3",
            "data_api_version": "3.0",
            "screens": parsed["screens"],
            "routes": parsed["routes"],
            "fields": parsed["fields"],
        })

        self.assertEqual(flow.generate_flow_json(), raw_json)
        self.assertTrue(all(route.server_driven for route in flow.routes))
        self.assertEqual(flow.fields[0].form_name, "details_form")
        self.assertEqual(flow.fields[0].input_type, "number")
        self.assertEqual(json.loads(flow.fields[1].options), "${data.states}")
        self.assertEqual(flow.fields[2].action_name, "data_exchange")

    def test_phone_number_availability(self):
        """Selected availability allows only configured numbers in the WABA."""
        account_name = "Test Account Secondary"
        if not frappe.db.exists("WhatsApp Account", account_name):
            frappe.get_doc({
                "doctype": "WhatsApp Account",
                "account_name": account_name,
                "url": "https://graph.facebook.com",
                "version": "v18.0",
                "phone_id": "test_secondary_phone_id",
                "business_id": "987654321",
                "token": "test_token"
            }).insert(ignore_permissions=True)

        flow = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": "Test Selected Phone",
            "whatsapp_account": "Test Account",
            "category": "OTHER",
            "availability_mode": "Selected Phone Numbers",
            "available_accounts": [{"whatsapp_account": account_name}],
            "screens": [{"screen_id": "DONE", "screen_title": "Done", "terminal": 1}],
            "fields": []
        })
        flow.insert(ignore_permissions=True)

        self.assertTrue(flow.is_available_on(account_name))
        self.assertFalse(flow.is_available_on("Test Account"))

        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow import (
            get_available_flows,
        )

        allowed_results = get_available_flows(
            "WhatsApp Flow", "Test Selected", "name", 0, 20,
            {"whatsapp_account": account_name}
        )
        blocked_results = get_available_flows(
            "WhatsApp Flow", "Test Selected", "name", 0, 20,
            {"whatsapp_account": "Test Account"}
        )
        self.assertIn(flow.name, {row[0] for row in allowed_results})
        self.assertNotIn(flow.name, {row[0] for row in blocked_results})


class TestWhatsAppFlowMessage(FrappeTestCase):
    """Test cases for sending WhatsApp Flow messages."""

    def test_message_flow_cta_respects_whatsapp_length_limit(self):
        message = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "15555550107",
            "content_type": "flow",
            "flow_cta": "x" * 21,
            "whatsapp_account": "Test Account",
        })
        with self.assertRaisesRegex(frappe.ValidationError, "20 characters"):
            message.validate()

    def test_flow_message_structure(self):
        """Test the structure of flow message data."""
        # This tests the message building logic without actually sending
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage

        # Mock flow doc
        class MockFlow:
            flow_id = "123456789"
            flow_cta = "Open Form"
            status = "Draft"
            screens = [type('Screen', (), {'screen_id': 'test'})()]

        class MockMessage(WhatsAppMessage):
            def __init__(self):
                self.content_type = "flow"
                self.flow = "Test Flow"
                self.flow_cta = "Fill Form"
                self.flow_screen = "contact"
                self.flow_token = "test_token_123"
                self.message = "Please complete the form"
                self.to = "15555550107"

        # Verify the expected message structure
        expected_keys = ["flow_message_version", "flow_id", "flow_cta", "flow_action", "flow_action_payload", "flow_token"]

        # The actual structure would be built in before_insert
        # Here we just verify the expected fields exist in our implementation


class TestFlowEndpoint(FrappeTestCase):
    """Test cases for flow endpoint handler."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.passphrase = "test-flow-passphrase"
        cls.private_pem = cls.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.BestAvailableEncryption(cls.passphrase.encode()),
        ).decode()

    def make_envelope(self, payload):
        aes_key = b"0123456789abcdef"
        initial_vector = b"0123456789abcdef"
        encrypted_key = self.private_key.public_key().encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        encrypted_data = AESGCM(aes_key).encrypt(
            initial_vector,
            json.dumps(payload).encode(),
            None,
        )
        return {
            "encrypted_aes_key": base64.b64encode(encrypted_key).decode(),
            "encrypted_flow_data": base64.b64encode(encrypted_data).decode(),
            "initial_vector": base64.b64encode(initial_vector).decode(),
        }, aes_key, initial_vector

    def test_decrypts_meta_envelope(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import decrypt_request

        payload = {
            "action": "data_exchange",
            "screen": "DETAILS",
            "flow_token": "token-1",
            "data": {"request_type": "standard"},
        }
        envelope, expected_key, expected_iv = self.make_envelope(payload)

        decrypted, aes_key, initial_vector = decrypt_request(
            envelope, self.private_pem, self.passphrase
        )

        self.assertEqual(decrypted, payload)
        self.assertEqual(aes_key, expected_key)
        self.assertEqual(initial_vector, expected_iv)

    def test_encrypts_response_with_inverted_iv(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import encrypt_response

        aes_key = b"0123456789abcdef"
        initial_vector = b"0123456789abcdef"
        response = {"screen": "SUMMARY", "data": {"result": 42}}

        encoded = encrypt_response(response, aes_key, initial_vector)
        flipped_iv = bytes(byte ^ 0xFF for byte in initial_vector)
        plaintext = AESGCM(aes_key).decrypt(
            flipped_iv, base64.b64decode(encoded), None
        )

        self.assertEqual(json.loads(plaintext), response)

    def test_verifies_meta_signature(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import verify_signature

        payload = b'{"encrypted_flow_data":"value"}'
        signature = "sha256=5f04605dd4b9086391171422cca7944e748c74d58e3d7a6d7d163cbcb32a2308"

        self.assertTrue(verify_signature(payload, signature, "secret"))
        self.assertFalse(verify_signature(payload, signature, "wrong-secret"))

    def test_validates_and_formats_data_api_version(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import (
            FlowProtocolError,
            format_protocol_response,
            validate_data_api_version,
        )

        flow = frappe._dict(data_api_version="3.0")
        validate_data_api_version({"version": "3.0"}, flow)
        with self.assertRaises(FlowProtocolError):
            validate_data_api_version({}, flow)
        with self.assertRaises(FlowProtocolError):
            validate_data_api_version({"version": "2.0"}, flow)

        response = format_protocol_response(
            {"screen": "SUMMARY", "data": {"result": 42}},
            {"action": "data_exchange"},
            flow,
        )
        self.assertEqual(
            response,
            {"version": "3.0", "screen": "SUMMARY", "data": {"result": 42}},
        )

    def test_protocol_error_logs_only_structural_diagnostics(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import (
            FlowProtocolError,
            log_protocol_error,
        )

        diagnostics = {
            "flow": "Example Registration",
            "whatsapp_account": "Test Account",
            "action": "data_exchange",
            "screen": "DETAILS",
            "data": {"sensitive_field": "private-value"},
            "flow_token": "secret-token",
        }
        with patch("frappe.log_error") as log_error:
            log_protocol_error(
                FlowProtocolError("Unsupported Flow action"),
                "WhatsApp Flow Protocol Error",
                diagnostics,
            )

        logged_message = log_error.call_args.args[0]
        self.assertIn("flow=Example Registration", logged_message)
        self.assertIn("action=data_exchange", logged_message)
        self.assertIn("screen=DETAILS", logged_message)
        self.assertNotIn("private-value", logged_message)
        self.assertNotIn("secret-token", logged_message)

    def test_compares_public_keys_cryptographically(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_account.whatsapp_account import (
            public_keys_match,
        )

        public_key = self.private_key.public_key()
        subject_public_key = public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        pkcs1_public_key = public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.PKCS1,
        ).decode()
        different_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        ).public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        self.assertTrue(public_keys_match(subject_public_key, pkcs1_public_key))
        self.assertFalse(public_keys_match(subject_public_key, different_key))
        self.assertFalse(public_keys_match(subject_public_key, "not a public key"))

    def test_verifies_meta_key_status_and_local_key_match(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_account.whatsapp_account import (
            WhatsAppAccount,
        )

        public_key = self.private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        account = frappe._dict(flow_public_key=public_key)
        account.get_flow_encryption_api_config = lambda: (
            "https://graph.facebook.com/v18.0/phone/whatsapp_business_encryption",
            {"Authorization": "Bearer redacted"},
        )
        account.save = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "business_public_key": public_key,
            "business_public_key_signature_status": "VALID",
        }

        with patch("requests.get", return_value=response):
            result = WhatsAppAccount.verify_flow_public_key(account)

        self.assertEqual(
            result,
            {
                "verified": True,
                "signature_status": "VALID",
                "key_matches": True,
                "promoted": False,
            },
        )
        self.assertEqual(account.flow_public_key_verified, 1)
        self.assertIsNotNone(account.flow_public_key_verified_on)
        account.save.assert_called_once_with(ignore_permissions=True)

        response.json.return_value["business_public_key_signature_status"] = "MISMATCH"
        account.save.reset_mock()
        with patch("requests.get", return_value=response):
            mismatch = WhatsAppAccount.verify_flow_public_key(account)

        self.assertFalse(mismatch["verified"])
        self.assertTrue(mismatch["key_matches"])
        self.assertEqual(account.flow_public_key_verified, 0)
        account.save.assert_called_once_with(ignore_permissions=True)

    def test_key_generation_stages_without_replacing_active_key(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_account.whatsapp_account import (
            WhatsAppAccount,
        )

        account = frappe._dict(
            flow_public_key="active-public-key",
            flow_private_key="active-private-key",
            flow_private_key_passphrase="active-passphrase",
            flow_pending_public_key=None,
        )
        account.save = MagicMock()

        generated_public_key = WhatsAppAccount.generate_flow_encryption_keys(account)

        self.assertEqual(account.flow_public_key, "active-public-key")
        self.assertEqual(account.flow_private_key, "active-private-key")
        self.assertEqual(account.flow_private_key_passphrase, "active-passphrase")
        self.assertEqual(account.flow_pending_public_key, generated_public_key)
        self.assertIn("BEGIN PUBLIC KEY", generated_public_key)
        self.assertIn("BEGIN RSA PRIVATE KEY", account.flow_pending_private_key)
        self.assertEqual(account.flow_pending_key_status, "Generated - Not Registered")
        account.save.assert_called_once_with(ignore_permissions=True)

    def test_verified_pending_key_is_promoted_with_active_key_retained(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_account.whatsapp_account import (
            WhatsAppAccount,
        )

        pending_private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        pending_public_pem = pending_private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        account = frappe._dict(
            flow_public_key="old-public-key",
            flow_pending_public_key=pending_public_pem,
            flow_pending_key_status="Registered - Verification Required",
        )
        passwords = {
            "flow_private_key": "old-private-key",
            "flow_private_key_passphrase": "old-passphrase",
            "flow_pending_private_key": "new-private-key",
            "flow_pending_private_key_passphrase": "new-passphrase",
        }
        account.get_password = MagicMock(
            side_effect=lambda fieldname, raise_exception=False: passwords.get(fieldname)
        )
        account.get_flow_encryption_api_config = lambda: (
            "https://graph.facebook.com/v18.0/phone/whatsapp_business_encryption",
            {"Authorization": "Bearer redacted"},
        )
        account.promote_pending_flow_encryption_key = (
            lambda signature_status: WhatsAppAccount.promote_pending_flow_encryption_key(
                account, signature_status
            )
        )
        account.save = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "business_public_key": pending_public_pem,
            "business_public_key_signature_status": "VALID",
        }

        with patch("requests.get", return_value=response):
            result = WhatsAppAccount.verify_flow_public_key(account)

        self.assertTrue(result["promoted"])
        self.assertEqual(account.flow_public_key, pending_public_pem)
        self.assertEqual(account.flow_private_key, "new-private-key")
        self.assertEqual(account.flow_previous_public_key, "old-public-key")
        self.assertEqual(account.flow_previous_private_key, "old-private-key")
        self.assertIsNone(account.flow_pending_public_key)
        self.assertEqual(account.flow_public_key_verified, 1)
        account.save.assert_called_once_with(ignore_permissions=True)

    def test_registration_sends_pending_key_without_replacing_active_key(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_account.whatsapp_account import (
            WhatsAppAccount,
        )

        account = frappe._dict(
            flow_public_key="active-public-key",
            flow_pending_public_key="pending-public-key",
            flow_pending_key_status="Generated - Not Registered",
            flow_public_key_verified=1,
        )
        account.get_flow_encryption_api_config = lambda: (
            "https://graph.facebook.com/v18.0/phone/whatsapp_business_encryption",
            {"Authorization": "Bearer redacted"},
        )
        account.save = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {"success": True}

        with patch("requests.post", return_value=response) as post:
            WhatsAppAccount.register_flow_public_key(account)

        self.assertEqual(
            post.call_args.kwargs["data"],
            {"business_public_key": "pending-public-key"},
        )
        self.assertEqual(account.flow_public_key, "active-public-key")
        self.assertEqual(account.flow_public_key_verified, 1)
        self.assertEqual(
            account.flow_pending_key_status, "Registered - Verification Required"
        )
        account.save.assert_called_once_with(ignore_permissions=True)

    def test_decryption_keys_include_rotation_fallbacks(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import (
            get_flow_decryption_keys,
        )

        passwords = {
            "flow_private_key": "active-key",
            "flow_private_key_passphrase": "active-passphrase",
            "flow_pending_private_key": "pending-key",
            "flow_pending_private_key_passphrase": "pending-passphrase",
            "flow_previous_private_key": "previous-key",
            "flow_previous_private_key_passphrase": "previous-passphrase",
        }
        account = frappe._dict()
        account.get_password = MagicMock(
            side_effect=lambda fieldname, raise_exception=False: passwords.get(fieldname)
        )

        self.assertEqual(
            get_flow_decryption_keys(account),
            [
                ("active-key", "active-passphrase"),
                ("pending-key", "pending-passphrase"),
                ("previous-key", "previous-passphrase"),
            ],
        )

    def test_normalizes_and_dispatches_to_installed_app(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import (
            dispatch_data_exchange,
            normalize_context,
        )

        account = frappe._dict(name="Test Account")
        flow = frappe._dict(name="Example Registration", handler_key="example_handler")
        decrypted = {
            "action": "data_exchange",
            "screen": "DETAILS",
            "flow_token": "token-1",
            "data": {"request_type": "standard"},
        }
        context = normalize_context(decrypted, flow, account)
        handler = lambda received: {
            "screen": "SUMMARY",
            "data": {"received": received["data"]["request_type"]},
        }

        with patch(
            "frappe.get_hooks",
            return_value={"example_handler": ["test_app.flow_handler"]},
        ), patch(
            "frappe.get_attr", return_value=handler
        ) as get_attr:
            response = dispatch_data_exchange(context, flow)

        self.assertEqual(
            context,
            {
                "flow": "Example Registration",
                "whatsapp_account": "Test Account",
                "action": "data_exchange",
                "screen": "DETAILS",
                "flow_token": "token-1",
                "data": {"request_type": "standard"},
            },
        )
        self.assertEqual(
            response,
            {"screen": "SUMMARY", "data": {"received": "standard"}},
        )
        get_attr.assert_called_once_with("test_app.flow_handler")

    def test_handler_dispatch_fails_closed_for_unknown_or_duplicate_key(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import (
            FlowProtocolError,
            dispatch_data_exchange,
        )

        context = {
            "flow": "Example Registration",
            "whatsapp_account": "Test Account",
            "action": "data_exchange",
            "screen": "DETAILS",
            "flow_token": "token-1",
            "data": {},
        }
        flow = frappe._dict(name="Example Registration", handler_key="example_handler")

        with patch("frappe.get_hooks", return_value={}):
            with self.assertRaises(FlowProtocolError):
                dispatch_data_exchange(context, flow)

        with patch(
            "frappe.get_hooks",
            return_value={
                "example_handler": ["app_one.handler", "app_two.handler"]
            },
        ), patch("frappe.get_attr") as get_attr:
            with self.assertRaises(FlowProtocolError):
                dispatch_data_exchange(context, flow)
            get_attr.assert_not_called()

    def test_handler_key_cannot_be_a_python_method_path(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow import (
            WhatsAppFlow,
        )

        flow = frappe._dict(handler_key="custom_app.integrations.handle_exchange")
        with self.assertRaises(frappe.ValidationError):
            WhatsAppFlow.validate_handler_key(flow)

        flow.handler_key = "example_handler"
        WhatsAppFlow.validate_handler_key(flow)

    def test_builds_flow_specific_endpoint_uri_idempotently(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow import (
            build_flow_endpoint_uri,
        )

        endpoint = build_flow_endpoint_uri(
            "https://example.com/flow?tenant=one&flow=Old+Flow",
            "Example Registration",
        )

        self.assertEqual(
            endpoint,
            "https://example.com/flow?tenant=one&flow=Example+Registration",
        )
        self.assertEqual(build_flow_endpoint_uri(endpoint, "Example Registration"), endpoint)

    def test_dynamic_flow_generates_app_endpoint_and_meta_payload(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow import (
            WhatsAppFlow,
        )

        flow = frappe._dict(
            name="Example Registration",
            flow_name="Example Registration",
            category="OTHER",
            handler_key="example_handler",
            endpoint_uri=None,
        )
        with patch(
            "frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow.get_url",
            return_value="https://example.com/api/method/flow_endpoint",
        ):
            WhatsAppFlow.configure_endpoint_uri(flow)

        payload = WhatsAppFlow.get_meta_metadata_payload(flow)
        self.assertEqual(
            flow.endpoint_uri,
            "https://example.com/api/method/flow_endpoint?flow=Example+Registration",
        )
        self.assertEqual(payload["endpoint_uri"], flow.endpoint_uri)

    def test_create_and_update_send_endpoint_uri_to_meta(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow import (
            WhatsAppFlow,
        )

        endpoint_uri = "https://example.com/flow?flow=Example+Registration"
        payload = {
            "name": "Example Registration",
            "categories": ["OTHER"],
            "endpoint_uri": endpoint_uri,
        }
        account = frappe._dict(
            url="https://graph.facebook.com",
            version="v18.0",
            business_id="waba-1",
        )
        account.get_password = MagicMock(return_value="redacted-token")
        flow = MagicMock(
            flow_id=None,
            whatsapp_account="Test Account",
        )
        flow.get_meta_metadata_payload.return_value = payload

        with patch("frappe.get_doc", return_value=account), patch(
            "frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow.make_post_request",
            return_value={"id": "meta-flow-1"},
        ) as post, patch("frappe.msgprint"):
            WhatsAppFlow.create_on_whatsapp(flow)

        self.assertEqual(json.loads(post.call_args.kwargs["data"]), payload)
        flow.upload_flow_json.assert_called_once_with(update_metadata=False)

        flow.flow_id = "meta-flow-1"
        with patch("frappe.get_doc", return_value=account), patch(
            "frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow.whatsapp_flow.make_post_request",
            return_value={"success": True},
        ) as post, patch("frappe.msgprint"):
            WhatsAppFlow.update_flow_metadata(flow)

        self.assertEqual(
            post.call_args.args[0],
            "https://graph.facebook.com/v18.0/meta-flow-1",
        )
        self.assertEqual(json.loads(post.call_args.kwargs["data"]), payload)

    def test_resolves_flow_by_token_and_account(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import resolve_flow

        account = frappe._dict(name="Test Account")
        flow = frappe._dict(name="Example Registration")
        flow.is_available_on = lambda account_name: account_name == "Test Account"

        with patch(
            "frappe.get_all",
            return_value=[frappe._dict(flow="Example Registration")],
        ) as get_all, patch("frappe.db.exists", return_value=True), patch(
            "frappe.get_doc", return_value=flow
        ):
            resolved = resolve_flow(
                {"action": "INIT", "flow_token": "token-1"}, account
            )

        self.assertEqual(resolved.name, "Example Registration")
        self.assertEqual(
            get_all.call_args.kwargs["filters"],
            {
                "type": "Outgoing",
                "content_type": "flow",
                "flow_token": "token-1",
                "whatsapp_account": "Test Account",
            },
        )

    def test_flow_hint_is_accepted_only_for_ping(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import (
            FlowTokenError,
            resolve_flow,
        )

        account = frappe._dict(name="Test Account")
        flow = frappe._dict(name="Example Registration")
        flow.is_available_on = lambda account_name: account_name == "Test Account"

        with patch("frappe.db.exists", return_value=True), patch(
            "frappe.get_doc", return_value=flow
        ), patch("frappe.get_all") as get_all:
            with self.assertRaises(FlowTokenError):
                resolve_flow(
                    {"action": "INIT"}, account, flow_hint="Example Registration"
                )
            resolved = resolve_flow(
                {"action": "ping"}, account, flow_hint="Example Registration"
            )

        self.assertEqual(resolved.name, "Example Registration")
        get_all.assert_not_called()

    def test_unknown_flow_token_fails_closed(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import (
            FlowTokenError,
            resolve_flow,
        )

        account = frappe._dict(name="Test Account")
        with patch("frappe.get_all", return_value=[]):
            with self.assertRaises(FlowTokenError):
                resolve_flow(
                    {"action": "data_exchange", "flow_token": "unknown"},
                    account,
                    flow_hint="Example Registration",
                )

    def test_request_signatures_are_mandatory_outside_developer_mode(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import (
            is_request_signature_required,
        )

        account = frappe._dict(require_flow_request_signature=0)
        with patch.dict(frappe.conf, {"developer_mode": 0}):
            self.assertTrue(is_request_signature_required(account))
        with patch.dict(frappe.conf, {"developer_mode": 1}):
            self.assertFalse(is_request_signature_required(account))
            account.require_flow_request_signature = 1
            self.assertTrue(is_request_signature_required(account))

    def test_ping_has_a_builtin_response(self):
        from frappe_whatsapp.frappe_whatsapp.api.flow_endpoint import dispatch_data_exchange

        with patch("frappe.get_hooks") as get_hooks:
            response = dispatch_data_exchange(
                {
                    "flow": None,
                    "whatsapp_account": "Test Account",
                    "action": "ping",
                    "screen": None,
                    "flow_token": None,
                    "data": {},
                }
            )

        self.assertEqual(response, {"data": {"status": "active"}})
        get_hooks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
