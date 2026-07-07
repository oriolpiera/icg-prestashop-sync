from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.icg.services import ICGClientesWebWriter, ICGFacturasWebWriter
from apps.prestashop.client import (
    PrestashopAddress,
    PrestashopClient,
    PrestashopCustomerSnapshot,
    PrestashopOrderDiscountLine,
    PrestashopOrderLine,
    PrestashopOrderSnapshot,
)
from apps.sales.models import (
    ExportStatus,
    PrestashopCustomer,
    PrestashopOrder,
)
from apps.sales.models import (
    PrestashopOrderDiscountLine as MirroredDiscountLine,
)
from apps.sales.models import (
    PrestashopOrderLine as MirroredOrderLine,
)
from apps.sync.customer_export import map_snapshot_to_clientes_web
from apps.sync.order_export import map_snapshot_to_facturas_web


def refresh_customer_from_prestashop(
    prestashop_customer_id: int,
    *,
    client: PrestashopClient | None = None,
    captured_at: datetime | None = None,
) -> PrestashopCustomer:
    client = client or PrestashopClient()
    snapshot = client.get_customer_snapshot(prestashop_customer_id)
    return upsert_customer_snapshot(snapshot, captured_at=captured_at)


def upsert_customer_snapshot(
    snapshot: PrestashopCustomerSnapshot,
    *,
    captured_at: datetime | None = None,
) -> PrestashopCustomer:
    captured_at = captured_at or timezone.now()
    address = snapshot.address
    existing_customer = PrestashopCustomer.objects.filter(
        prestashop_id=snapshot.customer_id
    ).first()
    defaults = {
        "firstname": snapshot.firstname,
        "lastname": snapshot.lastname,
        "email": snapshot.email,
        "date_add": snapshot.date_add,
        "address1": _blank(address.address1 if address else None),
        "postcode": _blank(address.postcode if address else None),
        "city": _blank(address.city if address else None),
        "state": _blank(address.state if address else None),
        "country": _blank(address.country if address else None),
        "phone": _blank(address.phone if address else None),
        "phone_mobile": _blank(address.phone_mobile if address else None),
        "dni": _blank(address.dni if address else None),
        "vat_number": _blank(address.vat_number if address else None),
        "last_snapshot_at": captured_at,
    }
    customer, _ = PrestashopCustomer.objects.update_or_create(
        prestashop_id=snapshot.customer_id,
        defaults=defaults,
    )
    if existing_customer is not None and _customer_export_state_stale(existing_customer, defaults):
        customer.export_status = ExportStatus.NEVER
        customer.exported_to_icg_at = None
        customer.last_export_error = ""
        customer.last_export_inserted = None
        customer.save(
            update_fields=[
                "export_status",
                "exported_to_icg_at",
                "last_export_error",
                "last_export_inserted",
                "updated_at",
            ]
        )
    return customer


def _customer_export_state_stale(
    customer: PrestashopCustomer, incoming: dict[str, str | datetime | None]
) -> bool:
    exported_fields = (
        "firstname",
        "lastname",
        "email",
        "address1",
        "postcode",
        "city",
        "state",
        "country",
        "phone",
        "phone_mobile",
        "dni",
        "vat_number",
    )
    return any(getattr(customer, field) != incoming[field] for field in exported_fields)


def refresh_order_from_prestashop(
    prestashop_order_id: int,
    *,
    client: PrestashopClient | None = None,
    captured_at: datetime | None = None,
) -> PrestashopOrder:
    client = client or PrestashopClient()
    captured_at = captured_at or timezone.now()
    order_snapshot = client.get_order_snapshot(prestashop_order_id)
    customer_snapshot = client.get_customer_snapshot(order_snapshot.customer_id)
    with transaction.atomic():
        customer = upsert_customer_snapshot(customer_snapshot, captured_at=captured_at)
        return upsert_order_snapshot(order_snapshot, customer=customer, captured_at=captured_at)


