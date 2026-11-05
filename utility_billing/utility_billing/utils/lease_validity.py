"""Lease Status of a leased property: Automatic, Valid or Invalid.

Each Requested Properties row (and its Contract copy) carries a Lease Status:

- **Automatic** (the default): the lease runs by its dates. The month its end
  falls in is the last one billed, in full.
- **Valid**: chosen by hand when the lease has ended but the tenant still
  occupies the property. The lease is held over: it has no end, the property
  stays occupied and billing goes on every month at the last rent.
- **Invalid**: the tenant has left on the row's Vacated On date. The row is
  released (``is_active = 0``), so its Contract releases the property too. The
  month of the Vacated On date is still billed, in full unless the invoice
  bills the occupied days only (with an attached approval).
"""

import calendar
from datetime import date

import frappe
from frappe import _
from frappe.utils import cint, getdate

AUTOMATIC = "Automatic"
VALID = "Valid"
INVALID = "Invalid"


def is_holding_over(row) -> bool:
	"""Return whether a row's lease is held over past its end (Lease Status Valid)."""
	return bool(row) and row.get("lease_status") == VALID


def is_vacated(row) -> bool:
	"""Return whether the tenant of a row has left (Lease Status Invalid)."""
	return bool(row) and row.get("lease_status") == INVALID and bool(row.get("vacated_on"))


def row_lease_end(row, fallback=None) -> date | None:
	"""Return the last day a row's lease runs to, given its Lease Status.

	A held over lease has no end. A vacated lease ends on its Vacated On date,
	or on its own end when that is earlier. Otherwise the row's end, else the
	``fallback`` (the request's or Contract's end).
	"""
	if is_holding_over(row):
		return None

	end = (row.get("end_date") if row else None) or fallback
	end = getdate(end) if end else None
	if is_vacated(row):
		vacated = getdate(row.vacated_on)
		return min(end, vacated) if end else vacated

	return end


def month_end(day) -> date:
	day = getdate(day)
	return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def same_month(a, b) -> bool:
	a, b = getdate(a), getdate(b)
	return (a.year, a.month) == (b.year, b.month)


def apply_lease_status(doc) -> None:
	"""Keep each Requested Properties row's Lease Status and Is Active in step.

	Invalid releases the row and needs its Vacated On date, which may not fall
	before the row's start. A row released by unticking Is Active keeps its
	Lease Status, so releases made before Lease Status existed stay valid.

	Raises:
		frappe.ValidationError: When an Invalid row has no Vacated On date or
			one before its start.
	"""
	for row in doc.get("requested_properties") or []:
		status = row.get("lease_status") or AUTOMATIC
		row.lease_status = status

		if status != INVALID:
			row.vacated_on = None
			continue

		if not row.get("vacated_on"):
			frappe.throw(
				_("Row {0}: set the Vacated On date of property {1} before marking its lease Invalid.").format(
					row.idx, frappe.bold(row.utility_property)
				)
			)
		if row.start_date and getdate(row.vacated_on) < getdate(row.start_date):
			frappe.throw(
				_("Row {0}: property {1} cannot be vacated on {2}, before its lease starts on {3}.").format(
					row.idx, frappe.bold(row.utility_property), row.vacated_on, row.start_date
				)
			)
		if cint(row.get("is_active")):
			row.is_active = 0


def occupied_fraction(row, billing_date) -> float:
	"""Return the share of a billing month the tenant of a vacated row occupied.

	Counts the days from the later of the month's first day and the lease start
	up to the Vacated On date, over the days in the month. Months other than the
	vacated one were occupied in full.
	"""
	if not is_vacated(row) or not same_month(row.vacated_on, billing_date):
		return 1.0

	vacated = getdate(row.vacated_on)
	first = vacated.replace(day=1)
	start = max(first, getdate(row.start_date)) if row.get("start_date") else first
	days_in_month = calendar.monthrange(vacated.year, vacated.month)[1]

	return max((vacated - start).days + 1, 0) / days_in_month
