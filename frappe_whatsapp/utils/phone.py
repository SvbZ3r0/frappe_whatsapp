import re


def normalize_phone_number(phone_number):
	"""Return only the digits from a phone number-like value."""
	return re.sub(r"\D", "", phone_number or "")
