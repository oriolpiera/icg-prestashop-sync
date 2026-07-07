from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.catalog.models import Combination, Product
from apps.prestashop.client import (
    PrestashopAddress,
    PrestashopCustomerSnapshot,
    PrestashopOrderDiscountLine,
    PrestashopOrderLine,
    PrestashopOrderSnapshot,
)
from apps.sales.models import ExportStatus, PrestashopCustomer, PrestashopOrder
from apps.sales.services import (
    export_customer_to_icg_from_mirror,
    export_order_to_icg_from_mirror,
    refresh_order_from_prestashop,
    upsert_customer_snapshot,
)


def _aware(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0):
    return timezone.make_aware(datetime(year, month, day, hour, minute, second))


@pytest.fixture(autouse=True)
def _clean_db(request):
    if request.node.get_closest_marker("django_db"):
        PrestashopOrder.objects.all().delete()
        PrestashopCustomer.objects.all().delete()
        Combination.objects.all().delete()
        Product.objects.all().delete()


@pytest.mark.django_db
def test_upsert_customer_snapshot_persists_address_data():
    customer = upsert_customer_snapshot(
        PrestashopCustomerSnapshot(
            customer_id=42,
            firstname="Ada",
            lastname="Lovelace",
            email="ada@example.com",
            date_add=_aware(2026, 7, 1, 10),
            address=PrestashopAddress(
                address1="Main street 1",
                postcode="08001",
                city="Barcelona",
                state="Barcelona",
                country="Spain",
                phone="931000000",
                phone_mobile="600000000",
                dni="12345678A",
                vat_number=None,
            ),
        ),
        captured_at=_aware(2026, 7, 1, 12),
    )

    assert customer.prestashop_id == 42
    assert customer.city == "Barcelona"
    assert customer.last_snapshot_at == _aware(2026, 7, 1, 12)


@pytest.mark.django_db
def test_upsert_customer_snapshot_preserves_export_state():
    customer = PrestashopCustomer.objects.create(
        prestashop_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 10),
        address1="Main street 1",
        postcode="08001",
        city="Barcelona",
        state="Barcelona",
        country="Spain",
        phone="931000000",
        phone_mobile="600000000",
        dni="12345678A",
        last_snapshot_at=_aware(2026, 7, 1, 11),
        export_status=ExportStatus.FAILED,
        exported_to_icg_at=_aware(2026, 7, 1, 12),
        last_export_error="old error",
        last_export_inserted=False,
    )

    refreshed = upsert_customer_snapshot(
        PrestashopCustomerSnapshot(
            customer_id=42,
            firstname="Ada",
            lastname="Lovelace",
            email="ada@example.com",
            date_add=_aware(2026, 7, 1, 10),
            address=PrestashopAddress(
                address1="Main street 1",
                postcode="08001",
                city="Barcelona",
                state="Barcelona",
                country="Spain",
                phone="931000000",
                phone_mobile="600000000",
                dni="12345678A",
                vat_number=None,
            ),
        ),
        captured_at=_aware(2026, 7, 1, 13),
    )

    customer.refresh_from_db()
    assert refreshed.pk == customer.pk
    assert customer.export_status == ExportStatus.FAILED
    assert customer.exported_to_icg_at == _aware(2026, 7, 1, 12)
    assert customer.last_export_error == "old error"
    assert customer.last_export_inserted is False


@pytest.mark.django_db
def test_upsert_customer_snapshot_resets_export_state_when_exported_fields_change():
    customer = PrestashopCustomer.objects.create(
        prestashop_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 10),
        address1="Main street 1",
        postcode="08001",
        city="Barcelona",
        state="Barcelona",
        country="Spain",
        phone="931000000",
        phone_mobile="600000000",
        dni="12345678A",
        last_snapshot_at=_aware(2026, 7, 1, 11),
        export_status=ExportStatus.SUCCEEDED,
        exported_to_icg_at=_aware(2026, 7, 1, 12),
        last_export_error="",
        last_export_inserted=True,
    )

    refreshed = upsert_customer_snapshot(
        PrestashopCustomerSnapshot(
            customer_id=42,
            firstname="Ada",
            lastname="Lovelace",
            email="ada+new@example.com",
            date_add=_aware(2026, 7, 1, 10),
            address=PrestashopAddress(
                address1="Main street 1",
                postcode="08001",
                city="Barcelona",
                state="Barcelona",
                country="Spain",
                phone="931000000",
                phone_mobile="600000000",
                dni="12345678A",
                vat_number=None,
            ),
        ),
        captured_at=_aware(2026, 7, 1, 13),
    )

    customer.refresh_from_db()
    assert refreshed.pk == customer.pk
    assert customer.export_status == ExportStatus.NEVER
    assert customer.exported_to_icg_at is None
    assert customer.last_export_error == ""
    assert customer.last_export_inserted is None