def upsert_order_snapshot(
    snapshot: PrestashopOrderSnapshot,
    *,
    customer: PrestashopCustomer,
    captured_at: datetime | None = None,
) -> PrestashopOrder:
    captured_at = captured_at or timezone.now()
    existing_order = PrestashopOrder.objects.filter(prestashop_id=snapshot.order_id).first()
    export_state_stale = existing_order is not None and _order_export_state_stale(
        existing_order, snapshot, customer=customer
    )

    order, _ = PrestashopOrder.objects.update_or_create(
        prestashop_id=snapshot.order_id,
        defaults={
            "customer": customer,
            "payment": snapshot.payment,
            "date_add": snapshot.date_add,
            "total_paid_tax_incl": snapshot.total_paid_tax_incl,
            "total_shipping_tax_incl": snapshot.total_shipping_tax_incl,
            "total_shipping_tax_excl": snapshot.total_shipping_tax_excl,
            "last_snapshot_at": captured_at,
        },
    )
    if export_state_stale:
        order.export_status = ExportStatus.NEVER
        order.exported_to_icg_at = None
        order.last_export_error = ""
        order.inserted_rows = 0
        order.save(
            update_fields=[
                "export_status",
                "exported_to_icg_at",
                "last_export_error",
                "inserted_rows",
                "updated_at",
            ]
        )

    existing_override_by_line = {
        (
            line.position,
            line.prestashop_product_id,
            line.prestashop_combination_id,
        ): line.override_combination_id
        for line in order.lines.all()
    }

    order.lines.all().delete()
    order.discounts.all().delete()
    MirroredOrderLine.objects.bulk_create(
        [
            MirroredOrderLine(
                order=order,
                position=index,
                prestashop_product_id=line.product_id,
                prestashop_combination_id=line.combination_id,
                description=line.description,
                quantity=line.quantity,
                unit_price_tax_incl=line.unit_price_tax_incl,
                total_price_tax_incl=line.total_price_tax_incl,
                vat_rate=line.vat_rate,
                override_combination_id=existing_override_by_line.get(
                    (
                        index,
                        line.product_id,
                        line.combination_id,
                    )
                ),
            )
            for index, line in enumerate(snapshot.lines, start=1)
        ]
    )
    MirroredDiscountLine.objects.bulk_create(
        [
            MirroredDiscountLine(
                order=order,
                position=index,
                description=discount.description,
                amount_tax_incl=discount.amount_tax_incl,
                amount_tax_excl=discount.amount_tax_excl,
                vat_rate=discount.vat_rate,
            )
            for index, discount in enumerate(snapshot.discounts, start=1)
        ]
    )
    return order


def _order_export_state_stale(
    order: PrestashopOrder,
    snapshot: PrestashopOrderSnapshot,
    *,
    customer: PrestashopCustomer,
) -> bool:
    if order.customer_id != customer.pk:
        return True
    if order.payment != snapshot.payment:
        return True
    if order.date_add != snapshot.date_add:
        return True
    if order.total_paid_tax_incl != snapshot.total_paid_tax_incl:
        return True
    if order.total_shipping_tax_incl != snapshot.total_shipping_tax_incl:
        return True
    if order.total_shipping_tax_excl != snapshot.total_shipping_tax_excl:
        return True

    existing_lines = list(
        order.lines.order_by("position").values_list(
            "position",
            "prestashop_product_id",
            "prestashop_combination_id",
            "description",
            "quantity",
            "unit_price_tax_incl",
            "total_price_tax_incl",
            "vat_rate",
        )
    )
    incoming_lines = [
        (
            index,
            line.product_id,
            line.combination_id,
            line.description,
            line.quantity,
            line.unit_price_tax_incl,
            line.total_price_tax_incl,
            line.vat_rate,
        )
        for index, line in enumerate(snapshot.lines, start=1)
    ]
    if existing_lines != incoming_lines:
        return True

    existing_discounts = list(
        order.discounts.order_by("position").values_list(
            "position",
            "description",
            "amount_tax_incl",
            "amount_tax_excl",
            "vat_rate",
        )
    )
    incoming_discounts = [
        (
            index,
            discount.description,
            discount.amount_tax_incl,
            discount.amount_tax_excl,
            discount.vat_rate,
        )
        for index, discount in enumerate(snapshot.discounts, start=1)
    ]
    return existing_discounts != incoming_discounts


