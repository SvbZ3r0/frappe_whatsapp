# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

"""Encrypted WhatsApp Flow Data Exchange protocol endpoint."""

import base64
import binascii
import hashlib
import hmac
import json
import re

import frappe
from frappe import _
from frappe.utils import cint
from werkzeug.wrappers import Response

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_flow_exchange.whatsapp_flow_exchange import (
	FlowExchangeInProgressError,
	FlowExchangeReplayUnavailableError,
	begin_exchange,
	complete_exchange,
	fail_exchange,
)


class FlowProtocolError(Exception):
	"""The request does not satisfy the WhatsApp Flow protocol."""


class FlowDecryptionError(FlowProtocolError):
	"""No configured account could decrypt the request."""


class FlowSignatureError(FlowProtocolError):
	"""Meta request signature validation failed."""


class FlowTokenError(FlowProtocolError):
	"""The request is not correlated to a persisted Flow launch."""


@frappe.whitelist(allow_guest=True)
def handle_flow_request(flow=None, whatsapp_account=None):
	"""Decrypt, normalize, dispatch, and encrypt a WhatsApp Flow request.

	The optional ``flow`` and ``whatsapp_account`` query parameters are hints
	that make account/Flow resolution deterministic for pings. The encryption
	key and persisted flow token remain authoritative.
	"""
	if frappe.request.method == "GET":
		return Response("ok", status=200, content_type="text/plain")

	raw_body = frappe.request.get_data(cache=True)
	diagnostics = {}
	try:
		try:
			envelope = json.loads(raw_body)
		except (json.JSONDecodeError, UnicodeDecodeError) as exc:
			raise FlowProtocolError("Encrypted request body must be valid JSON") from exc
		validate_envelope(envelope)
		decrypted, aes_key, initial_vector, account = decrypt_and_identify_account(
			envelope,
			flow_hint=flow,
			account_hint=whatsapp_account,
		)
		diagnostics.update(
			{
				"whatsapp_account": account.name,
				"action": decrypted.get("action"),
				"screen": decrypted.get("screen"),
			}
		)
		validate_request_signature(raw_body, account)

		flow_doc = resolve_flow(decrypted, account, flow)
		if flow_doc:
			diagnostics["flow"] = flow_doc.name
		validate_data_api_version(decrypted, flow_doc)
		context = normalize_context(decrypted, flow_doc, account)
		if context["action"] == "ping":
			ordinary_response = dispatch_data_exchange(context, flow_doc)
			ordinary_response = format_protocol_response(
				ordinary_response, decrypted, flow_doc
			)
		else:
			request_fingerprint = hashlib.sha256(raw_body).hexdigest()
			session, exchange, cached_response = begin_exchange(
				context, flow_doc, request_fingerprint
			)
			if cached_response is not None:
				ordinary_response = cached_response
			else:
				try:
					ordinary_response = dispatch_data_exchange(context, flow_doc)
					ordinary_response = format_protocol_response(
						ordinary_response, decrypted, flow_doc
					)
					complete_exchange(session, exchange, ordinary_response)
				except Exception as exc:
					fail_exchange(session, exchange, exc)
					raise
		encrypted_response = encrypt_response(
			ordinary_response, aes_key, initial_vector
		)
		return Response(encrypted_response, status=200, content_type="text/plain")
	except FlowSignatureError as exc:
		log_protocol_error(exc, "WhatsApp Flow Signature Error", diagnostics)
		return Response("", status=432, content_type="text/plain")
	except FlowTokenError as exc:
		log_protocol_error(exc, "WhatsApp Flow Token Error", diagnostics)
		return Response("", status=427, content_type="text/plain")
	except FlowDecryptionError as exc:
		log_protocol_error(exc, "WhatsApp Flow Decryption Error", diagnostics)
		return Response("", status=421, content_type="text/plain")
	except FlowExchangeInProgressError as exc:
		log_protocol_error(exc, "WhatsApp Flow Exchange In Progress", diagnostics)
		return Response("", status=409, content_type="text/plain")
	except FlowExchangeReplayUnavailableError as exc:
		log_protocol_error(exc, "WhatsApp Flow Exchange Replay Blocked", diagnostics)
		return Response("", status=409, content_type="text/plain")
	except FlowProtocolError as exc:
		log_protocol_error(exc, "WhatsApp Flow Protocol Error", diagnostics)
		return Response("", status=400, content_type="text/plain")
	except Exception as exc:
		# Handler exceptions are untrusted and may embed decrypted customer data.
		log_protocol_error(
			exc,
			"WhatsApp Flow Handler Error",
			diagnostics,
			include_message=False,
		)
		return Response("", status=500, content_type="text/plain")


def validate_envelope(envelope):
	if not isinstance(envelope, dict):
		raise FlowProtocolError("Encrypted request body must be a JSON object")
	for fieldname in ("encrypted_aes_key", "encrypted_flow_data", "initial_vector"):
		if not isinstance(envelope.get(fieldname), str) or not envelope[fieldname]:
			raise FlowProtocolError(f"Missing encrypted envelope field: {fieldname}")


