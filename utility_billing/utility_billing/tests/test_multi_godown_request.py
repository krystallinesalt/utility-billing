"""Tests for one Utility Service Request leasing several properties.

A customer renting several properties (for example godowns) on different terms
needs a single request: each Requested Properties row keeps its own lease, the
request's dates are the envelope of its rows, and every property is billed on
its own invoice carrying that property.

Every test runs in a savepoint rolled back afterwards.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import getdate

from utility_billing.utility_billing.overrides.server import sales_invoice as si_override
from utility_billing.utility_billing.overrides.server.sales_order import get_invoice_groups
from utility_billing.utility_billing.tests import lease_fixtures as fx
from utility_billing.utility_billing.utils import auto_repeat
from utility_billing.utility_billing.utils.cost_center import resolve_primary_cost_center

A = "_Test Multi Godown A"
B = "_Test Multi Godown B"
A_START, A_END = "2026-01-01", "2027-01-01"
B_START, B_END = "2026-04-01", "2028-04-01"
MID_LEASE = "2026-06-01"


def price(item_code: str, rate: float, start: str, end: str) -> None:
	frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item_code,
			"price_list": fx.PRICE_LIST,
			"customer": fx.CUSTOMER,
			"uom": "Nos",
			"price_list_rate": rate,
			"valid_from": start,
			"valid_upto": end,
		}
	).insert(ignore_permissions=True)


class MultiGodownCase(fx.SavepointMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		fx.use_approach("Item Price")
		for name in (A, B):
			fx.lease_property(name)


class TestRequestDates(MultiGodownCase):
	"""The request's dates are the envelope of its property rows."""

	def test_dates_span_every_property(self):
		# The dates typed on the request are replaced by the rows' envelope
		request = fx.make_request([(A, A_START, A_END), (B, B_START, B_END)], "2026-03-01", "2026-09-01")

		self.assertEqual(getdate(request.start_date), getdate(A_START))
		self.assertEqual(getdate(request.end_date), getdate(B_END))
		self.assertEqual(request.contract_length_months, 27)

	def test_each_property_keeps_its_own_lease(self):
		request = fx.make_request([(A, A_START, A_END), (B, B_START, B_END)], A_START, A_END)

		leases = {
			row.utility_property: (str(row.start_date), str(row.end_date))
			for row in request.requested_properties
		}
		self.assertEqual(leases, {A: (A_START, A_END), B: (B_START, B_END)})

	def test_an_open_ended_property_leaves_the_request_open_ended(self):
		request = fx.make_request([(A, A_START, A_END), (B, B_START, None)], A_START, A_END)

		self.assertIsNone(request.end_date)

	def test_a_property_ending_before_it_starts_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be before its start date"):
			fx.make_request([(A, A_START, "2025-06-01")], A_START, A_END)


class TestDailyBillingPerProperty(MultiGodownCase):
	"""The daily Item Price job bills each property on its own invoice."""

	def setUp(self):
		super().setUp()
		self.request = fx.make_request(
			[(A, A_START, A_END), (B, B_START, B_END)], A_START, B_END, billing=True
		)
		price(A, 5000, A_START, A_END)
		price(B, 7000, B_START, B_END)

	def drafts_on(self, day):
		with patch.object(auto_repeat, "today", return_value=day):
			auto_repeat.create_item_price_sales_invoices()

		names = frappe.get_all(
			"Sales Invoice",
			filters={"utility_service_request": self.request.name, "posting_date": day, "docstatus": 0},
			pluck="name",
		)
		return [frappe.get_doc("Sales Invoice", name) for name in names]

	def test_one_invoice_per_property(self):
		invoices = self.drafts_on(MID_LEASE)

		self.assertEqual(sorted(invoice.grand_total for invoice in invoices), [5000, 7000])
		self.assertEqual(sorted(invoice.utility_property for invoice in invoices), [A, B])
		for invoice in invoices:
			self.assertEqual([line.utility_property for line in invoice.items], [invoice.utility_property])
			self.assertEqual([line.item_code for line in invoice.items], [invoice.utility_property])

	def test_each_invoice_takes_its_property_cost_center(self):
		company = fx.company()
		for invoice in self.drafts_on(MID_LEASE):
			expected = resolve_primary_cost_center(
				invoice.utility_property, invoice.utility_property, company
			)
			if expected:
				self.assertEqual(invoice.cost_center, expected)
				self.assertEqual(invoice.items[0].cost_center, expected)

	def test_a_property_is_billed_only_within_its_own_lease(self):
		invoices = self.drafts_on("2026-02-01")

		self.assertEqual([invoice.utility_property for invoice in invoices], [A])

	def test_running_twice_bills_each_property_once(self):
		self.drafts_on(MID_LEASE)

		self.assertEqual(len(self.drafts_on(MID_LEASE)), 2)


