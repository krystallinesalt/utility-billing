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


class TestMovingAProperty(MultiGodownCase):
	"""A property released on one request and added to another keeps its prices."""

	def test_prices_of_the_added_property_are_left_alone(self):
		from utility_billing.utility_billing.utils.contract_sync import CONSENT_KEY

		first = fx.make_request([(A, "2026-01-01", "2028-12-01")], "2026-01-01", "2028-12-01")
		second = fx.make_request([(B, B_START, "2027-04-01")], B_START, "2027-04-01")
		price(B, 7000, B_START, "2027-03-31")
		# A later price of the same customer and item, inside the first request's dates
		price(B, 9000, "2027-05-01", "2027-12-31")

		second.reload()
		second.requested_properties[0].is_active = 0
		second.set(CONSENT_KEY, 1)
		second.save(ignore_permissions=True)

		first.reload()
		first.append(
			"requested_properties",
			{
				"utility_property": B,
				"item_code": B,
				"start_date": B_START,
				"end_date": "2027-04-01",
				"is_active": 1,
				"rent_billing_approach": "Item Price",
			},
		)
		first.set(CONSENT_KEY, 1)
		first.save(ignore_permissions=True)

		prices = frappe.get_all(
			"Item Price",
			filters={"item_code": B, "price_list": fx.PRICE_LIST, "customer": fx.CUSTOMER},
			fields=["valid_from", "valid_upto", "price_list_rate"],
			order_by="valid_from",
		)
		self.assertEqual(
			[(str(p.valid_from), str(p.valid_upto), p.price_list_rate) for p in prices],
			[(B_START, "2027-03-31", 7000), ("2027-05-01", "2027-12-31", 9000)],
		)
		self.assertEqual(getdate(first.start_date), getdate("2026-01-01"))