def decrypt_and_identify_account(envelope, flow_hint=None, account_hint=None):
	"""Prove account identity by decrypting with its configured private key."""
	candidate_names = []
	if account_hint and frappe.db.exists("WhatsApp Account", account_hint):
		candidate_names.append(account_hint)
	if flow_hint:
		flow_account = frappe.db.get_value("WhatsApp Flow", flow_hint, "whatsapp_account")
		if flow_account and flow_account not in candidate_names:
			candidate_names.append(flow_account)

	configured_accounts = frappe.get_all(
		"WhatsApp Account",
		or_filters=[
			["flow_public_key", "!=", ""],
			["flow_pending_public_key", "!=", ""],
			["flow_previous_public_key", "!=", ""],
		],
		pluck="name",
	)
	for account_name in configured_accounts:
		if account_name not in candidate_names:
			candidate_names.append(account_name)

	for account_name in candidate_names:
		account = frappe.get_doc("WhatsApp Account", account_name)
		for private_key, passphrase in get_flow_decryption_keys(account):
			try:
				decrypted, aes_key, initial_vector = decrypt_request(
					envelope, private_key, passphrase
				)
				return decrypted, aes_key, initial_vector, account
			except (ValueError, TypeError, InvalidTag, binascii.Error):
				continue

	raise FlowDecryptionError("Unable to decrypt request with a configured account key")


def get_flow_decryption_keys(account):
	"""Return active, pending, and retained keys for interruption-free rotation."""
	key_fields = (
		("flow_private_key", "flow_private_key_passphrase"),
		("flow_pending_private_key", "flow_pending_private_key_passphrase"),
		("flow_previous_private_key", "flow_previous_private_key_passphrase"),
	)
	keys = []
	for private_key_field, passphrase_field in key_fields:
		private_key = account.get_password(private_key_field, raise_exception=False)
		if not private_key:
			continue
		passphrase = account.get_password(passphrase_field, raise_exception=False)
		keys.append((private_key, passphrase))
	return keys


def decrypt_request(envelope, private_key_pem, passphrase=None):
	"""Decrypt Meta's RSA-OAEP/AES-GCM request envelope."""
	password = passphrase.encode() if passphrase else None
	private_key = serialization.load_pem_private_key(
		private_key_pem.encode(), password=password
	)
	encrypted_aes_key = base64.b64decode(
		envelope["encrypted_aes_key"], validate=True
	)
	aes_key = private_key.decrypt(
		encrypted_aes_key,
		padding.OAEP(
			mgf=padding.MGF1(algorithm=hashes.SHA256()),
			algorithm=hashes.SHA256(),
			label=None,
		),
	)
	initial_vector = base64.b64decode(envelope["initial_vector"], validate=True)
	encrypted_flow_data = base64.b64decode(
		envelope["encrypted_flow_data"], validate=True
	)
	plaintext = AESGCM(aes_key).decrypt(
		initial_vector, encrypted_flow_data, None
	)
	decrypted = json.loads(plaintext)
	if not isinstance(decrypted, dict):
		raise FlowProtocolError("Decrypted Flow request must be a JSON object")
	return decrypted, aes_key, initial_vector


def validate_request_signature(raw_body, account):
	"""Validate X-Hub-Signature-256 when supplied or required by the account."""
	signature = frappe.request.headers.get("X-Hub-Signature-256")
	required = is_request_signature_required(account)
	if not signature and not required:
		return

	app_secret = account.get_password("app_secret", raise_exception=False)
	if not signature or not app_secret:
		raise FlowSignatureError("Missing Meta request signature configuration")
	if not verify_signature(raw_body, signature, app_secret):
		raise FlowSignatureError("Invalid Meta request signature")


def is_request_signature_required(account):
	"""Require Meta authentication in production; allow an explicit dev opt-out."""
	developer_mode = bool(cint(frappe.conf.get("developer_mode")))
	return bool(account.require_flow_request_signature) or not developer_mode


def verify_signature(payload, signature, app_secret):
	"""Constant-time validation of Meta's X-Hub-Signature-256 header."""
	if isinstance(payload, str):
		payload = payload.encode()
	expected = "sha256=" + hmac.new(
		app_secret.encode(), payload, hashlib.sha256
	).hexdigest()
	return hmac.compare_digest(signature, expected)


