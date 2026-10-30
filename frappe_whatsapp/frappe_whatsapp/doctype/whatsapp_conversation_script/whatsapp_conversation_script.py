# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import json
import re

import frappe
import regex as safe_regex
from frappe import _
from frappe.model.document import Document


STEP_FIELDS = (
	"step_id",
	"action_type",
	"message",
	"wait_for_reply",
	"options",
	"flow",
	"flow_action",
	"handler_key",
)
TRANSITION_FIELDS = (
	"source_step",
	"match_type",
	"match_value",
	"destination_step",
	"priority",
	"capture_as",
)
LIFECYCLE_FIELDS = (
	"idle_timeout_minutes",
	"maximum_lifetime_minutes",
	"maximum_message_count",
	"restart_keywords",
	"cancel_keywords",
	"unmatched_message_policy",
)
DEFAULT_SESSION_CONFIG = {
	"idle_timeout_minutes": 1440,
	"maximum_lifetime_minutes": 10080,
	"maximum_message_count": 50,
	"restart_keywords": ["restart", "start over"],
	"cancel_keywords": ["cancel", "stop"],
	"unmatched_message_policy": "Retry",
}
MAX_REGEX_LENGTH = 512
TEXT_MESSAGE_MAX_LENGTH = 4096
INTERACTIVE_BODY_MAX_LENGTH = 1024
REPLY_BUTTON_ID_MAX_LENGTH = 256
REPLY_BUTTON_TITLE_MAX_LENGTH = 20
LIST_ROW_ID_MAX_LENGTH = 200
LIST_ROW_TITLE_MAX_LENGTH = 24
LIST_ROW_DESCRIPTION_MAX_LENGTH = 72


