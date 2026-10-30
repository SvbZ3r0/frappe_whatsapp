# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.integrations.utils import make_post_request, make_request
from frappe.utils import get_url


FLOW_ENDPOINT_METHOD = (
    "frappe_whatsapp.frappe_whatsapp.api.flow_endpoint.handle_flow_request"
)


def build_flow_endpoint_uri(endpoint_uri, flow_name):
    """Add exactly one URL-encoded Flow hint while preserving other query values."""
    parts = urlsplit(endpoint_uri)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "flow"
    ]
    query.append(("flow", flow_name))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


STRUCTURED_FIELD_TYPES = {
    "TextInput",
    "TextArea",
    "DatePicker",
    "RadioButtonsGroup",
    "CheckboxGroup",
    "Dropdown",
    "OptIn",
    "EmbeddedLink",
    "Image",
    "TextHeading",
    "TextSubheading",
    "TextBody",
    "TextCaption",
}
TEXT_COMPONENT_TYPES = {
    "TextHeading",
    "TextSubheading",
    "TextBody",
    "TextCaption",
}
OPTION_COMPONENT_TYPES = {"Dropdown", "RadioButtonsGroup", "CheckboxGroup"}


def json_values_match(left, right):
    """Compare JSON strings semantically, falling back to direct equality."""
    try:
        return json.loads(left or "{}") == json.loads(right or "{}")
    except (TypeError, json.JSONDecodeError):
        return left == right


def parse_flow_json_to_structured(flow_json):
    """Parse the losslessly representable subset of Meta Flow JSON."""
    screens = flow_json.get("screens")
    if not isinstance(screens, list) or not screens:
        frappe.throw(_("Flow JSON must contain a non-empty screens array"))

    parsed = {"screens": [], "routes": [], "fields": []}
    display_field_index = 0
    for screen in screens:
        if not isinstance(screen, dict):
            frappe.throw(_("Each Flow screen must be a JSON object"))
        screen_id = screen.get("id")
        layout = screen.get("layout")
        if not screen_id or not isinstance(layout, dict):
            frappe.throw(_("Each Flow screen requires an id and layout"))
        if layout.get("type") != "SingleColumnLayout":
            frappe.throw(
                _("Structured mode supports only SingleColumnLayout screens")
            )
        children = layout.get("children")
        if not isinstance(children, list):
            frappe.throw(_("Screen {0} layout children must be an array").format(screen_id))

        terminal = bool(screen.get("terminal"))
        parsed["screens"].append(
            {
                "screen_id": screen_id,
                "screen_title": screen.get("title") or screen_id,
                "terminal": int(terminal),
                "refresh_on_back": int(bool(screen.get("refresh_on_back"))),
                "layout_type": "SingleColumnLayout",
            }
        )

        route_component_seen = False
        for component in children:
            if not isinstance(component, dict):
                frappe.throw(
                    _("Every component on screen {0} must be a JSON object").format(
                        screen_id
                    )
                )
            component_type = component.get("type")
            if component_type == "If":
                if terminal or route_component_seen:
                    frappe.throw(
                        _("Screen {0} has an unsupported conditional layout").format(
                            screen_id
                        )
                    )
                parsed["routes"].extend(
                    parse_conditional_routes(component, screen_id)
                )
                route_component_seen = True
                continue
            if component_type == "Footer":
                action_name = (component.get("on-click-action") or {}).get("name")
                if action_name == "navigate":
                    if terminal or route_component_seen:
                        frappe.throw(
                            _("Screen {0} has multiple or invalid route footers").format(
                                screen_id
                            )
                        )
                    parsed["routes"].append(
                        parse_route_footer(component, screen_id, is_default=True)
                    )
                    route_component_seen = True
                    continue
                if action_name != "complete" or not terminal:
                    frappe.throw(
                        _("Screen {0} uses an unsupported Footer action").format(
                            screen_id
                        )
                    )
                if component.get("label") == "Complete":
                    # This is the structured generator's implicit terminal footer.
                    continue
                display_field_index += 1
                parsed["fields"].append(
                    {
                        "screen": screen_id,
                        "field_name": f"footer_{display_field_index}",
                        "field_type": "Footer",
                        "label": component.get("label") or "Submit",
                        "enabled": 1,
                    }
                )
                continue

            if component_type not in STRUCTURED_FIELD_TYPES:
                frappe.throw(
                    _("Component type {0} is not supported by Structured mode").format(
                        component_type or "(missing)"
                    )
                )
            field, display_field_index = parse_field_component(
                component, screen_id, display_field_index
            )
            parsed["fields"].append(field)

    return parsed


def parse_field_component(component, screen_id, display_field_index):
    """Convert one supported Meta component into a field table row."""
    component_type = component["type"]
    field = {
        "screen": screen_id,
        "field_type": component_type,
        "enabled": 1,
    }

    if component_type in TEXT_COMPONENT_TYPES:
        display_field_index += 1
        field.update(
            {
                "field_name": f"display_{display_field_index}",
                "label": component.get("text") or "",
            }
        )
        return field, display_field_index

    if component_type == "Image":
        display_field_index += 1
        field.update(
            {
                "field_name": f"image_{display_field_index}",
                "label": component.get("alt-text"),
                "init_value": component.get("src") or "",
            }
        )
        return field, display_field_index

    if component_type == "EmbeddedLink":
        action = component.get("on-click-action") or {}
        next_screen = action.get("next") or {}
        if action.get("name") != "navigate" or next_screen.get("type") != "screen":
            frappe.throw(_("EmbeddedLink uses an unsupported action"))
        display_field_index += 1
        field.update(
            {
                "field_name": f"link_{display_field_index}",
                "label": component.get("text") or "Link",
                "init_value": next_screen.get("name") or "",
            }
        )
        return field, display_field_index

    if not component.get("name"):
        frappe.throw(
            _("Component {0} on screen {1} requires a name").format(
                component_type, screen_id
            )
        )
    field.update(
        {
            "field_name": component["name"],
            "label": component.get("label") or component["name"],
            "required": int(bool(component.get("required"))),
            "helper_text": component.get("helper-text"),
            "init_value": component.get("init-value"),
            "min_chars": component.get("min-chars"),
            "max_chars": component.get("max-chars"),
            "error_message": component.get("error-message"),
        }
    )
    if component_type in OPTION_COMPONENT_TYPES and component.get("data-source"):
        field["options"] = json.dumps(component["data-source"])
    return field, display_field_index


