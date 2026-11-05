import calendar
from typing import Any, Dict, List, Optional

import frappe
from frappe.query_builder import DocType
from frappe.utils import add_days, cint, date_diff, getdate, today

from utility_billing.utility_billing.utils.contract_status import contract_requirement_blocks
from utility_billing.utility_billing.utils.item_rate import get_rate_for_date
from utility_billing.utility_billing.utils.lease_validity import (
	INVALID,
	is_holding_over,
	is_vacated,
	same_month,
)
from utility_billing.utility_billing.utils.request_prices import is_item_price_approach, last_billed_day


@frappe.whitelist()
def cancel_auto_repeats_for_property(property_name: str) -> None:
	SalesInvoice = DocType("Sales Invoice")
	SalesInvoiceItem = DocType("Sales Invoice Item")
	AutoRepeat = DocType("Auto Repeat")

	invoice_query = (
		frappe.qb.from_(SalesInvoice)
		.left_join(SalesInvoiceItem)
		.on(SalesInvoice.name == SalesInvoiceItem.parent)
		.select(SalesInvoice.name)
		.where(
			(SalesInvoice.utility_property == property_name)
			| (SalesInvoiceItem.utility_property == property_name)
		)
		.distinct()
	)
	invoice_names = [row[0] for row in invoice_query.run()]

	if not invoice_names:
		return

	auto_repeat_query = (
		frappe.qb.from_(AutoRepeat)
		.select(AutoRepeat.name, AutoRepeat.reference_document)
		.where(
			(AutoRepeat.reference_doctype == "Sales Invoice")
			& (AutoRepeat.reference_document.isin(invoice_names))
		)
	)
	auto_repeats = auto_repeat_query.run()
	for auto_repeat in auto_repeats:
		auto_repeat_name = auto_repeat[0]
		auto_repeat_doc = frappe.get_doc("Auto Repeat", auto_repeat_name)
		auto_repeat_doc.disabled = 1
		auto_repeat_doc.save()