class WhatsAppConversationScript(Document):
	def validate(self):
		self.sync_authoring_representations()
		self.validate_entry_trigger()
		self.validate_lifecycle()
		self.validate_steps_and_transitions()
		self.validate_available_accounts()

	def before_save(self):
		self.script_json = json.dumps(self.generate_script_json(), indent=2)

	def get_script_json(self):
		try:
			value = json.loads(self.script_json or "{}")
		except (TypeError, json.JSONDecodeError) as exc:
			frappe.throw(_("Conversation Script JSON is invalid: {0}").format(str(exc)))
		if not isinstance(value, dict):
			frappe.throw(_("Conversation Script JSON must be an object"))
		return value

	def generate_script_json(self):
		return {
			"version": self.version or 1,
			"entry": {
				"match": self.entry_match_type,
				"value": self.entry_value or "",
			},
			"start_step": self.start_step,
			"session": {
				"idle_timeout_minutes": self.idle_timeout_minutes or 0,
				"maximum_lifetime_minutes": self.maximum_lifetime_minutes or 0,
				"maximum_message_count": self.maximum_message_count or 0,
				"restart_keywords": parse_keywords(self.restart_keywords),
				"cancel_keywords": parse_keywords(self.cancel_keywords),
				"unmatched_message_policy": self.unmatched_message_policy or "Retry",
			},
			"steps": [
				{
					"id": step.step_id,
					"action": step.action_type,
					"message": step.message or "",
					"wait_for_reply": bool(step.wait_for_reply),
					"options": self.parse_json_value(step.options, _("Options"), []),
					"flow": step.flow or "",
					"flow_action": step.flow_action or "navigate",
					"handler_key": step.handler_key or "",
				}
				for step in self.steps
			],
			"transitions": [
				{
					"from": transition.source_step,
					"match": transition.match_type,
					"value": transition.match_value or "",
					"to": transition.destination_step,
					"priority": transition.priority or 10,
					"capture_as": transition.capture_as or "",
				}
				for transition in self.transitions
			],
		}

	def import_script_json(self):
		value = self.get_script_json()
		entry = value.get("entry") or {}
		session = value.get("session") or {}
		steps = value.get("steps")
		transitions = value.get("transitions")
		if (
			not isinstance(entry, dict)
			or not isinstance(session, dict)
			or not isinstance(steps, list)
			or not isinstance(transitions, list)
		):
			frappe.throw(_("Conversation Script JSON requires entry, steps, and transitions"))
		session = {**DEFAULT_SESSION_CONFIG, **session}
		if not isinstance(session["restart_keywords"], list) or not isinstance(
			session["cancel_keywords"], list
		):
			frappe.throw(_("Conversation Script JSON restart and cancel keywords must be arrays"))
		if any(
			not isinstance(keyword, str)
			for keyword in session["restart_keywords"] + session["cancel_keywords"]
		):
			frappe.throw(_("Conversation Script JSON keywords must be strings"))

		self.version = value.get("version") or 1
		self.entry_match_type = entry.get("match")
		self.entry_value = entry.get("value") or ""
		self.start_step = value.get("start_step")
		self.idle_timeout_minutes = session["idle_timeout_minutes"]
		self.maximum_lifetime_minutes = session["maximum_lifetime_minutes"]
		self.maximum_message_count = session["maximum_message_count"]
		self.restart_keywords = "\n".join(session["restart_keywords"])
		self.cancel_keywords = "\n".join(session["cancel_keywords"])
		self.unmatched_message_policy = session.get("unmatched_message_policy") or "Retry"
		self.set("steps", [])
		self.set("transitions", [])
		for step in steps:
			if not isinstance(step, dict):
				frappe.throw(_("Every conversation step must be an object"))
			self.append(
				"steps",
				{
					"step_id": step.get("id"),
					"action_type": step.get("action"),
					"message": step.get("message") or "",
					"wait_for_reply": int(bool(step.get("wait_for_reply"))),
					"options": json.dumps(step.get("options") or []),
					"flow": step.get("flow") or None,
					"flow_action": step.get("flow_action") or "navigate",
					"handler_key": step.get("handler_key") or None,
				},
			)
		for transition in transitions:
			if not isinstance(transition, dict):
				frappe.throw(_("Every conversation transition must be an object"))
			self.append(
				"transitions",
				{
					"source_step": transition.get("from"),
					"match_type": transition.get("match"),
					"match_value": transition.get("value") or "",
					"destination_step": transition.get("to"),
					"priority": transition.get("priority") or 10,
					"capture_as": transition.get("capture_as") or "",
				},
			)

		normalized_value = dict(value)
		normalized_value["session"] = session
		if self.generate_script_json() != normalized_value:
			frappe.throw(
				_("Conversation Script JSON cannot be represented losslessly by the structured editor")
			)

	def sync_authoring_representations(self):
		previous = self.get_doc_before_save()
		if not previous:
			if self.script_json:
				self.import_script_json()
			else:
				self.script_json = json.dumps(self.generate_script_json(), indent=2)
			return

		json_changed = not json_values_match(self.script_json, previous.script_json)
		structured_changed = structured_state(self) != structured_state(previous)
		if json_changed:
			self.import_script_json()
		elif structured_changed:
			self.script_json = json.dumps(self.generate_script_json(), indent=2)

	def validate_entry_trigger(self):
		if self.entry_match_type not in ("Exact", "Contains", "Regex", "Content Type", "Any"):
			frappe.throw(_("Select a valid entry trigger match type"))
		if self.entry_match_type != "Any" and not self.entry_value:
			frappe.throw(_("Entry trigger value is required"))
		if self.entry_match_type == "Regex":
			validate_regex(self.entry_value, _("Entry trigger"))

	def validate_lifecycle(self):
		for fieldname, label in (
			("idle_timeout_minutes", _("Idle Timeout")),
			("maximum_lifetime_minutes", _("Maximum Lifetime")),
			("maximum_message_count", _("Maximum Message Count")),
		):
			if (self.get(fieldname) or 0) < 0:
				frappe.throw(_("{0} cannot be negative").format(label))
		if self.unmatched_message_policy not in ("Retry", "Restart", "Handoff", "Close"):
			frappe.throw(_("Select a valid unmatched-message policy"))
		restart = set(parse_keywords(self.restart_keywords))
		cancel = set(parse_keywords(self.cancel_keywords))
		if restart & cancel:
			frappe.throw(_("Restart and cancel keywords cannot overlap"))

	def validate_steps_and_transitions(self):
		if not self.steps:
			frappe.throw(_("Add at least one conversation step"))
		step_ids = [step.step_id for step in self.steps]
		if any(not step_id for step_id in step_ids) or len(step_ids) != len(set(step_ids)):
			frappe.throw(_("Conversation step IDs must be present and unique"))
		if self.start_step not in step_ids:
			frappe.throw(_("Start Step must reference an existing step"))

		transitions_by_source = {}
		for transition in self.transitions:
			if transition.source_step not in step_ids or transition.destination_step not in step_ids:
				frappe.throw(_("Every transition must reference existing steps"))
			if transition.match_type not in ("Exact", "Contains", "Regex", "Content Type", "Any", "Default"):
				frappe.throw(_("Select a valid transition match type"))
			if transition.match_type not in ("Any", "Default") and not transition.match_value:
				frappe.throw(_("Transition match value is required"))
			if transition.match_type == "Regex":
				validate_regex(transition.match_value, _("Transition"))
			transitions_by_source.setdefault(transition.source_step, []).append(transition)

		for source, transitions in transitions_by_source.items():
			if sum(1 for row in transitions if row.match_type == "Default") > 1:
				frappe.throw(_("Step {0} has more than one default transition").format(source))

		for step in self.steps:
			if step.action_type not in (
				"Send Text", "Send Options", "Launch Flow", "Run Handler", "Handoff", "End"
			):
				frappe.throw(_("Step {0} has an invalid action").format(step.step_id))
			if step.action_type == "Send Options":
				options = self.parse_json_value(step.options, _("Options"), [])
				if not isinstance(options, list) or not 1 <= len(options) <= 10:
					frappe.throw(_("Send Options requires between 1 and 10 options"))
				validate_options(options, step.step_id)
			if step.action_type == "Send Text":
				validate_message_length(step.message, step.step_id, TEXT_MESSAGE_MAX_LENGTH)
			if step.action_type in ("Send Options", "Launch Flow"):
				validate_message_length(step.message, step.step_id, INTERACTIVE_BODY_MAX_LENGTH)
			if step.action_type == "Launch Flow" and not step.flow:
				frappe.throw(_("Launch Flow step {0} requires a Flow").format(step.step_id))
			if step.action_type == "Run Handler":
				if not step.handler_key or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,139}", step.handler_key):
					frappe.throw(_("Run Handler step {0} requires a valid handler key").format(step.step_id))
			if (
				step.action_type not in ("End", "Handoff", "Run Handler")
				and step.step_id not in transitions_by_source
			):
				frappe.throw(_("Step {0} has no outgoing transition").format(step.step_id))

		graph = {
			source: {row.destination_step for row in rows}
			for source, rows in transitions_by_source.items()
		}
		reachable = set()
		pending = [self.start_step]
		while pending:
			step_id = pending.pop()
			if step_id in reachable:
				continue
			reachable.add(step_id)
			pending.extend(graph.get(step_id, set()))
		unreachable = set(step_ids) - reachable
		if unreachable:
			frappe.throw(_("Unreachable conversation steps: {0}").format(", ".join(sorted(unreachable))))

	def validate_available_accounts(self):
		owner_business_id = frappe.db.get_value("WhatsApp Account", self.whatsapp_account, "business_id")
		selected = [row.whatsapp_account for row in self.available_accounts]
		if len(selected) != len(set(selected)):
			frappe.throw(_("The same phone number cannot be selected more than once"))
		if self.availability_mode == "Selected Phone Numbers" and not selected:
			frappe.throw(_("Select at least one phone number for this script"))
		for account_name in selected:
			business_id = frappe.db.get_value("WhatsApp Account", account_name, "business_id")
			if account_name != self.whatsapp_account and not (
				owner_business_id and business_id == owner_business_id
			):
				frappe.throw(_("WhatsApp Account {0} does not belong to the script's WABA").format(account_name))

	def is_available_on(self, whatsapp_account):
		owner_business_id = frappe.db.get_value("WhatsApp Account", self.whatsapp_account, "business_id")
		account_business_id = frappe.db.get_value("WhatsApp Account", whatsapp_account, "business_id")
		if whatsapp_account != self.whatsapp_account and not (
			owner_business_id and owner_business_id == account_business_id
		):
			return False
		if self.availability_mode != "Selected Phone Numbers":
			return True
		return whatsapp_account in {row.whatsapp_account for row in self.available_accounts}

	@staticmethod
	def parse_json_value(value, label, default):
		if not value:
			return default
		try:
			return json.loads(value)
		except (TypeError, json.JSONDecodeError) as exc:
			frappe.throw(_("{0} is invalid JSON: {1}").format(label, str(exc)))