def parse_conditional_routes(component, screen_id):
    """Parse the nested If/Footer shape emitted by structured branches."""
    routes = []
    branch = component
    while isinstance(branch, dict) and branch.get("type") == "If":
        then_children = branch.get("then")
        else_children = branch.get("else")
        if (
            not branch.get("condition")
            or not isinstance(then_children, list)
            or len(then_children) != 1
            or not isinstance(else_children, list)
            or len(else_children) != 1
        ):
            frappe.throw(
                _("Screen {0} has a conditional layout that Structured mode cannot represent").format(
                    screen_id
                )
            )
        route = parse_route_footer(then_children[0], screen_id, is_default=False)
        route["condition"] = branch["condition"]
        routes.append(route)
        branch = else_children[0]

    routes.append(parse_route_footer(branch, screen_id, is_default=True))
    return routes


def parse_route_footer(component, screen_id, is_default):
    """Convert a navigation Footer into a route table row."""
    if not isinstance(component, dict) or component.get("type") != "Footer":
        frappe.throw(
            _("Screen {0} route branches must end in Footer components").format(
                screen_id
            )
        )
    action = component.get("on-click-action") or {}
    next_screen = action.get("next") or {}
    if (
        action.get("name") != "navigate"
        or next_screen.get("type") != "screen"
        or not next_screen.get("name")
    ):
        frappe.throw(
            _("Screen {0} route Footer must navigate to another screen").format(
                screen_id
            )
        )
    return {
        "source_screen": screen_id,
        "condition": None,
        "is_default": int(is_default),
        "destination_screen": next_screen["name"],
        "button_label": component.get("label") or "Continue",
    }