class TestLeaseStatus(MultiGodownCase):
	"""Automatic follows the dates, Valid holds the lease over, Invalid releases it."""

	def setUp(self):
		super().setUp()
		self.request = fx.make_request(
			[(A, A_START, A_END), (B, B_START, B_END)], A_START, B_END, billing=True
		)
		price(A, 5000, A_START, "2026-12-31")
		price(B, 7000, B_START, B_END)

	def set_row(self, property_name, **values):
		request = frappe.get_doc("Utility Service Request", self.request.name)
		row = next(r for r in request.requested_properties if r.utility_property == property_name)
		row.update(values)
		request.save(ignore_permissions=True)
		return request

	def drafts_on(self, day):
		with patch.object(auto_repeat, "today", return_value=day):
			auto_repeat.create_item_price_sales_invoices()

		names = frappe.get_all(
			"Sales Invoice",
			filters={"utility_service_request": self.request.name, "posting_date": day, "docstatus": 0},
			pluck="name",
		)
		return {
			doc.utility_property: doc for doc in (frappe.get_doc("Sales Invoice", name) for name in names)
		}

	def test_invalid_needs_the_vacated_on_date(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Vacated On"):
			self.set_row(B, lease_status="Invalid")

	def test_invalid_releases_the_property(self):
		request = self.set_row(B, lease_status="Invalid", vacated_on="2026-06-15")

		row = next(r for r in request.requested_properties if r.utility_property == B)
		self.assertEqual(row.is_active, 0)

	def test_the_vacated_month_is_billed_and_later_ones_are_not(self):
		self.set_row(B, lease_status="Invalid", vacated_on="2026-06-15")

		self.assertIn(B, self.drafts_on("2026-06-01"))
		self.assertNotIn(B, self.drafts_on("2026-07-01"))
		self.assertIn(A, self.drafts_on("2026-07-01"))

	def test_a_held_over_lease_goes_on_billing_the_last_rent(self):
		self.set_row(A, lease_status="Valid")

		invoices = self.drafts_on("2027-03-01")

		self.assertIn(A, invoices)
		self.assertEqual(invoices[A].items[0].rate, 5000)

	def test_an_automatic_lease_is_not_billed_after_it_ends(self):
		self.assertNotIn(A, self.drafts_on("2027-03-01"))

	def test_a_lease_ending_mid_month_bills_that_month_in_full(self):
		row = next(r for r in self.request.requested_properties if r.utility_property == A)
		frappe.db.set_value(
			"Contract Utility Property Item",
			row.name,
			{"end_date": "2026-12-15", "billing_date_type": "Last Day of Month"},
		)
		price(A, 5000, "2027-01-01", "2027-12-31")

		invoices = self.drafts_on("2026-12-31")

		self.assertIn(A, invoices)
		self.assertEqual(str(invoices[A].items[0].custom_posting_date), "2026-12-15")
		self.assertEqual(invoices[A].items[0].rate, 5000)


class TestOccupiedDays(MultiGodownCase):
	"""The vacated month can bill the occupied days only."""

	def test_the_occupied_share_of_the_vacated_month(self):
		from utility_billing.utility_billing.utils.lease_validity import occupied_fraction

		row = frappe._dict(lease_status="Invalid", vacated_on="2026-06-15", start_date="2026-01-01")

		self.assertAlmostEqual(occupied_fraction(row, "2026-06-30"), 15 / 30)
		self.assertEqual(occupied_fraction(row, "2026-05-31"), 1.0)

	def test_a_lease_starting_in_the_vacated_month_counts_from_its_start(self):
		from utility_billing.utility_billing.utils.lease_validity import occupied_fraction

		row = frappe._dict(lease_status="Invalid", vacated_on="2026-06-20", start_date="2026-06-11")

		self.assertAlmostEqual(occupied_fraction(row, "2026-06-30"), 10 / 30)

	def test_only_an_invoice_asking_for_it_is_cut(self):
		request = fx.make_request([(A, A_START, A_END), (B, B_START, B_END)], A_START, B_END)
		request.reload()
		row = next(r for r in request.requested_properties if r.utility_property == B)
		row.lease_status, row.vacated_on = "Invalid", "2026-06-15"
		request.save(ignore_permissions=True)

		line = frappe._dict(utility_property=B, item_code=B)
		full = frappe._dict(bill_occupied_days_only=0)
		occupied = frappe._dict(bill_occupied_days_only=1)

		self.assertEqual(si_override.occupied_factor(full, line, getdate("2026-06-30"), request.name), 1.0)
		self.assertAlmostEqual(
			si_override.occupied_factor(occupied, line, getdate("2026-06-30"), request.name), 0.5
		)
		other = frappe._dict(utility_property=A, item_code=A)
		self.assertEqual(
			si_override.occupied_factor(occupied, other, getdate("2026-06-30"), request.name), 1.0
		)

	def test_the_approval_is_required(self):
		invoice = frappe._dict(bill_occupied_days_only=1, occupancy_approval=None)

		with self.assertRaisesRegex(frappe.ValidationError, "approval"):
			si_override.check_occupied_days_approval(invoice, [])


class TestVacatedContract(MultiGodownCase):
	"""A Contract whose properties are all vacated still bills the month they were left in."""

	def test_the_vacated_month_is_still_held(self):
		from utility_billing.utility_billing.utils.contract_status import contract_released
		from utility_billing.utility_billing.utils.contract_sync import CONSENT_KEY

		request = fx.make_request([(A, A_START, A_END)], A_START, A_END)
		contract = fx.make_contract(request.name)
		request.reload()
		request.requested_properties[0].lease_status = "Invalid"
		request.requested_properties[0].vacated_on = "2026-06-15"
		request.set(CONSENT_KEY, 1)
		request.save(ignore_permissions=True)

		self.assertFalse(contract_released(contract.name, "2026-06-30"))
		self.assertTrue(contract_released(contract.name, "2026-07-31"))
		self.assertTrue(contract_released(contract.name))


class TestEndingALeaseOnTheContract(MultiGodownCase):
	"""Unticking a property on the Contract is Lease Status Invalid on the request."""

	def setUp(self):
		super().setUp()
		self.request = fx.make_request([(A, A_START, A_END), (B, B_START, B_END)], A_START, B_END)
		self.contract = fx.make_contract(self.request.name)

	def release_b(self, vacated_on=None):
		contract = frappe.get_doc("Contract", self.contract.name)
		row = next(r for r in contract.properties if r.utility_property == B)
		row.is_active = 0
		row.vacated_on = vacated_on
		contract.save(ignore_permissions=True)
		return contract

	def test_the_vacated_on_date_is_required(self):
		with self.assertRaisesRegex(frappe.ValidationError, "vacated"):
			self.release_b()

	def test_the_request_row_follows(self):
		contract = self.release_b("2026-06-15")

		request = frappe.get_doc("Utility Service Request", self.request.name)
		row = next(r for r in request.requested_properties if r.utility_property == B)
		self.assertEqual((row.lease_status, str(row.vacated_on), row.is_active), ("Invalid", "2026-06-15", 0))
		contract_row = next(r for r in contract.properties if r.utility_property == B)
		self.assertEqual(contract_row.lease_status, "Invalid")
		other = next(r for r in request.requested_properties if r.utility_property == A)
		self.assertEqual(other.is_active, 1)
		self.assertEqual(fx.property_status(B), "Available")

	def test_lease_status_cannot_be_edited_on_the_contract_otherwise(self):
		contract = frappe.get_doc("Contract", self.contract.name)
		contract.properties[0].lease_status = "Valid"

		with self.assertRaisesRegex(frappe.ValidationError, "cannot be changed"):
			contract.save(ignore_permissions=True)