class TestInvoiceProperty(MultiGodownCase):
	"""An invoice of one property bills only that property's lines."""

	def setUp(self):
		super().setUp()
		self.request = fx.make_request([(A, A_START, A_END), (B, B_START, B_END)], A_START, B_END)

	def invoice(self, header=None, lines=(A,)):
		invoice = frappe.new_doc("Sales Invoice")
		invoice.utility_service_request = self.request.name
		invoice.utility_property = header
		for name in lines:
			invoice.append("items", {"item_code": name, "qty": 1, "utility_property": name})
		return invoice

	def test_the_invoice_takes_the_one_property_it_bills(self):
		invoice = self.invoice()

		si_override.align_invoice_property(invoice)

		self.assertEqual(invoice.utility_property, A)

	def test_lines_of_several_properties_leave_the_invoice_without_one(self):
		invoice = self.invoice(lines=(A, B))

		si_override.align_invoice_property(invoice)

		self.assertFalse(invoice.utility_property)

	def test_a_line_of_another_property_is_refused(self):
		invoice = self.invoice(header=B, lines=(A,))

		with self.assertRaisesRegex(frappe.ValidationError, "this invoice is for property"):
			si_override.align_invoice_property(invoice)


class TestContractCoverage(MultiGodownCase):
	"""A property the Contract does not list is never billed under it."""

	def setUp(self):
		super().setUp()
		self.request = fx.make_request([(A, A_START, A_END), (B, B_START, B_END)], A_START, B_END)
		self.contract = fx.make_contract(self.request.name)

	def governed(self, property_name, day=MID_LEASE):
		row = frappe._dict(idx=1, item_code=property_name)
		return [frappe._dict(row=row, billing_date=getdate(day), utility_property=property_name)]

	def test_a_listed_property_is_billed(self):
		si_override.check_within_contract(self.contract.name, self.governed(B))

	def test_a_property_missing_from_the_contract_is_refused(self):
		frappe.db.delete(
			"Contract Utility Property Item",
			{"parent": self.contract.name, "parenttype": "Contract", "utility_property": B},
		)

		with self.assertRaisesRegex(frappe.ValidationError, "is not on Contract"):
			si_override.check_within_contract(self.contract.name, self.governed(B))

	def test_the_contract_spans_every_property(self):
		contract = frappe.get_doc("Contract", self.contract.name)

		self.assertEqual(
			(getdate(contract.start_date), getdate(contract.end_date)), (getdate(A_START), getdate(B_END))
		)
		self.assertEqual(sorted(row.utility_property for row in contract.properties), [A, B])


class TestBulkInvoiceGroups(MultiGodownCase):
	"""Bulk invoicing merges orders per customer, request and property."""

	def order(self, request, property_name):
		order = frappe.new_doc("Sales Order")
		order.customer = fx.CUSTOMER
		order.utility_service_request = request
		order.utility_property = property_name
		order.company = fx.company()
		order.transaction_date = MID_LEASE
		order.name = frappe.generate_hash(length=10)
		order.db_insert()
		return order.name

	def test_orders_of_different_properties_are_not_merged(self):
		request = fx.make_request([(A, A_START, A_END), (B, B_START, B_END)], A_START, B_END)
		a1 = self.order(request.name, A)
		a2 = self.order(request.name, A)
		b1 = self.order(request.name, B)

		groups = get_invoice_groups([a1, b1, a2])

		self.assertEqual(
			groups,
			{
				(fx.CUSTOMER, request.name, A): [a1, a2],
				(fx.CUSTOMER, request.name, B): [b1],
			},
		)