def structured_state(doc):
	return {
		"version": doc.get("version"),
		"entry_match_type": doc.get("entry_match_type"),
		"entry_value": doc.get("entry_value"),
		"start_step": doc.get("start_step"),
		**{fieldname: doc.get(fieldname) for fieldname in LIFECYCLE_FIELDS},
		"steps": [
			{fieldname: row.get(fieldname) for fieldname in STEP_FIELDS}
			for row in (doc.get("steps") or [])
		],
		"transitions": [
			{fieldname: row.get(fieldname) for fieldname in TRANSITION_FIELDS}
			for row in (doc.get("transitions") or [])
		],
	}


def json_values_match(left, right):
	try:
		return json.loads(left or "{}") == json.loads(right or "{}")
	except (TypeError, json.JSONDecodeError):
		return left == right


def validate_regex(pattern, label):
	if len(pattern or "") > MAX_REGEX_LENGTH:
		frappe.throw(_("{0} regex cannot exceed {1} characters").format(label, MAX_REGEX_LENGTH))
	try:
		safe_regex.compile(pattern, flags=safe_regex.VERSION0)
	except safe_regex.error as exc:
		frappe.throw(_("{0} regex is invalid: {1}").format(label, str(exc)))


def validate_message_length(message, step_id, maximum):
	if not message:
		frappe.throw(_("Step {0} requires a message").format(step_id))
	if len(message) > maximum:
		frappe.throw(
			_("Step {0} message exceeds the WhatsApp limit of {1} characters").format(
				step_id, maximum
			)
		)