def resolve_flow(decrypted, account, flow_hint=None):
	"""Resolve pings by hint and all business actions by persisted launch token."""
	action = decrypted.get("action")
	if action == "ping":
		if not flow_hint:
			return None
		flow_name = flow_hint
	else:
		flow_token = decrypted.get("flow_token")
		if not flow_token:
			raise FlowTokenError("Flow token is required")

		matches = frappe.get_all(
			"WhatsApp Message",
			filters={
				"type": "Outgoing",
				"content_type": "flow",
				"flow_token": flow_token,
				"whatsapp_account": account.name,
			},
			fields=["flow"],
			order_by="creation desc",
			limit=1,
		)
		if not matches or not matches[0].flow:
			raise FlowTokenError("Flow token does not match a persisted launch")
		flow_name = matches[0].flow
		if flow_hint and flow_hint != flow_name:
			raise FlowTokenError("Flow hint does not match the persisted launch token")

	if not frappe.db.exists("WhatsApp Flow", flow_name):
		raise FlowProtocolError("Unknown WhatsApp Flow")

	flow_doc = frappe.get_doc("WhatsApp Flow", flow_name)
	if not flow_doc.is_available_on(account.name):
		raise FlowProtocolError("Flow is not available on the identified account")
	return flow_doc


def normalize_context(decrypted, flow_doc, account):
	data = decrypted.get("data") or {}
	if not isinstance(data, dict):
		raise FlowProtocolError("Flow request data must be a JSON object")
	action = decrypted.get("action")
	if action not in ("INIT", "data_exchange", "BACK", "ping"):
		raise FlowProtocolError("Unsupported Flow action")
	return {
		"flow": flow_doc.name if flow_doc else None,
		"whatsapp_account": account.name,
		"action": action,
		"screen": decrypted.get("screen"),
		"flow_token": decrypted.get("flow_token"),
		"data": data,
	}


def validate_data_api_version(decrypted, flow_doc):
	"""Reject requests using a protocol version different from the Flow."""
	if not flow_doc:
		return
	if not decrypted.get("version"):
		raise FlowProtocolError("Flow Data API version is required")
	expected = flow_doc.data_api_version or "3.0"
	if decrypted["version"] != expected:
		raise FlowProtocolError("Unsupported Flow Data API version")


def format_protocol_response(response, request, flow_doc):
	"""Add Meta's protocol version without exposing it to app handlers."""
	if request.get("action") == "ping":
		return response
	formatted = dict(response)
	formatted["version"] = flow_doc.data_api_version or "3.0"
	return formatted


def dispatch_data_exchange(context, flow_doc=None):
	"""Dispatch only to the installed-app method selected by the Flow's key."""
	if context["action"] == "ping":
		return {"data": {"status": "active"}}

	handler_key = flow_doc.handler_key if flow_doc else None
	if not handler_key:
		raise FlowProtocolError(
			_("WhatsApp Flow {0} has no Backend Handler Key").format(context["flow"])
		)

	handler_map = frappe.get_hooks("whatsapp_flow_handlers", {}) or {}
	handler_paths = handler_map.get(handler_key, [])
	if isinstance(handler_paths, str):
		handler_paths = [handler_paths]
	if not handler_paths:
		raise FlowProtocolError(
			_("No installed app provides WhatsApp Flow handler key {0}").format(
				handler_key
			)
		)
	if len(handler_paths) != 1:
		raise FlowProtocolError(
			_("WhatsApp Flow handler key {0} is claimed by multiple installed apps").format(
				handler_key
			)
		)

	response = frappe.get_attr(handler_paths[0])(context)
	return validate_handler_response(response)


def validate_handler_response(response):
	if not isinstance(response, dict):
		raise FlowProtocolError("Flow handler must return a JSON object")
	if "data" not in response or not isinstance(response["data"], dict):
		raise FlowProtocolError("Flow handler response must contain a data object")
	if "screen" in response and not isinstance(response["screen"], str):
		raise FlowProtocolError("Flow handler response screen must be a string")
	return response


def encrypt_response(response, aes_key, initial_vector):
	"""Encrypt a response using AES-GCM and Meta's bitwise-inverted IV rule."""
	flipped_iv = bytes(byte ^ 0xFF for byte in initial_vector)
	plaintext = json.dumps(response, separators=(",", ":")).encode()
	encrypted = AESGCM(aes_key).encrypt(flipped_iv, plaintext, None)
	return base64.b64encode(encrypted).decode()


def log_protocol_error(exc, title, diagnostics=None, include_message=True):
	"""Log structural diagnostics without payloads, tokens, or key material."""
	message = f"{type(exc).__name__}: {exc}" if include_message else type(exc).__name__
	identifiers = []
	for key in ("flow", "whatsapp_account", "action", "screen"):
		value = get_safe_diagnostic_value(key, (diagnostics or {}).get(key))
		if value:
			identifiers.append(f"{key}={value}")
	if identifiers:
		message = f"{message}\n" + " ".join(identifiers)
	frappe.log_error(message, title)


def get_safe_diagnostic_value(key, value):
	if not isinstance(value, str) or not value:
		return None
	if key == "action":
		return value if value in ("INIT", "data_exchange", "BACK", "ping") else "<invalid>"
	if key == "screen":
		return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,140}", value) else "<invalid>"
	return value.replace("\n", " ").replace("\r", " ")[:140]
