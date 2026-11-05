import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate
from utility_billing.utility_billing.utils.auto_repeat import cancel_auto_repeats_for_property
from utility_billing.utility_billing.utils.contract_status import (
    released_property_status,
    set_inactive_when_released,
)
from utility_billing.utility_billing.utils.request_prices import is_item_price_approach

ROW_DOCTYPE = "Contract Utility Property Item"

# Copied from the Utility Service Request by create_contract, never edited on the Contract.
# Is Active stays editable: unticking it frees the property and cancels its Auto Repeats.
LOCKED_ROW_FIELDS = (
    "utility_property",
    "item_code",
    "start_date",
    "end_date",
    "contract_length_months",
    "adjustment_rule",
    "insurance",
    "lease_status",
    "vacated_on",
)
# Released on the Contract only together with Is Active, see check_release
RELEASE_FIELDS = ("lease_status", "vacated_on")
# Copied from the Utility Service Request too, so locked while the Contract is linked to one
LOCKED_HEADER_FIELDS = ("start_date", "end_date")
DATE_FIELDS = {"start_date", "end_date"}
FLOAT_FIELDS = {"contract_length_months"}


def validate(doc: Document, method: str) -> None:
    if doc.is_new():
        check_new_contract(doc)
    else:
        check_rows_unchanged(doc, get_saved_contract(doc))


def before_update_after_submit(doc: Document, method: str) -> None:
    check_rows_unchanged(doc, get_saved_contract(doc))
    # After ERPNext's own hook, which sets the status from the dates
    set_inactive_when_released(doc)


def get_saved_contract(doc: Document) -> Document:
    return doc.get_doc_before_save() or frappe.get_doc(doc.doctype, doc.name)


def check_new_contract(doc) -> None:
    """Allow properties and a request link only on a Contract made by ``create_contract``.

    ``create_contract`` sets ``flags.from_service_request``. Flags are never read
    from a request payload, so a Contract posted through the API cannot claim it.
    A Contract without either stays a plain ERPNext Contract.

    Raises:
        frappe.ValidationError: When the Contract is amended, or was not made
            from its Utility Service Request.
    """
    if not (doc.get("properties") or doc.get("utility_service_request")):
        return

    if doc.get("amended_from"):
        frappe.throw(
            _(
                "Contract {0} has properties from a Utility Service Request, so it cannot be "
                "amended. Create a new Contract from the Utility Service Request instead."
            ).format(frappe.bold(doc.amended_from))
        )

    if not doc.flags.from_service_request:
        frappe.throw(
            _(
                "A Contract's properties come from its Utility Service Request. Create the "
                "Contract from the Utility Service Request instead."
            )
        )


def check_rows_unchanged(doc, saved) -> None:
    """Block adding, removing or editing what was copied from the request.

    That is the properties and, on a Contract linked to a request, its start
    and end dates. Compared with the saved Contract, not the request: a request
    edited after its Contract was made leaves the Contract as it is, and the
    mismatch is caught when invoicing.

    A Contract updated by its request (see ``contract_sync``) carries the server
    only ``from_service_request`` flag and is not checked: that is the one way
    its dates and rows change.

    Raises:
        frappe.ValidationError: Naming the request link, date, row or field changed.
    """
    if doc.flags.from_service_request:
        return

    if (doc.get("utility_service_request") or None) != (saved.get("utility_service_request") or None):
        frappe.throw(_("The Utility Service Request of a Contract cannot be changed."))

    if saved.get("utility_service_request"):
        contract_meta = frappe.get_meta("Contract")
        for fieldname in LOCKED_HEADER_FIELDS:
            if normalise(fieldname, doc.get(fieldname)) != normalise(fieldname, saved.get(fieldname)):
                frappe.throw(
                    _("{0} cannot be changed. It is copied from Utility Service Request {1}.").format(
                        frappe.bold(_(contract_meta.get_label(fieldname))),
                        frappe.bold(saved.utility_service_request),
                    )
                )

    saved_rows = {row.name: row for row in saved.get("properties") or []}
    meta = frappe.get_meta(ROW_DOCTYPE)

    for row in doc.get("properties") or []:
        saved_row = saved_rows.get(row.name)
        if not saved_row:
            frappe.throw(
                _(
                    "Row #{0}: Properties cannot be added to a Contract. They are copied from "
                    "its Utility Service Request."
                ).format(row.idx)
            )

        released = check_release(doc, row, saved_row)

        for fieldname in LOCKED_ROW_FIELDS:
            if released and fieldname in RELEASE_FIELDS:
                continue
            if normalise(fieldname, row.get(fieldname)) != normalise(fieldname, saved_row.get(fieldname)):
                frappe.throw(
                    _(
                        "Row #{0}: {1} cannot be changed. It is copied from the Utility Service "
                        "Request."
                    ).format(row.idx, frappe.bold(_(meta.get_label(fieldname))))
                )

        if saved.get("docstatus") == 1 and cint(row.get("is_active")) and not cint(saved_row.get("is_active")):
            frappe.throw(
                _(
                    "Row #{0}: Utility Property {1} cannot be activated again once the Contract "
                    "is submitted."
                ).format(row.idx, frappe.bold(row.utility_property))
            )

    names = {row.name for row in doc.get("properties") or []}
    removed = [row.utility_property for row in saved_rows.values() if row.name not in names]
    if removed:
        frappe.throw(
            _("Properties cannot be removed from a Contract: {0}.").format(
                ", ".join(frappe.bold(name) for name in removed)
            )
        )