def validate_options(options, step_id):
	button_mode = len(options) <= 3
	seen_ids = set()
	for index, option in enumerate(options, start=1):
		if not isinstance(option, dict):
			frappe.throw(_("Option {0} in step {1} must be an object").format(index, step_id))
		option_id = option.get("id")
		title = option.get("title")
		description = option.get("description", "")
		if not isinstance(option_id, str) or not option_id.strip():
			frappe.throw(_("Option {0} in step {1} requires a string ID").format(index, step_id))
		if not isinstance(title, str) or not title.strip():
			frappe.throw(_("Option {0} in step {1} requires a string title").format(index, step_id))
		if not isinstance(description, str):
			frappe.throw(_("Option {0} in step {1} description must be a string").format(index, step_id))
		if option_id in seen_ids:
			frappe.throw(_("Option IDs must be unique in step {0}").format(step_id))
		seen_ids.add(option_id)
		id_limit = REPLY_BUTTON_ID_MAX_LENGTH if button_mode else LIST_ROW_ID_MAX_LENGTH
		title_limit = REPLY_BUTTON_TITLE_MAX_LENGTH if button_mode else LIST_ROW_TITLE_MAX_LENGTH
		if len(option_id) > id_limit:
			frappe.throw(_("Option {0} ID exceeds the WhatsApp limit of {1} characters").format(index, id_limit))
		if len(title) > title_limit:
			frappe.throw(_("Option {0} title exceeds the WhatsApp limit of {1} characters").format(index, title_limit))
		if button_mode and description:
			frappe.throw(_("Descriptions require at least four options so WhatsApp uses a list"))
		if len(description) > LIST_ROW_DESCRIPTION_MAX_LENGTH:
			frappe.throw(
				_("Option {0} description exceeds the WhatsApp limit of {1} characters").format(
					index, LIST_ROW_DESCRIPTION_MAX_LENGTH
				)
			)


def parse_keywords(value):
	if isinstance(value, list):
		values = value
	else:
		values = (value or "").splitlines()
	return list(dict.fromkeys(str(keyword).strip().casefold() for keyword in values if str(keyword).strip()))