@frappe.whitelist()
def process_penalties_for_overdue_invoices() -> None:
	# No params to check
	try:
		overdue_invoices = frappe.db.get_list(
			"Sales Invoice",
			filters={"status": "Overdue"},
			fields=["name", "due_date", "posting_date", "customer"],
		)

		for invoice in overdue_invoices:
			try:
				original_invoice = frappe.get_doc("Sales Invoice", invoice["name"])
				service_request = original_invoice.get("utility_service_request")

				if not service_request:
					continue

				service_request_doc = frappe.get_doc("Utility Service Request", service_request)
				requested_properties = service_request_doc.get("requested_properties")

				penalty_items = []

				for item in original_invoice.items:
					try:
						utility_property = item.utility_property

						matching_request = next(
							(req for req in requested_properties if req.utility_property == utility_property),
							None,
						)

						if not matching_request or not matching_request.adjustment_rule:
							continue

						rule = frappe.get_doc("Billing Adjustment Rule", matching_request.adjustment_rule)
						days_overdue = date_diff(today(), invoice["due_date"]) - float(rule.grace_period_days)
						if days_overdue <= rule.grace_period_days:
							continue

						frequency_days = get_frequency_days(rule.penalty_frequency)
						if frequency_days > 0:
							penalty_cycles = max(1, days_overdue // frequency_days)
							qty = penalty_cycles
						else:
							penalty_cycles = 1
							qty = 1

						penalty_amount = 0
						rate = 0

						if rule.penalty_type == "Fixed Amount":
							rate = rule.penalty_value
							penalty_amount = penalty_cycles * rule.penalty_value
						elif rule.penalty_type == "Percentage":
							base = item.amount
							if rule.is_compounding:
								for _ in range(penalty_cycles):
									increment = base * (rule.penalty_value / 100)
									penalty_amount += increment
									base += increment
								rate = penalty_amount / qty if qty else penalty_amount
							else:
								rate = base * (rule.penalty_value / 100)
								penalty_amount = rate * penalty_cycles

						if rule.penalty_cap and penalty_amount > rule.penalty_cap:
							penalty_amount = rule.penalty_cap
							rate = penalty_amount / qty if qty else penalty_amount

						if penalty_amount > 0:
							penalty_items.append(
								{
									"item_code": item.item_code,
									"description": f"Penalty for overdue invoice {invoice['name']}",
									"qty": qty,
									"rate": rate,
									"amount": penalty_amount,
									"income_account": rule.penalty_income_account,
									"receivable_account": rule.penalty_receivable_account,
									"utility_property": utility_property,
								}
							)

					except Exception:
						frappe.log_error(
							f"Error processing item {item.item_code} in invoice {invoice['name']}: {frappe.get_traceback()}"
						)
				if penalty_items:
					try:
						create_penalty_invoice(original_invoice, service_request, rule, penalty_items)
					except Exception:
						frappe.log_error(
							f"Error creating penalty invoice for {invoice['name']}: {frappe.get_traceback()}"
						)

			except Exception:
				frappe.log_error(
					f"Error processing invoice {invoice.get('name', '')}: {frappe.get_traceback()}"
				)

	except Exception:
		frappe.log_error(f"Error in process_penalties_for_overdue_invoices: {frappe.get_traceback()}")


def get_frequency_days(frequency: str) -> int:
	return {"Daily": 1, "Weekly": 7, "Monthly": 30, "One-time": 0}.get(frequency, 1)


def create_penalty_invoice(
	original_invoice: Any, service_request: str, rule: Any, items: list[dict[str, Any]]
) -> None:
	try:
		new_total = sum(float(item.get("amount") or 0) for item in items)
		penalty_remarks = f"Penalty Invoice for overdue invoice {original_invoice.name}"

		previous_penalties = frappe.get_all(
			"Sales Invoice",
			filters={"remarks": penalty_remarks},
			fields=["name", "creation"],
			order_by="creation desc",
		)

		latest_invoice = None

		for idx, penalty in enumerate(previous_penalties):
			doc = frappe.get_doc("Sales Invoice", penalty.name)
			if idx == 0:
				latest_invoice = doc
			else:
				try:
					if doc.docstatus == 1:
						doc.cancel()
					elif doc.docstatus == 0:
						frappe.delete_doc("Sales Invoice", doc.name, ignore_permissions=True)
				except Exception:
					frappe.log_error(
						frappe.get_traceback(), f"Failed to cancel/delete penalty invoice {doc.name}"
					)

		if latest_invoice:
			previous_total = sum(float(it.amount or 0) for it in latest_invoice.items)
			if previous_total == new_total:
				if latest_invoice.docstatus == 0:
					latest_invoice.items = []
					for item in items:
						latest_invoice.append("items", item)
					latest_invoice.save(ignore_permissions=True)
					frappe.msgprint(
						f"Penalty Invoice {latest_invoice.name} updated for {original_invoice.name}"
					)
				return
			else:
				if latest_invoice.docstatus == 1:
					latest_invoice.cancel()
				else:
					frappe.delete_doc("Sales Invoice", latest_invoice.name, ignore_permissions=True)

		invoice = frappe.new_doc("Sales Invoice")
		invoice.customer = original_invoice.customer
		invoice.due_date = add_days(today(), 7)
		invoice.posting_date = today()

		for item in items:
			invoice.append("items", item)

		invoice.remarks = penalty_remarks
		invoice.utility_service_request = service_request
		invoice.reference_invoice = original_invoice.name
		invoice.is_penalty_invoice = 1

		invoice.insert(ignore_permissions=True)
		if rule.submit_penalty_invoice:
			invoice.submit()

		comment_content = (
			f"Penalty invoice <a href='/app/sales-invoice/{invoice.name}'>{invoice.name}</a> "
			f"was created for overdue invoice <a href='/app/sales-invoice/{original_invoice.name}'>{original_invoice.name}</a>."
		)

		add_comment("Sales Invoice", original_invoice.name, comment_content)
		if getattr(original_invoice, "utility_service_request", None):
			add_comment("Utility Service Request", original_invoice.utility_service_request, comment_content)

		frappe.msgprint(f"Penalty Invoice {invoice.name} created for {original_invoice.name}")

	except Exception:
		frappe.log_error(
			f"Error in create_penalty_invoice for {original_invoice.name}: {frappe.get_traceback()}"
		)


def add_comment(doctype: str, name: str, content: str) -> None:
	try:
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": doctype,
				"reference_name": name,
				"content": content,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(f"Error adding comment to {doctype} {name}: {frappe.get_traceback()}")


def final_billing_day(request, row, billing_date):
	"""Return the date a row's line bills on ``billing_date``, or ``None`` to skip it.

	Normally the billing date itself. When the lease ends before it in the same
	month (its last billed day, or the Vacated On date of a vacated row), the
	line bills that last day, so the month is billed in full at the last rent.
	A later month is past the lease and not billed. A held over lease (Lease
	Status Valid) never ends.
	"""
	if is_holding_over(row):
		return billing_date

	if is_vacated(row):
		last_day = getdate(row.vacated_on)
	elif row.end_date and is_item_price_approach():
		last_day = last_billed_day(request, row.utility_property, row.end_date)
	else:
		last_day = getdate(row.end_date) if row.end_date else None

	if not last_day or billing_date <= last_day:
		return billing_date

	return last_day if same_month(last_day, billing_date) else None


def set_property_on_invoice(invoice, row, source_item, company: str) -> str | None:
	"""Mark an auto-billed invoice as the bill of one leased property.

	A request may lease several properties, each billed on its own invoice, so
	the property and its cost center come from the Requested Properties row,
	not from the request header (which is empty when it leases several).

	Args:
		invoice: The new ``Sales Invoice``.
		row: The ``Contract Utility Property Item`` row being billed.
		source_item: The request's item line billed for the row.
		company: The request's company.

	Returns:
		The property's cost center, or ``None`` when it has none in ``company``.
	"""
	from utility_billing.utility_billing.utils.cost_center import resolve_primary_cost_center

	if invoice.meta.has_field("utility_property"):
		invoice.utility_property = row.utility_property

	cost_center = resolve_primary_cost_center(row.utility_property, source_item.item_code, company)
	if cost_center:
		invoice.cost_center = cost_center

	return cost_center


@frappe.whitelist()
def create_item_price_sales_invoices():
	billing_date = getdate(today())
	period = billing_date.strftime("%Y-%m")

	rows = frappe.get_all(
		"Contract Utility Property Item",
		filters={
			"parenttype": "Utility Service Request",
			"parentfield": "requested_properties",
			"enable_auto_billing": 1,
			"rent_billing_approach": "Item Price",
		},
		# Active rows, and vacated ones: the month a tenant left in is still billed
		or_filters={"is_active": 1, "lease_status": INVALID},
		fields=[
			"name",
			"parent",
			"utility_property",
			"item_code",
			"start_date",
			"end_date",
			"is_active",
			"lease_status",
			"vacated_on",
			"billing_date_type",
			"billing_day_of_month",
		],
	)

	for row in rows:
		try:
			if not row.item_code or not row.parent:
				continue

			if row.start_date and billing_date < getdate(row.start_date):
				continue

			if not cint(row.is_active) and not (is_vacated(row) and same_month(row.vacated_on, billing_date)):
				continue

			last_day = calendar.monthrange(billing_date.year, billing_date.month)[1]

			if row.billing_date_type == "First Day of Month":
				scheduled_day = 1
			elif row.billing_date_type == "Last Day of Month":
				scheduled_day = last_day
			elif row.billing_date_type == "Specific Day of Month":
				scheduled_day = min(
					max(cint(row.billing_day_of_month), 1),
					last_day,
				)
			else:
				continue

			if billing_date.day != scheduled_day:
				continue

			request = frappe.get_doc("Utility Service Request", row.parent)

			if request.docstatus != 1:
				continue

			# The day the line bills: the billing day, or the lease's last day
			# when the lease ends earlier in the same month, which is then billed
			# in full at the last rent. A held over lease (Valid) has no end.
			line_date = final_billing_day(request, row, billing_date)
			if not line_date:
				continue

			# The drafts are inserted without validation, so Require Contract is
			# checked here: no submitted Contract, none signed when signing is
			# required, or every row released (the month a row was vacated in aside).
			if contract_requirement_blocks(request.name, line_date):
				continue

			customer = request.get("customer") or (
				request.get("party_name") if request.get("service_request_from") == "Customer" else None
			)

			if not customer or not request.get("company"):
				continue

			matching_items = [
				item
				for item in request.items
				if item.item_code == row.item_code and item.utility_property == row.utility_property
			]

			for source_item in matching_items:
				if not source_item.item_code or not source_item.qty:
					continue

				marker = f"AUTO-BILLING:{request.name}:{row.name}:{source_item.name}:{period}"

				existing = frappe.get_all(
					"Sales Invoice",
					filters={
						"docstatus": ["!=", 2],
						"remarks": ["like", f"%{marker}%"],
					},
					pluck="name",
					limit=1,
				)

				if existing:
					continue

				invoice = frappe.new_doc("Sales Invoice")
				invoice.customer = customer
				invoice.company = request.company
				invoice.utility_service_request = request.name
				invoice.posting_date = billing_date
				invoice.set_posting_time = 1

				field_mapping = {
					"currency": "currency",
					"price_list": "selling_price_list",
					"territory": "territory",
					"customer_address": "customer_address",
					"contact_person": "contact_person",
					"tax_category": "tax_category",
					"taxes_and_charges": "taxes_and_charges",
					"payment_terms_template": "payment_terms_template",
					"ignore_default_payment_terms_template": ("ignore_default_payment_terms_template"),
					"cost_center": "cost_center",
					"project": "project",
					"department": "department",
					"branch": "branch",
				}

				for source_field, target_field in field_mapping.items():
					value = request.get(source_field)
					if value and invoice.meta.has_field(target_field):
						invoice.set(target_field, value)

				for field in (
					"cost_center",
					"project",
					"department",
					"branch",
				):
					if not invoice.get(field) and source_item.get(field) and invoice.meta.has_field(field):
						invoice.set(field, source_item.get(field))

				for dimension in frappe.get_all(
					"Accounting Dimension",
					filters={"disabled": 0},
					fields=["fieldname"],
				):
					fieldname = dimension.fieldname
					if (
						fieldname
						and request.meta.has_field(fieldname)
						and invoice.meta.has_field(fieldname)
						and request.get(fieldname)
					):
						invoice.set(fieldname, request.get(fieldname))

				property_cost_center = set_property_on_invoice(invoice, row, source_item, request.company)

				if not invoice.get("territory"):
					invoice.territory = frappe.db.get_value("Customer", customer, "territory")

				invoice.remarks = (
					f"Auto-generated from Utility Service Request "
					f"{request.name}, property {row.utility_property}, "
					f"billing period {period}. {marker}"
				)

				invoice.set_missing_values()

				item = invoice.append(
					"items",
					{
						"item_code": source_item.item_code,
						"qty": source_item.qty,
						"uom": source_item.get("uom"),
						"conversion_factor": (source_item.get("conversion_factor") or 1),
						"cost_center": (
							source_item.get("cost_center") or property_cost_center or invoice.get("cost_center")
						),
						"utility_property": row.utility_property,
						"project": (source_item.get("project") or invoice.get("project")),
					},
				)

				from erpnext.stock.get_item_details import get_item_details

				args = frappe._dict(item.as_dict())
				args.update(
					{
						"doctype": "Sales Invoice",
						"company": invoice.company,
						"customer": invoice.customer,
						"currency": invoice.currency,
						"price_list": invoice.selling_price_list,
						"price_list_currency": invoice.price_list_currency,
						"plc_conversion_rate": (invoice.plc_conversion_rate or 1),
						"conversion_rate": invoice.conversion_rate or 1,
						"transaction_date": str(billing_date),
						"posting_date": str(billing_date),
						"is_pos": 0,
						"ignore_pricing_rule": 0,
					}
				)

				details = get_item_details(args, doc=invoice)

				for field, value in details.items():
					if item.meta.has_field(field) and value is not None:
						item.set(field, value)

				item.qty = source_item.qty
				if line_date != billing_date:
					item.custom_posting_date = line_date
				# The request's own price on the line's date, the last rent when held over
				rate = get_rate_for_date(
					item.item_code,
					line_date,
					customer=invoice.customer,
					price_list=invoice.selling_price_list,
					uom=item.uom,
					qty=item.qty,
					conversion_factor=item.conversion_factor or 1,
					plc_conversion_rate=invoice.plc_conversion_rate or 1,
					conversion_rate=invoice.conversion_rate or 1,
					service_request=request.name,
				)
				if rate is not None:
					item.price_list_rate = rate
					item.rate = rate
				# The item's defaults must not move the line off its property
				item.utility_property = row.utility_property
				item.cost_center = (
					source_item.get("cost_center") or property_cost_center or item.get("cost_center")
				)

				invoice.calculate_taxes_and_totals()
				invoice.posting_date = str(getdate(invoice.posting_date))
				invoice.flags.ignore_validate = True
				invoice.insert(ignore_permissions=True, ignore_mandatory=True)

		except Exception:
			frappe.log_error(
				title="Item Price Auto Billing Failed",
				message=frappe.get_traceback(),
			)