class WhatsAppFlow(Document):
    def before_save(self):
        """Generate structured JSON or normalize user-authored raw JSON."""
        if self.authoring_mode == "Raw JSON":
            self.flow_json = json.dumps(self.get_raw_flow_json(), indent=2)
        else:
            self.flow_json = json.dumps(self.generate_flow_json(), indent=2)

    def validate(self):
        """Validate flow configuration."""
        self.convert_authoring_mode_if_changed()
        if self.authoring_mode == "Raw JSON":
            raw_flow_json = self.get_raw_flow_json()
            self.flow_json_version = raw_flow_json.get(
                "version", self.flow_json_version or "7.3"
            )
            self.data_api_version = raw_flow_json.get(
                "data_api_version", self.data_api_version or "3.0"
            )
        else:
            self.validate_screens()
            self.validate_routes()
        self.validate_available_accounts()
        self.validate_handler_key()
        self.configure_endpoint_uri()

    def validate_handler_key(self):
        """Keep handler selection declarative and unsuitable for method paths."""
        if not self.handler_key:
            return
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,139}", self.handler_key):
            frappe.throw(
                _(
                    "Backend Handler Key may contain only lowercase letters, numbers, underscores, and hyphens"
                )
            )

    def configure_endpoint_uri(self):
        """Generate and persist the exact Flow-specific endpoint Meta should call."""
        if not self.handler_key:
            return
        base_uri = self.endpoint_uri or get_url(f"/api/method/{FLOW_ENDPOINT_METHOD}")
        self.endpoint_uri = build_flow_endpoint_uri(
            base_uri, self.name or self.flow_name
        )

    def get_meta_metadata_payload(self):
        """Return metadata accepted by Meta's create and update Flow APIs."""
        if self.handler_key and not self.endpoint_uri:
            self.configure_endpoint_uri()
        payload = {
            "name": self.flow_name,
            "categories": [self.category],
        }
        if self.endpoint_uri:
            endpoint_uri = build_flow_endpoint_uri(
                self.endpoint_uri, self.name or self.flow_name
            )
            endpoint_parts = urlsplit(endpoint_uri)
            if endpoint_parts.scheme != "https" or not endpoint_parts.netloc:
                frappe.throw(_("Data Endpoint URI must be an absolute HTTPS URL"))
            payload["endpoint_uri"] = endpoint_uri
        return payload

    def get_raw_flow_json(self):
        """Return validated Flow JSON from the raw editor."""
        try:
            flow_json = json.loads(self.flow_json or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            frappe.throw(_("Flow JSON is invalid: {0}").format(str(exc)))

        if not isinstance(flow_json, dict) or not flow_json.get("screens"):
            frappe.throw(_("Flow JSON must contain at least one screen"))
        return flow_json

    def convert_authoring_mode_if_changed(self):
        """Convert representations on mode changes without silently losing data."""
        previous = self.get_doc_before_save()
        if not previous or previous.authoring_mode == self.authoring_mode:
            return

        if self.authoring_mode == "Raw JSON":
            # If the editor was deliberately changed at the same time as the mode,
            # it is already authoritative. Otherwise include current table edits.
            if json_values_match(self.flow_json, previous.flow_json):
                self.flow_json = json.dumps(self.generate_flow_json(), indent=2)
            return

        if self.authoring_mode == "Structured":
            self.import_raw_json_to_structured()

    def import_raw_json_to_structured(self):
        """Populate structured tables when Raw JSON is losslessly representable."""
        raw_flow_json = self.get_raw_flow_json()
        parsed = parse_flow_json_to_structured(raw_flow_json)
        self.set("screens", parsed["screens"])
        self.set("routes", parsed["routes"])
        self.set("fields", parsed["fields"])
        self.flow_json_version = raw_flow_json.get(
            "version", self.flow_json_version or "7.3"
        )
        self.data_api_version = raw_flow_json.get(
            "data_api_version", self.data_api_version or "3.0"
        )

        regenerated = self.generate_flow_json()
        if regenerated != raw_flow_json:
            frappe.throw(
                _(
                    "This Raw JSON contains constructs that the structured screen, route, and field tables cannot represent losslessly. Keep this Flow in Raw JSON mode."
                )
            )

    def validate_screens(self):
        """Ensure at least one screen exists and has valid configuration."""
        if not self.screens:
            frappe.throw(_("At least one screen is required"))

        screen_ids = []
        terminal_count = 0

        for screen in self.screens:
            if screen.screen_id in screen_ids:
                frappe.throw(_("Duplicate screen ID: {0}").format(screen.screen_id))
            screen_ids.append(screen.screen_id)

            if screen.terminal:
                terminal_count += 1

        if terminal_count == 0:
            frappe.throw(_("At least one screen must be marked as terminal"))

    def validate_routes(self):
        """Validate the explicit route graph used by structured flows."""
        if not self.routes:
            return

        screens = {screen.screen_id: screen for screen in self.screens}
        routes_by_source = self.get_routes_by_source()

        for source, routes in routes_by_source.items():
            if source not in screens:
                frappe.throw(_("Route source screen does not exist: {0}").format(source))
            if screens[source].terminal:
                frappe.throw(_("Terminal screen {0} cannot have outgoing routes").format(source))

            defaults = [route for route in routes if route.is_default]
            if len(defaults) > 1:
                frappe.throw(_("Screen {0} has more than one default route").format(source))
            if len(routes) > 4:
                frappe.throw(
                    _("Screen {0} exceeds the supported limit of three conditions plus a default route").format(source)
                )
            if len(routes) > 1 and not defaults:
                frappe.throw(_("Branching screen {0} requires a default route").format(source))
            if len(routes) == 1 and routes[0].condition:
                frappe.throw(
                    _("A single route on {0} cannot be conditional; add a default branch").format(source)
                )

            conditions = set()
            for route in routes:
                if route.destination_screen not in screens:
                    frappe.throw(_("Route destination screen does not exist: {0}").format(route.destination_screen))
                if route.destination_screen == source:
                    frappe.throw(_("A screen cannot route to itself: {0}").format(source))
                if route.is_default and route.condition:
                    frappe.throw(_("Default route on {0} cannot have a condition").format(source))
                if not route.is_default and not route.condition and len(routes) > 1:
                    frappe.throw(_("Non-default route on {0} requires a condition").format(source))
                if route.condition and route.condition in conditions:
                    frappe.throw(_("Duplicate route condition on screen {0}").format(source))
                conditions.add(route.condition)

        graph = self.get_route_graph()
        visiting = set()
        visited = set()

        def visit(screen_id):
            if screen_id in visiting:
                frappe.throw(_("Flow routes contain a cycle at screen {0}").format(screen_id))
            if screen_id in visited:
                return
            visiting.add(screen_id)
            for destination in graph.get(screen_id, []):
                visit(destination)
            visiting.remove(screen_id)
            visited.add(screen_id)

        visit(self.screens[0].screen_id)
        unreachable = set(screens) - visited
        if unreachable:
            frappe.throw(_("Unreachable screens: {0}").format(", ".join(sorted(unreachable))))
        invalid_leaves = [
            screen_id
            for screen_id, destinations in graph.items()
            if not destinations and not screens[screen_id].terminal
        ]
        if invalid_leaves:
            frappe.throw(
                _("Non-terminal screens without an outgoing route: {0}").format(
                    ", ".join(sorted(invalid_leaves))
                )
            )

    def validate_available_accounts(self):
        """Restrict selected sending numbers to the Flow's WABA."""
        if not self.whatsapp_account:
            return

        owner_business_id = frappe.db.get_value(
            "WhatsApp Account", self.whatsapp_account, "business_id"
        )
        selected = [row.whatsapp_account for row in self.available_accounts]
        if len(selected) != len(set(selected)):
            frappe.throw(_("The same phone number cannot be selected more than once"))

        if self.availability_mode == "Selected Phone Numbers" and not selected:
            frappe.throw(_("Select at least one phone number for this Flow"))

        for account_name in selected:
            business_id = frappe.db.get_value("WhatsApp Account", account_name, "business_id")
            same_waba = account_name == self.whatsapp_account or (
                owner_business_id and business_id == owner_business_id
            )
            if not same_waba:
                frappe.throw(
                    _("WhatsApp Account {0} does not belong to the Flow's WABA").format(account_name)
                )

    def is_available_on(self, whatsapp_account):
        """Return whether this Flow may be sent by the selected phone number."""
        if not whatsapp_account:
            return False

        owner_business_id = frappe.db.get_value(
            "WhatsApp Account", self.whatsapp_account, "business_id"
        )
        account_business_id = frappe.db.get_value(
            "WhatsApp Account", whatsapp_account, "business_id"
        )
        same_waba = whatsapp_account == self.whatsapp_account or (
            owner_business_id and owner_business_id == account_business_id
        )
        if not same_waba:
            return False
        if self.availability_mode != "Selected Phone Numbers":
            return True
        return whatsapp_account in {
            row.whatsapp_account for row in self.available_accounts
        }

    def get_routes_by_source(self):
        routes = {}
        for route in self.routes:
            routes.setdefault(route.source_screen, []).append(route)
        return routes

    def get_route_graph(self):
        """Return explicit routes plus legacy table-order routes."""
        explicit = self.get_routes_by_source()
        graph = {}
        for index, screen in enumerate(self.screens):
            if screen.terminal:
                graph[screen.screen_id] = []
            elif screen.screen_id in explicit:
                graph[screen.screen_id] = [
                    route.destination_screen for route in explicit[screen.screen_id]
                ]
            elif index + 1 < len(self.screens):
                graph[screen.screen_id] = [self.screens[index + 1].screen_id]
            else:
                graph[screen.screen_id] = []
        return graph

    def generate_flow_json(self):
        """Generate static or endpoint-backed Flow JSON from configuration."""
        flow = {
            "version": self.flow_json_version or "7.3",
            "screens": []
        }

        if self.handler_key:
            flow["data_api_version"] = self.data_api_version or "3.0"
            flow["routing_model"] = self.get_route_graph()

        # Build a graph-aware map of fields that may arrive from ancestor screens.
        screen_incoming_data = self.build_screen_data_map()

        for screen in self.screens:
            screen_data = self.build_screen(screen, screen_incoming_data.get(screen.screen_id, {}))
            flow["screens"].append(screen_data)

        return flow

    def build_screen_data_map(self):
        """Build a map of fields passed to each screen from previous screens.

        Returns:
            dict: {screen_id: {field_name: {"type": "string", "__example__": ""}}}
        """
        screen_data_map = {}
        for screen in self.screens:
            incoming = {}
            for ancestor_id in self.get_ancestor_screen_ids(screen.screen_id):
                for field in self.get_input_fields(ancestor_id):
                    incoming[field.field_name] = {
                        "type": "string",
                        "__example__": ""
                    }
            if incoming:
                screen_data_map[screen.screen_id] = incoming
        return screen_data_map

    def get_input_fields(self, screen_id):
        display_types = {
            "TextHeading", "TextSubheading", "TextBody", "TextCaption",
            "Image", "EmbeddedLink", "Footer"
        }
        return [
            field for field in self.fields
            if field.screen == screen_id and field.enabled and field.field_type not in display_types
        ]

    def get_ancestor_screen_ids(self, screen_id):
        graph = self.get_route_graph()
        reverse_graph = {screen.screen_id: set() for screen in self.screens}
        for source, destinations in graph.items():
            for destination in destinations:
                reverse_graph.setdefault(destination, set()).add(source)

        ancestors = set()
        pending = list(reverse_graph.get(screen_id, set()))
        while pending:
            ancestor = pending.pop()
            if ancestor in ancestors:
                continue
            ancestors.add(ancestor)
            pending.extend(reverse_graph.get(ancestor, set()))
        return [
            screen.screen_id for screen in self.screens
            if screen.screen_id in ancestors
        ]

    def build_screen(self, screen, incoming_data=None):
        """Build a single screen definition."""
        screen_data = {
            "id": screen.screen_id,
            "title": screen.screen_title,
            "data": incoming_data or {},
            "layout": {
                "type": "SingleColumnLayout",
                "children": []
            }
        }

        if screen.terminal:
            screen_data["terminal"] = True
            screen_data["success"] = True

        if screen.refresh_on_back:
            screen_data["refresh_on_back"] = True

        # Build fields for this screen
        children = self.build_screen_fields(screen)
        screen_data["layout"]["children"] = children

        return screen_data

    def build_screen_fields(self, screen):
        """Build field components for a screen."""
        children = []
        has_footer = False
        explicit_routes = self.get_routes_by_source().get(screen.screen_id, [])

        # Get fields that belong to this screen from the fields table
        for field in self.fields:
            if field.screen != screen.screen_id:
                continue
            if not field.enabled:
                continue

            component = self.build_field_component(field, screen)
            if component:
                children.append(component)
                if field.field_type == "Footer" and not explicit_routes:
                    has_footer = True

        # Explicit routes own the footer because a branch may need one footer
        # in every arm of an If component.
        if explicit_routes and not screen.terminal:
            children = [child for child in children if child.get("type") != "Footer"]
            children.append(self.build_route_component(screen, explicit_routes))
        elif not has_footer:
            footer_action = self.build_footer_action(None, screen)
            children.append({
                "type": "Footer",
                "label": "Continue" if not screen.terminal else "Complete",
                "on-click-action": footer_action
            })

        return children

    def build_route_component(self, screen, routes):
        """Compile ordered route rows to navigate footers or nested Meta Ifs."""
        default_route = next((route for route in routes if route.is_default), None)
        conditional_routes = [route for route in routes if not route.is_default]

        if len(routes) == 1:
            route = routes[0]
            return self.build_route_footer(screen, route)

        def build_branch(index):
            route = conditional_routes[index]
            if index + 1 < len(conditional_routes):
                else_children = [build_branch(index + 1)]
            else:
                else_children = [self.build_route_footer(screen, default_route)]
            return {
                "type": "If",
                "condition": route.condition,
                "then": [self.build_route_footer(screen, route)],
                "else": else_children
            }

        return build_branch(0)

    def build_route_footer(self, screen, route):
        return {
            "type": "Footer",
            "label": route.button_label or "Continue",
            "on-click-action": self.build_footer_action(None, screen, route)
        }

    def build_field_component(self, field, screen):
        """Build a single field component."""
        field_type = field.field_type

        # Text display components (no input)
        if field_type in ["TextHeading", "TextSubheading", "TextBody", "TextCaption"]:
            return {
                "type": field_type,
                "text": field.label or ""
            }

        # Image component
        if field_type == "Image":
            component = {
                "type": "Image",
                "src": field.init_value or ""
            }
            if field.label:
                component["alt-text"] = field.label
            return component

        # Embedded link
        if field_type == "EmbeddedLink":
            return {
                "type": "EmbeddedLink",
                "text": field.label or "Link",
                "on-click-action": {
                    "name": "navigate",
                    "next": {"type": "screen", "name": field.init_value or ""}
                }
            }

        # Footer (submit button)
        if field_type == "Footer":
            action = self.build_footer_action(field, screen)
            return {
                "type": "Footer",
                "label": field.label or "Submit",
                "on-click-action": action
            }

        # Input components
        component = {
            "type": field_type,
            "name": field.field_name,
            "label": field.label or field.field_name
        }

        if field.required:
            component["required"] = True

        if field.helper_text:
            component["helper-text"] = field.helper_text

        if field.init_value:
            component["init-value"] = field.init_value

        # Text input specific
        if field_type in ["TextInput", "TextArea"]:
            if field.min_chars:
                component["min-chars"] = field.min_chars
            if field.max_chars:
                component["max-chars"] = field.max_chars
            if field.error_message:
                component["error-message"] = field.error_message

        # Options for dropdown/radio/checkbox
        if field_type in ["Dropdown", "RadioButtonsGroup", "CheckboxGroup"]:
            options = self.parse_options(field.options)
            if options:
                component["data-source"] = options

        # OptIn specific
        if field_type == "OptIn":
            component["label"] = field.label or "I agree"
            if field.required:
                component["required"] = True

        return component

    def build_footer_action(self, field, screen, route=None):
        """Build the action for a footer button."""
        if screen.terminal:
            # Complete the flow
            return {
                "name": "complete",
                "payload": self.build_payload(screen)
            }
        else:
            # Navigate to next screen
            next_screen = None
            if route:
                next_screen = next(
                    (item for item in self.screens if item.screen_id == route.destination_screen),
                    None
                )
            if not next_screen:
                next_screen = self.get_next_screen(screen)
            if next_screen:
                return {
                    "name": "navigate",
                    "next": {
                        "type": "screen",
                        "name": next_screen.screen_id
                    },
                    "payload": self.build_payload(screen)
                }
            else:
                return {
                    "name": "complete",
                    "payload": self.build_payload(screen)
                }

    def build_payload(self, screen):
        """Build payload with all field values up to and including this screen."""
        payload = {}

        for ancestor_id in self.get_ancestor_screen_ids(screen.screen_id):
            for field in self.get_input_fields(ancestor_id):
                payload[field.field_name] = "${data." + field.field_name + "}"
        for field in self.get_input_fields(screen.screen_id):
            payload[field.field_name] = "${form." + field.field_name + "}"

        return payload

    def get_next_screen(self, current_screen):
        """Get the next screen in sequence."""
        found_current = False
        for screen in self.screens:
            if found_current:
                return screen
            if screen.screen_id == current_screen.screen_id:
                found_current = True
        return None

    def parse_options(self, options_json):
        """Parse options JSON string to list."""
        if not options_json:
            return []
        try:
            options = json.loads(options_json)
            if isinstance(options, list):
                return options
            return []
        except json.JSONDecodeError:
            return []

    @frappe.whitelist()
    def create_on_whatsapp(self):
        """Create the flow on WhatsApp Business API."""
        if self.flow_id:
            frappe.throw(_("Flow already exists on WhatsApp. Use update instead."))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        # Create the flow
        url = f"{account.url}/{account.version}/{account.business_id}/flows"

        payload = self.get_meta_metadata_payload()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            response = make_post_request(url, headers=headers, data=json.dumps(payload))
            self.flow_id = response.get("id")
            self.save()

            # Upload the flow JSON
            self.upload_flow_json(update_metadata=False)

            frappe.msgprint(_("Flow created successfully on WhatsApp"))

        except Exception as e:
            frappe.throw(_("Failed to create flow: {0}").format(str(e)))

    @frappe.whitelist()
    def update_flow_metadata(self, show_message=True):
        """Update Flow name, category, and endpoint URI on Meta."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")
        url = f"{account.url}/{account.version}/{self.flow_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            response = make_post_request(
                url,
                headers=headers,
                data=json.dumps(self.get_meta_metadata_payload()),
            )
            if not response.get("success"):
                frappe.throw(_("Meta did not accept the Flow metadata update"))
            self.save()
            if show_message:
                frappe.msgprint(_("Flow metadata updated successfully on WhatsApp"))
            return response
        except Exception as e:
            frappe.throw(_("Failed to update flow metadata: {0}").format(str(e)))

    @frappe.whitelist()
    def upload_flow_json(self, update_metadata=True):
        """Upload flow JSON to WhatsApp."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}/assets"

        # Generate structured JSON or upload raw Meta JSON verbatim.
        flow_json = (
            self.get_raw_flow_json()
            if self.authoring_mode == "Raw JSON"
            else self.generate_flow_json()
        )

        headers = {
            "Authorization": f"Bearer {token}"
        }

        files = {
            "file": ("flow.json", json.dumps(flow_json), "application/json"),
            "name": (None, "flow.json"),
            "asset_type": (None, "FLOW_JSON")
        }

        try:
            import requests

            if update_metadata:
                self.update_flow_metadata(show_message=False)
            response = requests.post(url, headers=headers, files=files)

            if response.status_code != 200:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", response.text)
                # Also check for validation errors
                error_details = error_data.get("error", {}).get("error_user_msg", "")
                if error_details:
                    error_msg = f"{error_msg} - {error_details}"
                frappe.throw(_("Failed to upload flow JSON: {0}").format(error_msg))

            frappe.msgprint(_("Flow JSON uploaded successfully"))

        except requests.exceptions.RequestException as e:
            frappe.throw(_("Failed to upload flow JSON: {0}").format(str(e)))

    @frappe.whitelist()
    def publish_flow(self):
        """Publish the flow to make it available for use."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        if self.status == "Published":
            frappe.throw(_("Flow is already published"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}/publish"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            import requests
            response = requests.post(url, headers=headers)

            if response.status_code != 200:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", response.text)
                frappe.throw(_("Failed to publish flow: {0}").format(error_msg))

            self.status = "Published"
            self.save()

            frappe.msgprint(_("Flow published successfully"))

        except Exception as e:
            frappe.throw(_("Failed to publish flow: {0}").format(str(e)))

    @frappe.whitelist()
    def deprecate_flow(self):
        """Deprecate the flow."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}/deprecate"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            response = make_post_request(url, headers=headers)
            self.status = "Deprecated"
            self.save()

            frappe.msgprint(_("Flow deprecated successfully"))

        except Exception as e:
            frappe.throw(_("Failed to deprecate flow: {0}").format(str(e)))

    @frappe.whitelist()
    def delete_from_whatsapp(self):
        """Delete the flow from WhatsApp."""
        if not self.flow_id:
            frappe.throw(_("Flow does not exist on WhatsApp"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.delete(url, headers=headers)
            response.raise_for_status()

            self.flow_id = None
            self.status = "Draft"
            self.save()

            frappe.msgprint(_("Flow deleted from WhatsApp"))

        except Exception as e:
            frappe.throw(_("Failed to delete flow: {0}").format(str(e)))

    @frappe.whitelist()
    def get_flow_preview(self):
        """Get the preview URL for the flow."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}?fields=preview.invalidate(false)"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            data = response.json()
            preview_url = data.get("preview", {}).get("preview_url")

            if preview_url:
                self.preview_url = preview_url
                self.save()
                return preview_url

            frappe.throw(_("Preview URL not available"))

        except Exception as e:
            frappe.throw(_("Failed to get preview: {0}").format(str(e)))

    @frappe.whitelist()
    def send_test(self, phone_number, message=None):
        """Send a test flow message to a phone number.

        Args:
            phone_number: Phone number to send to (must be a test number for draft flows)
            message: Optional message body
        """
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        # Create a WhatsApp Message to send the flow
        msg = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": phone_number,
            "content_type": "flow",
            "flow": self.name,
            "flow_cta": self.flow_cta or "Open Form",
            "message": message or f"Test: {self.flow_name}",
            "whatsapp_account": self.whatsapp_account
        })
        msg.insert(ignore_permissions=True)

        frappe.msgprint(
            _("Test flow sent to {0}").format(phone_number),
            indicator="green"
        )
        return msg.name

    @frappe.whitelist()
    def get_flow_status(self):
        """Get flow status and validation errors from WhatsApp."""
        if not self.flow_id:
            frappe.throw(_("Flow must be created on WhatsApp first"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}?fields=id,name,status,categories,validation_errors,json_version,data_api_version,data_channel_uri,preview,whatsapp_business_account"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            data = response.json()

            changed = False
            if data.get("status"):
                self.status = data["status"].title()
                changed = True
            if data.get("data_channel_uri"):
                self.endpoint_uri = data["data_channel_uri"]
                changed = True
            if data.get("json_version"):
                self.flow_json_version = data["json_version"]
                changed = True
            if data.get("data_api_version"):
                self.data_api_version = data["data_api_version"]
                changed = True
            if changed:
                self.save()

            # Check for validation errors
            validation_errors = data.get("validation_errors", [])
            if validation_errors:
                error_messages = []
                for error in validation_errors:
                    error_messages.append(f"- {error.get('error', 'Unknown error')} at {error.get('error_type', 'unknown')}")
                frappe.msgprint(
                    _("Flow Validation Errors:\n{0}").format("\n".join(error_messages)),
                    title=_("Validation Errors"),
                    indicator="red"
                )
            else:
                frappe.msgprint(
                    _("Flow Status: {0}\nJSON Version: {1}").format(
                        data.get("status", "Unknown"),
                        data.get("json_version", "Unknown")
                    ),
                    title=_("Flow Status"),
                    indicator="green" if data.get("status") == "PUBLISHED" else "blue"
                )

            return data

        except Exception as e:
            frappe.throw(_("Failed to get flow status: {0}").format(str(e)))

    @frappe.whitelist()
    def sync_from_whatsapp(self):
        """Sync flow details from WhatsApp."""
        if not self.flow_id:
            frappe.throw(_("Flow ID is required to sync"))

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        # Get flow details
        url = f"{account.url}/{account.version}/{self.flow_id}?fields=id,name,status,categories,json_version,data_api_version,data_channel_uri,preview"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            data = response.json()

            # Update local fields
            if data.get("status"):
                self.status = data["status"].title()
            if data.get("categories"):
                self.category = data["categories"][0] if data["categories"] else "OTHER"
            if data.get("json_version"):
                self.flow_json_version = data["json_version"]
            if data.get("data_api_version"):
                self.data_api_version = data["data_api_version"]
            if data.get("data_channel_uri"):
                self.endpoint_uri = data["data_channel_uri"]
            if data.get("preview", {}).get("preview_url"):
                self.preview_url = data["preview"]["preview_url"]

            # Try to get the flow JSON
            flow_json = self.fetch_flow_json()
            if flow_json and self.authoring_mode == "Raw JSON":
                self.flow_json = json.dumps(flow_json, indent=2)

            self.save()
            frappe.msgprint(_("Flow synced successfully from WhatsApp"), indicator="green")

            return data

        except Exception as e:
            frappe.throw(_("Failed to sync flow: {0}").format(str(e)))

    def fetch_flow_json(self):
        """Fetch the flow JSON from WhatsApp."""
        if not self.flow_id:
            return None

        account = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        token = account.get_password("token")

        url = f"{account.url}/{account.version}/{self.flow_id}/assets"

        headers = {
            "Authorization": f"Bearer {token}"
        }

        try:
            import requests
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            data = response.json()
            assets = data.get("data", [])

            # Find the flow.json asset
            for asset in assets:
                if asset.get("name") == "flow.json":
                    # Download the asset
                    download_url = asset.get("download_url")
                    if download_url:
                        asset_response = requests.get(download_url, headers=headers)
                        if asset_response.status_code == 200:
                            return asset_response.json()

            return None

        except Exception as e:
            frappe.log_error(f"Failed to fetch flow JSON: {str(e)}")
            return None


@frappe.whitelist()
def get_whatsapp_flows(whatsapp_account):
    """Get list of all flows from WhatsApp Business Account.

    Args:
        whatsapp_account: Name of WhatsApp Account document

    Returns:
        List of flows from WhatsApp
    """
    account = frappe.get_doc("WhatsApp Account", whatsapp_account)
    token = account.get_password("token")

    url = f"{account.url}/{account.version}/{account.business_id}/flows?fields=id,name,status,categories,json_version,data_api_version,data_channel_uri"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    try:
        import requests
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        flows = data.get("data", [])

        # Check which flows already exist locally
        for flow in flows:
            existing = frappe.db.exists("WhatsApp Flow", {"flow_id": flow["id"]})
            flow["exists_locally"] = bool(existing)
            flow["local_name"] = existing if existing else None

        return flows

    except Exception as e:
        frappe.throw(_("Failed to fetch flows: {0}").format(str(e)))


@frappe.whitelist()
def import_flow_from_whatsapp(whatsapp_account, flow_id, flow_name=None):
    """Import a flow from WhatsApp into Frappe.

    Args:
        whatsapp_account: Name of WhatsApp Account document
        flow_id: WhatsApp Flow ID to import
        flow_name: Optional name for the flow (uses WhatsApp name if not provided)

    Returns:
        Name of created WhatsApp Flow document
    """
    # Check if flow already exists
    existing = frappe.db.exists("WhatsApp Flow", {"flow_id": flow_id})
    if existing:
        frappe.throw(_("Flow already exists: {0}").format(existing))

    account = frappe.get_doc("WhatsApp Account", whatsapp_account)
    token = account.get_password("token")

    # Get flow details
    url = f"{account.url}/{account.version}/{flow_id}?fields=id,name,status,categories,json_version,data_api_version,data_channel_uri,preview"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    try:
        import requests
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()

        # Create the flow document
        flow_doc = frappe.get_doc({
            "doctype": "WhatsApp Flow",
            "flow_name": flow_name or data.get("name", f"Imported Flow {flow_id}"),
            "whatsapp_account": whatsapp_account,
            "flow_id": flow_id,
            "status": data.get("status", "Draft").title(),
            "authoring_mode": "Raw JSON",
            "category": data["categories"][0] if data.get("categories") else "OTHER",
            "flow_json_version": data.get("json_version", "7.3"),
            "data_api_version": data.get("data_api_version", "3.0"),
            "endpoint_uri": data.get("data_channel_uri", ""),
            "preview_url": data.get("preview", {}).get("preview_url", "")
        })

        # Try to fetch and parse the flow JSON to create screens/fields
        flow_json = fetch_flow_json_by_id(whatsapp_account, flow_id)
        if flow_json:
            flow_doc.flow_json = json.dumps(flow_json, indent=2)
            parse_flow_json_to_screens(flow_doc, flow_json)

        # Skip validation since we're importing (may not have screens)
        flow_doc.flags.ignore_validate = True
        flow_doc.insert(ignore_permissions=True)

        frappe.msgprint(_("Flow imported successfully: {0}").format(flow_doc.name), indicator="green")

        return flow_doc.name

    except Exception as e:
        frappe.throw(_("Failed to import flow: {0}").format(str(e)))


def fetch_flow_json_by_id(whatsapp_account, flow_id):
    """Fetch flow JSON by flow ID."""
    account = frappe.get_doc("WhatsApp Account", whatsapp_account)
    token = account.get_password("token")

    url = f"{account.url}/{account.version}/{flow_id}/assets"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    try:
        import requests
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        assets = data.get("data", [])

        for asset in assets:
            if asset.get("name") == "flow.json":
                download_url = asset.get("download_url")
                if download_url:
                    asset_response = requests.get(download_url, headers=headers)
                    if asset_response.status_code == 200:
                        return asset_response.json()

        return None

    except Exception as e:
        frappe.log_error(f"Failed to fetch flow JSON: {str(e)}")
        return None


@frappe.whitelist()
def sync_all_flows(whatsapp_account):
    """Sync all flows from WhatsApp Business Account.

    Imports new flows and updates existing ones.

    Args:
        whatsapp_account: Name of WhatsApp Account document

    Returns:
        Dict with counts: imported, updated, skipped
    """
    account = frappe.get_doc("WhatsApp Account", whatsapp_account)
    token = account.get_password("token")

    url = f"{account.url}/{account.version}/{account.business_id}/flows?fields=id,name,status,categories,json_version,data_api_version,data_channel_uri"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    result = {"imported": 0, "updated": 0, "skipped": 0}

    try:
        import requests
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        flows = data.get("data", [])

        for flow in flows:
            flow_id = flow.get("id")
            flow_name = flow.get("name")

            # Check if flow exists locally
            existing = frappe.db.exists("WhatsApp Flow", {"flow_id": flow_id})

            if existing:
                # Update existing flow
                try:
                    flow_doc = frappe.get_doc("WhatsApp Flow", existing)
                    flow_doc.status = flow.get("status", "Draft").title()
                    if flow.get("categories"):
                        flow_doc.category = flow["categories"][0]
                    if flow.get("data_channel_uri"):
                        flow_doc.endpoint_uri = flow["data_channel_uri"]
                    if flow.get("json_version"):
                        flow_doc.flow_json_version = flow["json_version"]
                    if flow.get("data_api_version"):
                        flow_doc.data_api_version = flow["data_api_version"]

                    # Try to fetch flow JSON and update screens/fields
                    flow_json = fetch_flow_json_by_id(whatsapp_account, flow_id)
                    if flow_json:
                        flow_doc.flow_json = json.dumps(flow_json, indent=2)
                        flow_doc.flow_json_version = flow_json.get("version", "7.3")
                        flow_doc.data_api_version = flow_json.get(
                            "data_api_version", flow_doc.data_api_version or "3.0"
                        )
                        if flow_doc.authoring_mode == "Raw JSON":
                            # Keep a read-only summary while preserving the full asset.
                            flow_doc.screens = []
                            flow_doc.fields = []
                            flow_doc.routes = []
                            parse_flow_json_to_screens(flow_doc, flow_json)

                    flow_doc.flags.ignore_validate = True
                    flow_doc.save(ignore_permissions=True)
                    result["updated"] += 1
                except Exception as e:
                    frappe.log_error(f"Failed to update flow {flow_id}: {str(e)}")
                    result["skipped"] += 1
            else:
                # Import new flow
                try:
                    flow_doc = frappe.get_doc({
                        "doctype": "WhatsApp Flow",
                        "flow_name": flow_name or f"Flow {flow_id}",
                        "whatsapp_account": whatsapp_account,
                        "flow_id": flow_id,
                        "status": flow.get("status", "Draft").title(),
                        "authoring_mode": "Raw JSON",
                        "category": flow["categories"][0] if flow.get("categories") else "OTHER",
                        "flow_json_version": flow.get("json_version", "7.3"),
                        "data_api_version": flow.get("data_api_version", "3.0"),
                        "endpoint_uri": flow.get("data_channel_uri", "")
                    })

                    # Try to fetch and parse flow JSON
                    flow_json = fetch_flow_json_by_id(whatsapp_account, flow_id)
                    if flow_json:
                        flow_doc.flow_json = json.dumps(flow_json, indent=2)
                        flow_doc.flow_json_version = flow_json.get("version", "7.3")
                        flow_doc.data_api_version = flow_json.get(
                            "data_api_version", flow_doc.data_api_version or "3.0"
                        )
                        parse_flow_json_to_screens(flow_doc, flow_json)

                    flow_doc.flags.ignore_validate = True
                    flow_doc.insert(ignore_permissions=True)
                    result["imported"] += 1
                except Exception as e:
                    frappe.log_error(f"Failed to import flow {flow_id}: {str(e)}")
                    result["skipped"] += 1

        return result

    except Exception as e:
        frappe.throw(_("Failed to sync flows: {0}").format(str(e)))


def parse_flow_json_to_screens(flow_doc, flow_json):
    """Parse flow JSON and create screens and fields in the document.

    Args:
        flow_doc: WhatsApp Flow document
        flow_json: Parsed flow JSON dict
    """
    screens = flow_json.get("screens", [])

    for screen_data in screens:
        # Add screen
        flow_doc.append("screens", {
            "screen_id": screen_data.get("id"),
            "screen_title": screen_data.get("title", ""),
            "terminal": 1 if screen_data.get("terminal") else 0,
            "refresh_on_back": 1 if screen_data.get("refresh_on_back") else 0
        })

        # Parse fields from layout
        layout = screen_data.get("layout", {})
        children = layout.get("children", [])

        for child in children:
            field_type = child.get("type")
            if not field_type:
                continue

            # Map WhatsApp field types to our field types
            field_data = {
                "screen": screen_data.get("id"),
                "field_type": field_type,
                "field_name": child.get("name", field_type.lower()),
                "label": child.get("label") or child.get("text", ""),
                "required": 1 if child.get("required") else 0,
                "enabled": 1,
                "helper_text": child.get("helper-text", ""),
                "init_value": child.get("init-value", ""),
                "min_chars": child.get("min-chars"),
                "max_chars": child.get("max-chars"),
                "error_message": child.get("error-message", "")
            }

            # Handle options for dropdowns
            if field_type in ["Dropdown", "RadioButtonsGroup", "CheckboxGroup"]:
                data_source = child.get("data-source", [])
                if data_source:
                    field_data["options"] = json.dumps(data_source)

            flow_doc.append("fields", field_data)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_available_flows(doctype, txt, searchfield, start, page_len, filters):
    """Link query for Flows that the selected sending phone number may use."""
    frappe.has_permission("WhatsApp Flow", "read", throw=True)
    whatsapp_account = (filters or {}).get("whatsapp_account")
    if not whatsapp_account:
        return []

    business_id = frappe.db.get_value("WhatsApp Account", whatsapp_account, "business_id")
    accounts = [whatsapp_account]
    if business_id:
        accounts = frappe.get_all(
            "WhatsApp Account", filters={"business_id": business_id}, pluck="name"
        )
    if not accounts:
        return []

    placeholders = ", ".join(["%s"] * len(accounts))
    search_text = f"%{txt}%"
    return frappe.db.sql(
        f"""
        SELECT DISTINCT flow.name, flow.flow_name, flow.status
        FROM `tabWhatsApp Flow` flow
        LEFT JOIN `tabWhatsApp Flow Account` allowed
            ON allowed.parent = flow.name
            AND allowed.parenttype = 'WhatsApp Flow'
            AND allowed.parentfield = 'available_accounts'
        WHERE flow.whatsapp_account IN ({placeholders})
          AND (
            COALESCE(flow.availability_mode, 'All Phone Numbers in WABA') = 'All Phone Numbers in WABA'
            OR allowed.whatsapp_account = %s
          )
          AND (flow.name LIKE %s OR flow.flow_name LIKE %s)
        ORDER BY flow.flow_name
        LIMIT %s OFFSET %s
        """,
        [*accounts, whatsapp_account, search_text, search_text, page_len, start]
    )