def export_customer_to_icg_from_mirror(
    prestashop_customer_id: int,
    *,
    writer: ICGClientesWebWriter | None = None,
    exported_at: datetime | None = None,
) -> dict[str, int | bool]:
    writer = writer or ICGClientesWebWriter()
    exported_at = exported_at or timezone.now()
    customer = PrestashopCustomer.objects.get(prestashop_id=prestashop_customer_id)
    try:
        snapshot = _customer_snapshot_from_record(customer)
        row = map_snapshot_to_clientes_web(snapshot, exported_at=exported_at)
        inserted = writer.insert_customer(row)
    except Exception as exc:
        customer.export_status = ExportStatus.FAILED
        customer.last_export_error = str(exc)
        customer.save(update_fields=["export_status", "last_export_error", "updated_at"])
        raise

    customer.export_status = ExportStatus.SUCCEEDED
    customer.exported_to_icg_at = exported_at
    customer.last_export_error = ""
    customer.last_export_inserted = inserted
    customer.save(
        update_fields=[
            "export_status",
            "exported_to_icg_at",
            "last_export_error",
            "last_export_inserted",
            "updated_at",
        ]
    )
    return {"customer_id": prestashop_customer_id, "inserted": inserted}


def export_order_to_icg_from_mirror(
    prestashop_order_id: int,
    *,
    writer: ICGFacturasWebWriter | None = None,
    exported_at: datetime | None = None,
) -> dict[str, int]:
    writer = writer or ICGFacturasWebWriter()
    exported_at = exported_at or timezone.now()
    order = (
        PrestashopOrder.objects.prefetch_related("lines", "discounts")
        .select_related("customer")
        .get(prestashop_id=prestashop_order_id)
    )
    try:
        snapshot = _order_snapshot_from_record(order)
        rows = map_snapshot_to_facturas_web(snapshot, exported_at=exported_at)
        inserted_rows = writer.insert_order_rows(rows)
    except Exception as exc:
        order.export_status = ExportStatus.FAILED
        order.last_export_error = str(exc)
        order.save(update_fields=["export_status", "last_export_error", "updated_at"])
        raise

    order.export_status = ExportStatus.SUCCEEDED
    order.exported_to_icg_at = exported_at
    order.last_export_error = ""
    order.inserted_rows = inserted_rows
    order.save(
        update_fields=[
            "export_status",
            "exported_to_icg_at",
            "last_export_error",
            "inserted_rows",
            "updated_at",
        ]
    )
    return {"order_id": prestashop_order_id, "inserted_rows": inserted_rows}


def _customer_snapshot_from_record(customer: PrestashopCustomer) -> PrestashopCustomerSnapshot:
    address = None
    if any(
        [
            customer.address1,
            customer.postcode,
            customer.city,
            customer.state,
            customer.country,
            customer.phone,
            customer.phone_mobile,
            customer.dni,
            customer.vat_number,
        ]
    ):
        address = PrestashopAddress(
            address1=_none(customer.address1),
            postcode=_none(customer.postcode),
            city=_none(customer.city),
            state=_none(customer.state),
            country=_none(customer.country),
            phone=_none(customer.phone),
            phone_mobile=_none(customer.phone_mobile),
            dni=_none(customer.dni),
            vat_number=_none(customer.vat_number),
        )
    return PrestashopCustomerSnapshot(
        customer_id=customer.prestashop_id,
        firstname=customer.firstname,
        lastname=customer.lastname,
        email=customer.email,
        date_add=customer.date_add,
        address=address,
    )


def _order_snapshot_from_record(order: PrestashopOrder) -> PrestashopOrderSnapshot:
    return PrestashopOrderSnapshot(
        order_id=order.prestashop_id,
        customer_id=order.customer.prestashop_id,
        payment=order.payment,
        date_add=order.date_add,
        total_paid_tax_incl=order.total_paid_tax_incl,
        total_shipping_tax_incl=order.total_shipping_tax_incl,
        total_shipping_tax_excl=order.total_shipping_tax_excl,
        lines=[
            PrestashopOrderLine(
                product_id=line.prestashop_product_id,
                combination_id=line.prestashop_combination_id,
                description=line.description,
                quantity=line.quantity,
                unit_price_tax_incl=line.unit_price_tax_incl,
                total_price_tax_incl=line.total_price_tax_incl,
                vat_rate=line.vat_rate,
                override_combination_id=line.override_combination_id,
            )
            for line in order.lines.all()
        ],
        discounts=[
            PrestashopOrderDiscountLine(
                description=discount.description,
                amount_tax_incl=discount.amount_tax_incl,
                amount_tax_excl=discount.amount_tax_excl,
                vat_rate=discount.vat_rate,
            )
            for discount in order.discounts.all()
        ],
    )


def _blank(value: str | None) -> str:
    return (value or "").strip()


def _none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
