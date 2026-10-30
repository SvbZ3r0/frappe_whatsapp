# Copyright (c) 2022, Shridhar Patil and contributors
# For license information, please see license.txt
import json
import frappe
from frappe import _, throw
from frappe.model.document import Document
from frappe.integrations.utils import make_post_request

from frappe_whatsapp.utils import get_whatsapp_account, format_number

class WhatsAppMessage(Document):
    def validate(self):
        self.set_whatsapp_account()
        if self.flow_cta and len(self.flow_cta) > 20:
            frappe.throw(_("Flow Call to Action cannot exceed 20 characters"))

    def on_update(self):
        self.update_profile_name()

    def update_profile_name(self):
        number = self.get("from")
        if not number:
            return
        from_number = format_number(number)

        if (
            self.has_value_changed("profile_name")
            and self.profile_name
            and from_number
            and frappe.db.exists("WhatsApp Profiles", {"number": from_number})
        ):
            profile_id = frappe.get_value("WhatsApp Profiles", {"number": from_number}, "name")
            frappe.db.set_value("WhatsApp Profiles", profile_id, "profile_name", self.profile_name)

    def create_whatsapp_profile(self):
        number = format_number(self.get("from") or self.to)
        if not frappe.db.exists("WhatsApp Profiles", {"number": number}):
            frappe.get_doc({
                "doctype": "WhatsApp Profiles",
                "profile_name": self.profile_name,
                "number": number,
                "whatsapp_account": self.whatsapp_account
            }).insert(ignore_permissions=True)

    def set_whatsapp_account(self):
        """Set whatsapp account to default if missing"""
        if not self.whatsapp_account:
            account_type = 'outgoing' if self.type == 'Outgoing' else 'incoming'
            default_whatsapp_account = get_whatsapp_account(account_type=account_type)
            if not default_whatsapp_account:
                throw(_("Please set a default outgoing WhatsApp Account or Select available WhatsApp Account"))
            else:
                self.whatsapp_account = default_whatsapp_account.name

    @staticmethod
    def parse_flow_json_object(value, label):
        """Parse an optional JSON field and require an object value."""
        if not value:
            return {}
        try:
            parsed = frappe.parse_json(value) if isinstance(value, str) else value
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            frappe.throw(_("{0} contains invalid JSON: {1}").format(label, str(exc)))
        if not isinstance(parsed, dict):
            frappe.throw(_("{0} must be a JSON object").format(label))
        return dict(parsed)

    def build_flow_action_payload(self, default_screen):
        """Merge the advanced payload with convenient screen/data fields."""
        payload = self.parse_flow_json_object(
            self.flow_action_payload, _("Flow Action Payload")
        )
        initial_data = self.parse_flow_json_object(
            self.flow_initial_data, _("Initial Flow Data")
        )

        screen = self.flow_screen or payload.get("screen") or default_screen
        if screen:
            payload["screen"] = screen

        if "data" in payload and not isinstance(payload["data"], dict):
            frappe.throw(_("Flow Action Payload data must be a JSON object"))
        if initial_data:
            payload_data = dict(payload.get("data") or {})
            payload_data.update(initial_data)
            payload["data"] = payload_data

        return payload

    """Send whats app messages."""
    def before_insert(self):
        """Send message."""
        self.set_whatsapp_account()
        # Route to template path when a template is selected,
        # since message_type is read_only and cannot be set from the UI.
        if self.template:
            self.message_type = "Template"
        if self.type == "Outgoing" and self.defer_send:
            self.status = "Queued"
        else:
            self.send_outgoing()
        self.create_whatsapp_profile()

    def send_outgoing(self):
        """Dispatch an Outgoing message to Meta.

        Called from `before_insert` for first-time sends and from bulk
        retry for re-sending Failed messages. No-op for non-Outgoing docs.
        On non-template sends, raises and sets status to Failed on error;
        on template sends, `send_template` -> `notify` raises on error.
        """
        if self.type != "Outgoing":
            return

        if self.message_type != "Template":
            if self.attach and not self.attach.startswith("http"):
                link = frappe.utils.get_url() + "/" + self.attach
            else:
                link = self.attach

            data = {
                "messaging_product": "whatsapp",
                "to": format_number(self.to),
                "type": self.content_type,
            }
            if self.is_reply and self.reply_to_message_id:
                data["context"] = {"message_id": self.reply_to_message_id}
            if self.content_type in ["document", "image", "video"]:
                data[self.content_type.lower()] = {
                    "link": link,
                    "caption": self.message,
                }
            elif self.content_type == "reaction":
                data["reaction"] = {
                    "message_id": self.reply_to_message_id,
                    "emoji": self.message,
                }
            elif self.content_type == "text":
                data["text"] = {"preview_url": True, "body": self.message}

            elif self.content_type == "audio":
                data["audio"] = {"link": link}

            elif self.content_type == "interactive":
                # Interactive message (buttons or list)
                data["type"] = "interactive"
                buttons_data = json.loads(self.buttons) if isinstance(self.buttons, str) else self.buttons

                if isinstance(buttons_data, list) and len(buttons_data) > 3:
                    # Use list message for more than 3 options (max 10)
                    data["interactive"] = {
                        "type": "list",
                        "body": {"text": self.message},
                        "action": {
                            "button": "Select Option",
                            "sections": [{
                                "title": "Options",
                                "rows": [
                                    {"id": btn["id"], "title": btn["title"], "description": btn.get("description", "")}
                                    for btn in buttons_data[:10]
                                ]
                            }]
                        }
                    }
                else:
                    # Use button message for 3 or fewer options
                    data["interactive"] = {
                        "type": "button",
                        "body": {"text": self.message},
                        "action": {
                            "buttons": [
                                {
                                    "type": "reply",
                                    "reply": {"id": btn["id"], "title": btn["title"]}
                                }
                                for btn in buttons_data[:3]
                            ]
                        }
                    }

            elif self.content_type == "flow":
                # WhatsApp Flow message
                if not self.flow:
                    frappe.throw(_("WhatsApp Flow is required for flow content type"))

                flow_doc = frappe.get_doc("WhatsApp Flow", self.flow)

                if not flow_doc.flow_id:
                    frappe.throw(_("Flow must be created on WhatsApp before sending"))
                if not flow_doc.is_available_on(self.whatsapp_account):
                    frappe.throw(
                        _("Flow {0} is not available on WhatsApp Account {1}").format(
                            flow_doc.name, self.whatsapp_account
                        )
                    )

                # Determine flow mode - draft flows can be tested with mode: "draft"
                flow_mode = None
                if flow_doc.status != "Published":
                    flow_mode = "draft"
                    frappe.msgprint(_("Sending flow in draft mode (for testing only)"), indicator="orange")

                # Get a default first screen. An explicit payload or Flow Screen
                # may override this when the launch payload is built.
                default_flow_screen = None
                if flow_doc.screens:
                    default_flow_screen = flow_doc.screens[0].screen_id
                if not default_flow_screen and flow_doc.authoring_mode == "Raw JSON":
                    raw_screens = flow_doc.get_raw_flow_json().get("screens", [])
                    if raw_screens:
                        default_flow_screen = raw_screens[0].get("id")

                flow_action = self.flow_action or "navigate"
                if flow_action not in ("navigate", "data_exchange"):
                    frappe.throw(_("Flow Action must be navigate or data_exchange"))
                flow_action_payload = self.build_flow_action_payload(default_flow_screen)

                data["type"] = "interactive"
                data["interactive"] = {
                    "type": "flow",
                    "body": {"text": self.message or "Please fill out the form"},
                    "action": {
                        "name": "flow",
                        "parameters": {
                            "flow_message_version": "3",
                            "flow_id": flow_doc.flow_id,
                            "flow_cta": self.flow_cta or flow_doc.flow_cta or "Open",
                            "flow_action": flow_action,
                            "flow_action_payload": flow_action_payload
                        }
                    }
                }

                # Add draft mode for testing unpublished flows
                if flow_mode:
                    data["interactive"]["action"]["parameters"]["mode"] = flow_mode

                # Add flow token - generate one if not provided (required by WhatsApp)
                flow_token = self.flow_token or frappe.generate_hash(length=16)
                self.flow_token = flow_token
                data["interactive"]["action"]["parameters"]["flow_token"] = flow_token

            try:
                self.notify(data)
                self.status = "Success"
            except Exception as e:
                self.status = "Failed"
                frappe.throw(f"Failed to send message {str(e)}")
        elif not self.message_id:
            self.send_template()

    def send_template(self):
        """Send template."""
        template = frappe.get_doc("WhatsApp Templates", self.template)
        data = {
            "messaging_product": "whatsapp",
            "to": format_number(self.to),
            "type": "template",
            "template": {
                "name": template.actual_name or template.template_name,
                "language": {"code": template.language_code},
                "components": [],
            },
        }

        parameters = []
        template_parameters = []
        if template.sample_values:
            field_names = template.field_names.split(",") if template.field_names else template.sample_values.split(",")

            if self.body_param is not None:
                params = list(json.loads(self.body_param).values())
                for param in params:
                    parameters.append({"type": "text", "text": param})
                    template_parameters.append(param)
            elif self.flags.custom_ref_doc:
                custom_values = self.flags.custom_ref_doc
                for field_name in field_names:
                    value = custom_values.get(field_name.strip())
                    parameters.append({"type": "text", "text": value})
                    template_parameters.append(value)                    

            else:
                ref_doc = frappe.get_doc(self.reference_doctype, self.reference_name)
                for field_name in field_names:
                    value = ref_doc.get_formatted(field_name.strip())
                    parameters.append({"type": "text", "text": value})
                    template_parameters.append(value)

            self.template_parameters = json.dumps(template_parameters)

        # Always add the body component, even if parameters list is empty
        data["template"]["components"].append({
            "type": "body",
            "parameters": parameters,
        })

        if template.header_type:
            if self.attach:
                if self.attach.startswith("http"):
                    url = f'{self.attach}'
                else:
                    url = f'{frappe.utils.get_url()}{self.attach}'
                if template.header_type == 'IMAGE':
                    data['template']['components'].append({
                        "type": "header",
                        "parameters": [{
                            "type": "image",
                            "image": {
                                "link": url
                            }
                        }]
                    })

                elif template.header_type == 'DOCUMENT':
                    data['template']['components'].append({
                        "type": "header",
                        "parameters": [{
                            "type": "document",
                            "document": {
                                "link": url,
                                "filename": "document.pdf"  # should be configurable
                            }
                        }]
                    })

            elif template.sample:
                if template.header_type == 'IMAGE':
                    if template.sample.startswith("http"):
                        url = f'{template.sample}'
                    else:
                        url = f'{frappe.utils.get_url()}{template.sample}'
                    data['template']['components'].append({
                        "type": "header",
                        "parameters": [{
                            "type": "image",
                            "image": {
                                "link": url
                            }
                        }]
                    })

        # We check this before standard buttons because MPM is an interactive action
        has_mpm = False
        if self.product_catalog_json:
            try:
                catalog_data = json.loads(self.product_catalog_json)
                data['template']['components'].append({
                    "type": "button",
                    "sub_type": "mpm",
                    "index": "0",
                    "parameters": [
                        {
                            "type": "action",
                            "action": catalog_data
                        }
                    ]
                })
                has_mpm = True
            except Exception as e:
                frappe.log_error(f"Failed to parse Product Catalog JSON: {str(e)}", "WhatsApp MPM Error")

        if template.buttons:
            # Only buttons with *runtime* parameters go into components.
            # Static Call Phone and static Visit Website buttons are applied
            # by Meta from the approved template — sending them here yields
            # "sub_type must be one of {...}" errors since Meta no longer
            # accepts `phone_number`. See issue #188.
            button_parameters = []
            for idx, btn in enumerate(template.buttons):
                # Shift index if MPM was added at index 0
                current_idx = str(idx + 1) if has_mpm else str(idx)

                if btn.button_type == "Quick Reply":
                    button_parameters.append({
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": current_idx,
                        "parameters": [{"type": "payload", "payload": btn.button_label}]
                    })
                elif btn.button_type == "Visit Website" and btn.url_type == "Dynamic":
                    ref_doc = frappe.get_doc(self.reference_doctype, self.reference_name)
                    url = ref_doc.get_formatted(btn.website_url)
                    button_parameters.append({
                        "type": "button",
                        "sub_type": "url",
                        "index": current_idx,
                        "parameters": [{"type": "text", "text": url}]
                    })

            if button_parameters:
                data['template']['components'].extend(button_parameters)

        self.notify(data)

    def notify(self, data):
        """Notify."""
        whatsapp_account = frappe.get_doc(
            "WhatsApp Account",
            self.whatsapp_account,
        )
        token = whatsapp_account.get_password("token")

        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        try:
            response = make_post_request(
                f"{whatsapp_account.url}/{whatsapp_account.version}/{whatsapp_account.phone_id}/messages",
                headers=headers,
                data=json.dumps(data),
            )
            self.message_id = response["messages"][0]["id"]

        except Exception as e:
            res = frappe.flags.integration_request.json().get("error", {})
            error_message = res.get("Error", res.get("message"))
            frappe.get_doc(
                {
                    "doctype": "WhatsApp Notification Log",
                    "template": "Text Message",
                    "meta_data": frappe.flags.integration_request.json(),
                }
            ).insert(ignore_permissions=True)

            frappe.throw(msg=error_message, title=res.get("error_user_title", "Error"))

    def format_number(self, number):
        """Format number."""
        if number.startswith("+"):
            number = number[1 : len(number)]

        return number

    @frappe.whitelist()
    def send_read_receipt(self):
        data = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": self.message_id
        }

        settings = frappe.get_doc(
            "WhatsApp Account",
            self.whatsapp_account,
        )

        token = settings.get_password("token")

        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        try:
            response = make_post_request(
                f"{settings.url}/{settings.version}/{settings.phone_id}/messages",
                headers=headers,
                data=json.dumps(data),
            )

            if response.get("success"):
                self.status = "marked as read"
                self.save()
                return response.get("success")

        except Exception as e:
            res = frappe.flags.integration_request.json().get("error", {})
            error_message = res.get("Error", res.get("message"))
            frappe.log_error("WhatsApp API Error", f"{error_message}\n{res}")


def on_doctype_update():
    frappe.db.add_index("WhatsApp Message", ["reference_doctype", "reference_name"])


@frappe.whitelist()
def send_template(to, reference_doctype, reference_name, template):
    try:
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "to": to,
            "type": "Outgoing",
            "message_type": "Template",
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "content_type": "text",
            "template": template
        })

        doc.save()
    except Exception as e:
        raise e