def check_release(doc, row, saved_row) -> bool:
    """Require the tenant's leaving date when a property is released on the Contract.

    Unticking Is Active on a submitted Contract linked to a request ends that
    lease early: it is the same as Lease Status Invalid on the request, so the
    row must carry the Vacated On date, and the request follows on save (see
    ``release_on_request``). The month vacated in is still billed.

    Returns:
        Whether this save releases the row, so its Lease Status and Vacated On
        may change.

    Raises:
        frappe.ValidationError: When a released row has no Vacated On date, or
            one before its start.
    """
    releasing = (
        saved_row.get("docstatus") == 1
        and cint(saved_row.get("is_active"))
        and not cint(row.get("is_active"))
        and doc.get("utility_service_request")
    )
    if not releasing:
        return False

    row.lease_status = "Invalid"
    if not row.get("vacated_on"):
        frappe.throw(
            _("Row #{0}: enter the date the tenant vacated {1} to end its lease.").format(
                row.idx, frappe.bold(row.utility_property)
            ),
            title=_("Vacated On needed"),
        )
    if row.start_date and getdate(row.vacated_on) < getdate(row.start_date):
        frappe.throw(
            _("Row #{0}: {1} cannot be vacated on {2}, before its lease starts on {3}.").format(
                row.idx, frappe.bold(row.utility_property), row.vacated_on, row.start_date
            )
        )

    return True


def release_on_request(doc: Document) -> None:
    """Mark properties released on the Contract as vacated on its request.

    The request is the source of each lease: its row becomes Lease Status
    Invalid with the same Vacated On date and is released, so billing, the
    property's status and every other Contract of the request agree. This
    Contract is already saved, so it is left out of the request's sync.
    """
    if doc.flags.from_service_request or not doc.get("utility_service_request"):
        return

    released = {
        entry.utility_property: entry.vacated_on
        for entry in doc.properties
        if entry.utility_property and entry.has_value_changed("is_active") and not cint(entry.is_active)
    }
    if not released:
        return

    from utility_billing.utility_billing.utils.contract_sync import CONSENT_KEY

    request = frappe.get_doc("Utility Service Request", doc.utility_service_request)
    changed = False
    for row in request.requested_properties:
        if row.utility_property in released and cint(row.is_active):
            row.lease_status = "Invalid"
            row.vacated_on = released[row.utility_property]
            row.is_active = 0
            changed = True

    if changed:
        request.flags.released_from_contract = doc.name
        request.set(CONSENT_KEY, 1)
        request.save(ignore_permissions=True)


def normalise(fieldname: str, value):
    """Return a row value in one form, whether read from the database or a form."""
    if fieldname in DATE_FIELDS:
        return getdate(value) if value else None
    if fieldname in FLOAT_FIELDS:
        return flt(value)

    return value or None


@frappe.whitelist()
def before_submit(doc: Document, method: str) -> None:
    for entry in doc.properties:
        utility_property = entry.utility_property
        if utility_property and entry.is_active:
            status = frappe.db.get_value("Utility Property", utility_property, "status")
            if status != "Available":
                frappe.throw(_("Utility Property {0} is not available. Current status: {1}. It must be available to submit the contract.").format(utility_property, status))
            
@frappe.whitelist()
def on_submit(doc: Document, method: str) -> None:
    for entry in doc.properties:
        utility_property = entry.utility_property
       
        if utility_property and entry.is_active:
            status = frappe.db.get_value("Utility Property", utility_property, "status")
            if doc.status == "Active" and status in ("Available", "Reserved"):
                frappe.db.set_value("Utility Property", utility_property, "status", "Occupied")
            elif doc.status in ("Unsigned", "Inactive") and status in ("Available", "Reserved"):
                frappe.db.set_value("Utility Property", utility_property, "status", "Reserved")
            


@frappe.whitelist()
def on_cancel(doc: Document, method: str) -> None:
    for entry in doc.properties:
        utility_property = entry.utility_property
        if utility_property and entry.is_active:
            cancel_auto_repeats_for_property(utility_property)
            frappe.db.set_value("Utility Property", utility_property, "status", "Available")
            

@frappe.whitelist()
def on_update_after_submit(doc: Document, method: str) -> None:
    release_on_request(doc)
    status_changed = doc.has_value_changed("status")
    current_status = doc.status

    for entry in doc.properties:
        utility_property = entry.utility_property
        if not utility_property:
            continue

        # Reactivating a row after submit is blocked in check_rows_unchanged

        if current_status == "Active" and entry.is_active:
            frappe.db.set_value("Utility Property", utility_property, "status", "Occupied")
            continue

        # Under Item Price a row unticked in the same save that changes the status
        # (the last row released makes the Contract Inactive) is freed too.
        unticked = entry.has_value_changed("is_active") and not entry.is_active
        should_set_available = (
            (status_changed and entry.is_active) or
            (unticked and (not status_changed or is_item_price_approach()))
        )

        if should_set_available:
            cancel_auto_repeats_for_property(utility_property)
            frappe.db.set_value(
                "Utility Property", utility_property, "status", released_property_status(doc, entry)
            )