@pytest.mark.django_db
def test_refresh_order_from_prestashop_replaces_lines_and_discounts():
    client = Mock()
    client.get_order_snapshot.return_value = PrestashopOrderSnapshot(
        order_id=77,
        customer_id=42,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("100.00"),
        total_shipping_tax_incl=Decimal("12.10"),
        total_shipping_tax_excl=Decimal("10.00"),
        lines=[
            PrestashopOrderLine(
                product_id=101,
                combination_id=202,
                description="Blue mug",
                quantity=2,
                unit_price_tax_incl=Decimal("24.20"),
                total_price_tax_incl=Decimal("48.40"),
                vat_rate=Decimal("21.00"),
            )
        ],
        discounts=[
            PrestashopOrderDiscountLine(
                description="Summer promo",
                amount_tax_incl=Decimal("6.05"),
                amount_tax_excl=Decimal("5.00"),
                vat_rate=Decimal("21.00"),
            )
        ],
    )
    client.get_customer_snapshot.return_value = PrestashopCustomerSnapshot(
        customer_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 9),
        address=None,
    )

    order = refresh_order_from_prestashop(77, client=client, captured_at=_aware(2026, 7, 1, 12))
    assert order.lines.count() == 1
    assert order.discounts.count() == 1

    client.get_order_snapshot.return_value = PrestashopOrderSnapshot(
        order_id=77,
        customer_id=42,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("120.00"),
        total_shipping_tax_incl=Decimal("0.00"),
        total_shipping_tax_excl=Decimal("0.00"),
        lines=[],
        discounts=[],
    )

    order = refresh_order_from_prestashop(77, client=client, captured_at=_aware(2026, 7, 1, 13))
    assert order.lines.count() == 0
    assert order.discounts.count() == 0
    assert order.total_paid_tax_incl == Decimal("120.00")
    assert order.last_snapshot_at == _aware(2026, 7, 1, 13)


@pytest.mark.django_db
def test_refresh_order_from_prestashop_preserves_export_state():
    customer = PrestashopCustomer.objects.create(
        prestashop_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 9),
        last_snapshot_at=_aware(2026, 7, 1, 10),
    )
    PrestashopOrder.objects.create(
        prestashop_id=77,
        customer=customer,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("100.00"),
        total_shipping_tax_incl=Decimal("12.10"),
        total_shipping_tax_excl=Decimal("10.00"),
        last_snapshot_at=_aware(2026, 7, 1, 11),
        export_status=ExportStatus.FAILED,
        exported_to_icg_at=_aware(2026, 7, 1, 12),
        last_export_error="old error",
        inserted_rows=3,
    )
    client = Mock()
    client.get_order_snapshot.return_value = PrestashopOrderSnapshot(
        order_id=77,
        customer_id=42,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("100.00"),
        total_shipping_tax_incl=Decimal("12.10"),
        total_shipping_tax_excl=Decimal("10.00"),
        lines=[],
        discounts=[],
    )
    client.get_customer_snapshot.return_value = PrestashopCustomerSnapshot(
        customer_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 9),
        address=None,
    )

    order = refresh_order_from_prestashop(77, client=client, captured_at=_aware(2026, 7, 1, 13))

    assert order.export_status == ExportStatus.FAILED
    assert order.exported_to_icg_at == _aware(2026, 7, 1, 12)
    assert order.last_export_error == "old error"
    assert order.inserted_rows == 3


