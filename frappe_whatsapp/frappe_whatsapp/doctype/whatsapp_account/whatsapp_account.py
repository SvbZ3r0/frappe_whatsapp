# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.integrations.utils import make_post_request
from frappe.model.document import Document
from frappe.utils import now_datetime


class WhatsAppAccount(Document):
	def validate(self):
		retention_days = self.flow_exchange_retention_days or 7
		if retention_days < 1 or retention_days > 90:
			frappe.throw(_("Flow Exchange Response Retention must be between 1 and 90 days"))

	def on_update(self):
		"""Check there is only one default of each type."""
		self.there_must_be_only_one_default()

	def there_must_be_only_one_default(self):
		"""If current WhatsApp Account is default, un-default all other accounts."""
		for field in ("is_default_incoming", "is_default_outgoing"):
			if not self.get(field):
				continue

			for whatsapp_account in frappe.get_all("WhatsApp Account", filters={field: 1}):
				if whatsapp_account.name == self.name:
					continue

				whatsapp_account = frappe.get_doc("WhatsApp Account", whatsapp_account.name)
				whatsapp_account.set(field, 0)
				whatsapp_account.save()

	@frappe.whitelist()
	def generate_flow_encryption_keys(self):
		"""Stage an encrypted RSA-2048 key pair without replacing the active key."""
		from cryptography.hazmat.primitives import serialization
		from cryptography.hazmat.primitives.asymmetric import rsa

		if self.flow_pending_public_key:
			frappe.throw(
				_(
					"A Flow encryption key rotation is already pending. Register and verify it before generating another key."
				)
			)

		passphrase = frappe.generate_hash(length=32)
		private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
		private_pem = private_key.private_bytes(
			encoding=serialization.Encoding.PEM,
			format=serialization.PrivateFormat.TraditionalOpenSSL,
			encryption_algorithm=serialization.BestAvailableEncryption(passphrase.encode()),
		).decode()
		public_pem = private_key.public_key().public_bytes(
			encoding=serialization.Encoding.PEM,
			format=serialization.PublicFormat.SubjectPublicKeyInfo,
		).decode()

		self.flow_pending_private_key = private_pem
		self.flow_pending_private_key_passphrase = passphrase
		self.flow_pending_public_key = public_pem
		self.flow_pending_key_status = "Generated - Not Registered"
		self.save(ignore_permissions=True)
		return public_pem

	def get_flow_encryption_api_config(self):
		for field in ("url", "version", "phone_id"):
			if not self.get(field):
				frappe.throw(
					_("{0} is required").format(frappe.bold(self.meta.get_label(field)))
				)
		token = self.get_password("token")
		if not token:
			frappe.throw(_("Access token is required"))
		return (
			f"{self.url}/{self.version}/{self.phone_id}/whatsapp_business_encryption",
			{"Authorization": f"Bearer {token}"},
		)

	@frappe.whitelist()
	def register_flow_public_key(self):
		"""Register the pending key, leaving the active decryption key untouched."""
		import requests

		public_key = self.flow_pending_public_key or self.flow_public_key
		if not public_key:
			frappe.throw(_("Generate a Flow encryption key first"))
		registering_pending_key = bool(self.flow_pending_public_key)
		endpoint, headers = self.get_flow_encryption_api_config()
		response = requests.post(
			endpoint,
			headers=headers,
			data={"business_public_key": public_key},
			timeout=30,
		)
		if response.status_code != 200:
			try:
				error = response.json().get("error", {})
				error_message = error.get("error_user_msg") or error.get("message")
			except (ValueError, AttributeError):
				error_message = response.text
			frappe.throw(_("Failed to register Flow public key: {0}").format(error_message))

		result = response.json()
		if not result.get("success"):
			frappe.throw(_("Meta did not accept the Flow public key"))
		if registering_pending_key:
			self.flow_pending_key_status = "Registered - Verification Required"
		else:
			self.flow_public_key_signature_status = "NOT VERIFIED"
			self.flow_public_key_verified = 0
			self.flow_public_key_verified_on = None
		self.save(ignore_permissions=True)
		return result

	@frappe.whitelist()
	def verify_flow_public_key(self):
		"""Verify Meta's key and atomically promote a pending rotation."""
		import requests

		candidate_public_key = self.flow_pending_public_key or self.flow_public_key
		if not candidate_public_key:
			frappe.throw(_("Generate a Flow encryption key first"))
		verifying_pending_key = bool(self.flow_pending_public_key)
		if (
			verifying_pending_key
			and self.flow_pending_key_status == "Generated - Not Registered"
		):
			frappe.throw(_("Register the pending Flow public key before verifying it"))
		endpoint, headers = self.get_flow_encryption_api_config()
		response = requests.get(endpoint, headers=headers, timeout=30)
		if response.status_code != 200:
			try:
				error = response.json().get("error", {})
				error_message = error.get("error_user_msg") or error.get("message")
			except (ValueError, AttributeError):
				error_message = response.text
			frappe.throw(_("Failed to verify Flow public key: {0}").format(error_message))

		result = response.json()
		meta_public_key = result.get("business_public_key")
		signature_status = (
			result.get("business_public_key_signature_status") or "NOT REGISTERED"
		).upper()
		key_matches = public_keys_match(candidate_public_key, meta_public_key)
		verified = signature_status == "VALID" and key_matches

		if verifying_pending_key:
			if verified:
				self.promote_pending_flow_encryption_key(signature_status)
			else:
				self.flow_pending_key_status = f"Verification Failed - {signature_status}"
		else:
			self.flow_public_key_signature_status = signature_status
			self.flow_public_key_verified = int(verified)
			self.flow_public_key_verified_on = now_datetime()
		self.save(ignore_permissions=True)
		return {
			"verified": verified,
			"signature_status": signature_status,
			"key_matches": key_matches,
			"promoted": bool(verifying_pending_key and verified),
		}

	def promote_pending_flow_encryption_key(self, signature_status):
		"""Promote a verified pending key and retain the former active key."""
		pending_private_key = self.get_password(
			"flow_pending_private_key", raise_exception=False
		)
		pending_passphrase = self.get_password(
			"flow_pending_private_key_passphrase", raise_exception=False
		)
		if not pending_private_key:
			frappe.throw(_("Pending Flow private key is missing"))

		active_private_key = self.get_password(
			"flow_private_key", raise_exception=False
		)
		active_passphrase = self.get_password(
			"flow_private_key_passphrase", raise_exception=False
		)
		if self.flow_public_key and active_private_key:
			self.flow_previous_public_key = self.flow_public_key
			self.flow_previous_private_key = active_private_key
			self.flow_previous_private_key_passphrase = active_passphrase
			self.flow_previous_key_retained_on = now_datetime()

		self.flow_public_key = self.flow_pending_public_key
		self.flow_private_key = pending_private_key
		self.flow_private_key_passphrase = pending_passphrase
		self.flow_pending_public_key = None
		self.flow_pending_private_key = None
		self.flow_pending_private_key_passphrase = None
		self.flow_pending_key_status = None
		self.flow_public_key_signature_status = signature_status
		self.flow_public_key_verified = 1
		self.flow_public_key_verified_on = now_datetime()

	@frappe.whitelist()
	def subscribe_app(self):
		"""Subscribe this app to webhooks for the WhatsApp Business Account.

		Required after phone number registration to receive incoming messages.
		Calls POST /{version}/{business_id}/subscribed_apps on the Graph API.
		"""
		for field in ("url", "version", "business_id"):
			if not self.get(field):
				frappe.throw(_("{0} is required to subscribe the app").format(
					frappe.bold(self.meta.get_label(field))
				))

		token = self.get_password("token")
		if not token:
			frappe.throw(_("Access token is required to subscribe the app"))

		endpoint = f"{self.url}/{self.version}/{self.business_id}/subscribed_apps"
		headers = {
			"authorization": f"Bearer {token}",
			"content-type": "application/json",
		}

		try:
			response = make_post_request(endpoint, headers=headers)
		except Exception as e:
			error_message = str(e)
			if frappe.flags.integration_request:
				err = frappe.flags.integration_request.json().get("error", {})
				if err:
					error_message = err.get("message") or err.get("Error") or error_message
			frappe.throw(_("Failed to subscribe app to webhooks: {0}").format(error_message))

		if not response.get("success"):
			frappe.throw(_("Subscription was not successful: {0}").format(frappe.as_json(response)))

		frappe.logger().info(
			f"WhatsApp app subscribed to webhooks for business_id={self.business_id}"
		)
		return response


def public_keys_match(local_public_key, meta_public_key):
	"""Compare PEM public keys by canonical SubjectPublicKeyInfo bytes."""
	if not local_public_key or not meta_public_key:
		return False

	from cryptography.hazmat.primitives import serialization

	try:
		local_key = serialization.load_pem_public_key(local_public_key.encode())
		meta_key = serialization.load_pem_public_key(meta_public_key.encode())
		local_der = local_key.public_bytes(
			serialization.Encoding.DER,
			serialization.PublicFormat.SubjectPublicKeyInfo,
		)
		meta_der = meta_key.public_bytes(
			serialization.Encoding.DER,
			serialization.PublicFormat.SubjectPublicKeyInfo,
		)
	except (TypeError, ValueError):
		return False
	return local_der == meta_der
