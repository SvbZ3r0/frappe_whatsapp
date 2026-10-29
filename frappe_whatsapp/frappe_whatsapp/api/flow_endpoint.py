# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

"""Encrypted WhatsApp Flow Data Exchange protocol endpoint."""

import base64
import binascii
import hashlib
import hmac
import json

import frappe
from frappe import _
from werkzeug.wrappers import Response

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class FlowProtocolError(Exception):
	"""The request does not satisfy the WhatsApp Flow protocol."""


class FlowDecryptionError(FlowProtocolError):
	"""No configured account could decrypt the request."""


class FlowSignatureError(FlowProtocolError):
	"""Meta request signature validation failed."""


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
		validate_request_signature(raw_body, account)

		flow_doc = resolve_flow(decrypted, account, flow)
		context = normalize_context(decrypted, flow_doc, account)
		ordinary_response = dispatch_data_exchange(context, flow_doc)
		encrypted_response = encrypt_response(
			ordinary_response, aes_key, initial_vector
		)
		return Response(encrypted_response, status=200, content_type="text/plain")
	except FlowSignatureError as exc:
		log_protocol_error(exc, "WhatsApp Flow Signature Error")
		return Response("", status=432, content_type="text/plain")
	except FlowDecryptionError as exc:
		log_protocol_error(exc, "WhatsApp Flow Decryption Error")
		return Response("", status=421, content_type="text/plain")
	except FlowProtocolError as exc:
		log_protocol_error(exc, "WhatsApp Flow Protocol Error")
		return Response("", status=400, content_type="text/plain")
	except Exception as exc:
		# Handler exceptions are untrusted and may embed decrypted customer data.
		log_protocol_error(exc, "WhatsApp Flow Handler Error", include_message=False)
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
		filters={"flow_public_key": ["!=", ""]},
		pluck="name",
	)
	for account_name in configured_accounts:
		if account_name not in candidate_names:
			candidate_names.append(account_name)

	for account_name in candidate_names:
		account = frappe.get_doc("WhatsApp Account", account_name)
		private_key = account.get_password("flow_private_key", raise_exception=False)
		if not private_key:
			continue
		passphrase = account.get_password(
			"flow_private_key_passphrase", raise_exception=False
		)
		try:
			decrypted, aes_key, initial_vector = decrypt_request(
				envelope, private_key, passphrase
			)
			return decrypted, aes_key, initial_vector, account
		except (ValueError, TypeError, InvalidTag, binascii.Error):
			continue

	raise FlowDecryptionError("Unable to decrypt request with a configured account key")


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
	required = bool(account.require_flow_request_signature)
	if not signature and not required:
		return

	app_secret = account.get_password("app_secret", raise_exception=False)
	if not signature or not app_secret:
		raise FlowSignatureError("Missing Meta request signature configuration")
	if not verify_signature(raw_body, signature, app_secret):
		raise FlowSignatureError("Invalid Meta request signature")


def verify_signature(payload, signature, app_secret):
	"""Constant-time validation of Meta's X-Hub-Signature-256 header."""
	if isinstance(payload, str):
		payload = payload.encode()
	expected = "sha256=" + hmac.new(
		app_secret.encode(), payload, hashlib.sha256
	).hexdigest()
	return hmac.compare_digest(signature, expected)


def resolve_flow(decrypted, account, flow_hint=None):
	"""Resolve the Flow from an explicit hint and/or the persisted launch token."""
	flow_token = decrypted.get("flow_token")
	correlated_flow = None
	if flow_token:
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
		if matches:
			correlated_flow = matches[0].flow

	if flow_hint and correlated_flow and flow_hint != correlated_flow:
		raise FlowProtocolError("Flow hint does not match the persisted launch token")
	flow_name = correlated_flow or flow_hint
	if not flow_name:
		if decrypted.get("action") == "ping":
			return None
		raise FlowProtocolError("Unable to identify Flow from request")
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


def log_protocol_error(exc, title, include_message=True):
	# Never log the encrypted envelope, decrypted customer data, or key material.
	message = f"{type(exc).__name__}: {exc}" if include_message else type(exc).__name__
	frappe.log_error(message, title)