@pytest.mark.django_db
def test_refresh_order_from_prestashop_resets_export_state_when_lines_change():
    customer = PrestashopCustomer.objects.create(
        prestashop_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 9),
        last_snapshot_at=_aware(2026, 7, 1, 10),
    )
    order = PrestashopOrder.objects.create(
        prestashop_id=77,
        customer=customer,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("100.00"),
        total_shipping_tax_incl=Decimal("12.10"),
        total_shipping_tax_excl=Decimal("10.00"),
        last_snapshot_at=_aware(2026, 7, 1, 11),
        export_status=ExportStatus.SUCCEEDED,
        exported_to_icg_at=_aware(2026, 7, 1, 12),
        last_export_error="",
        inserted_rows=3,
    )
    order.lines.create(
        position=1,
        prestashop_product_id=101,
        prestashop_combination_id=202,
        description="Blue mug",
        quantity=1,
        unit_price_tax_incl=Decimal("24.20"),
        total_price_tax_incl=Decimal("24.20"),
        vat_rate=Decimal("21.00"),
    )
    client = Mock()
    client.get_order_snapshot.return_value = PrestashopOrderSnapshot(
        order_id=77,
        customer_id=42,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("100.00"),
        total_shipping_tax_incl=Decimal("12.10"),
        total_shipping_tax_excl=Decimal("10.00"),
        lines=[],
        discounts=[],
    )
    client.get_customer_snapshot.return_value = PrestashopCustomerSnapshot(
        customer_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 9),
        address=None,
    )

    refreshed = refresh_order_from_prestashop(77, client=client, captured_at=_aware(2026, 7, 1, 13))

    assert refreshed.export_status == ExportStatus.NEVER
    assert refreshed.exported_to_icg_at is None
    assert refreshed.last_export_error == ""
    assert refreshed.inserted_rows == 0


@pytest.mark.django_db
def test_export_customer_to_icg_from_mirror_updates_status():
    customer = PrestashopCustomer.objects.create(
        prestashop_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 10),
        last_snapshot_at=_aware(2026, 7, 1, 12),
    )
    writer = Mock()
    writer.insert_customer.return_value = True

    result = export_customer_to_icg_from_mirror(
        42,
        writer=writer,
        exported_at=_aware(2026, 7, 1, 14),
    )

    customer.refresh_from_db()
    assert result == {"customer_id": 42, "inserted": True}
    assert customer.export_status == ExportStatus.SUCCEEDED
    assert customer.last_export_inserted is True
    assert customer.exported_to_icg_at == _aware(2026, 7, 1, 14)


@pytest.mark.django_db
def test_export_order_to_icg_from_mirror_updates_status():
    product = Product.objects.create(
        icg_id=5001,
        prestashop_id=101,
        reference="MUG-001",
        name="Blue mug",
    )
    Combination.objects.create(
        product=product,
        prestashop_id=202,
        icg_size="UNI",
        icg_color="BLUE",
        ean13="1234567890123",
    )
    customer = PrestashopCustomer.objects.create(
        prestashop_id=7,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 9),
        last_snapshot_at=_aware(2026, 7, 1, 12),
    )
    order = PrestashopOrder.objects.create(
        prestashop_id=42,
        customer=customer,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("100.00"),
        total_shipping_tax_incl=Decimal("12.10"),
        total_shipping_tax_excl=Decimal("10.00"),
        last_snapshot_at=_aware(2026, 7, 1, 12),
    )
    order.lines.create(
        position=1,
        prestashop_product_id=101,
        prestashop_combination_id=202,
        description="Blue mug",
        quantity=2,
        unit_price_tax_incl=Decimal("24.20"),
        total_price_tax_incl=Decimal("48.40"),
        vat_rate=Decimal("21.00"),
    )
    order.discounts.create(
        position=1,
        description="Summer promo",
        amount_tax_incl=Decimal("6.05"),
        amount_tax_excl=Decimal("5.00"),
        vat_rate=Decimal("21.00"),
    )
    writer = Mock()
    writer.insert_order_rows.return_value = 3

    result = export_order_to_icg_from_mirror(42, writer=writer, exported_at=_aware(2026, 7, 1, 14))

    order.refresh_from_db()
    assert result == {"order_id": 42, "inserted_rows": 3}
    assert order.export_status == ExportStatus.SUCCEEDED
    assert order.inserted_rows == 3
    assert order.exported_to_icg_at == _aware(2026, 7, 1, 14)


