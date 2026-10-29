# Copyright (c) 2025, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe


def execute():
	"""Move the legacy schema version into its correctly named field."""
	frappe.db.sql(
		"""
		UPDATE `tabWhatsApp Flow`
		SET flow_json_version = COALESCE(NULLIF(data_api_version, ''), '7.3'),
			data_api_version = '3.0'
		WHERE data_api_version IS NULL
			OR data_api_version = ''
			OR data_api_version != '3.0'
		"""
	)