@pytest.mark.django_db
def test_export_order_to_icg_from_mirror_uses_order_line_override_combination():
    product = Product.objects.create(
        icg_id=5001,
        prestashop_id=101,
        reference="MUG-001",
        name="Blue mug",
    )
    original_combination = Combination.objects.create(
        product=product,
        prestashop_id=202,
        icg_size="UNI",
        icg_color="BLUE",
        ean13="1234567890123",
    )
    override_combination = Combination.objects.create(
        product=product,
        prestashop_id=304,
        icg_size="XL",
        icg_color="GREEN",
        ean13="9876543210987",
    )
    customer = PrestashopCustomer.objects.create(
        prestashop_id=7,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 9),
        last_snapshot_at=_aware(2026, 7, 1, 12),
    )
    order = PrestashopOrder.objects.create(
        prestashop_id=42,
        customer=customer,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("48.40"),
        total_shipping_tax_incl=Decimal("0.00"),
        total_shipping_tax_excl=Decimal("0.00"),
        last_snapshot_at=_aware(2026, 7, 1, 12),
    )
    order.lines.create(
        position=1,
        prestashop_product_id=101,
        prestashop_combination_id=original_combination.prestashop_id,
        description="Blue mug",
        quantity=2,
        unit_price_tax_incl=Decimal("24.20"),
        total_price_tax_incl=Decimal("48.40"),
        vat_rate=Decimal("21.00"),
        override_combination=override_combination,
    )
    writer = Mock()
    writer.insert_order_rows.return_value = 1

    export_order_to_icg_from_mirror(42, writer=writer, exported_at=_aware(2026, 7, 1, 14))

    inserted_rows = writer.insert_order_rows.call_args.args[0]
    assert inserted_rows[0].cod_articulo == 5001
    assert inserted_rows[0].talla == "XL"
    assert inserted_rows[0].color == "GREEN"
    assert inserted_rows[0].cod_barras == "9876543210987"


@pytest.mark.django_db
def test_refresh_order_from_prestashop_preserves_override_combination_for_matching_line():
    override_product = Product.objects.create(
        icg_id=5002,
        prestashop_id=102,
        reference="MUG-002",
        name="Green mug",
    )
    override_combination = Combination.objects.create(
        product=override_product,
        prestashop_id=303,
        icg_size="XL",
        icg_color="GREEN",
        ean13="9876543210987",
    )
    customer = PrestashopCustomer.objects.create(
        prestashop_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 9),
        last_snapshot_at=_aware(2026, 7, 1, 10),
    )
    order = PrestashopOrder.objects.create(
        prestashop_id=77,
        customer=customer,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("100.00"),
        total_shipping_tax_incl=Decimal("12.10"),
        total_shipping_tax_excl=Decimal("10.00"),
        last_snapshot_at=_aware(2026, 7, 1, 11),
    )
    order.lines.create(
        position=1,
        prestashop_product_id=101,
        prestashop_combination_id=202,
        description="Blue mug",
        quantity=2,
        unit_price_tax_incl=Decimal("24.20"),
        total_price_tax_incl=Decimal("48.40"),
        vat_rate=Decimal("21.00"),
        override_combination=override_combination,
    )

    client = Mock()
    client.get_order_snapshot.return_value = PrestashopOrderSnapshot(
        order_id=77,
        customer_id=42,
        payment="Redsys Card",
        date_add=_aware(2026, 7, 1, 10),
        total_paid_tax_incl=Decimal("100.00"),
        total_shipping_tax_incl=Decimal("12.10"),
        total_shipping_tax_excl=Decimal("10.00"),
        lines=[
            PrestashopOrderLine(
                product_id=101,
                combination_id=202,
                description="Blue mug",
                quantity=2,
                unit_price_tax_incl=Decimal("24.20"),
                total_price_tax_incl=Decimal("48.40"),
                vat_rate=Decimal("21.00"),
            )
        ],
        discounts=[],
    )
    client.get_customer_snapshot.return_value = PrestashopCustomerSnapshot(
        customer_id=42,
        firstname="Ada",
        lastname="Lovelace",
        email="ada@example.com",
        date_add=_aware(2026, 7, 1, 9),
        address=None,
    )

    refreshed = refresh_order_from_prestashop(77, client=client, captured_at=_aware(2026, 7, 1, 12))

    line = refreshed.lines.get(position=1)
    assert line.override_combination_id == override_combination.pk
