from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock

from django.utils import timezone

from apps.prestashop.client import PrestashopClient


def _response(payload: str, status_code: int = 200):
    response = Mock()
    response.status_code = status_code
    response.text = payload
    return response


class TestPrestashopOrderClient:
    def test_list_orders_created_after_filters_and_sorts(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        xml = (
            "<prestashop><orders>"
            "<order><id>9</id><id_customer>4</id_customer><payment>Card</payment>"
            "<date_add>2026-06-30 10:00:00</date_add></order>"
            "<order><id>8</id><id_customer>4</id_customer><payment>Card</payment>"
            "<date_add>2026-06-30 09:00:00</date_add></order>"
            "<order><id>11</id><id_customer>4</id_customer><payment>Bank wire</payment>"
            "<date_add>2026-06-30 09:00:00</date_add></order>"
            "</orders></prestashop>"
        )

        session = Mock()
        session.request.return_value = _response(xml)
        client = PrestashopClient(session=session)

        cursor_at = timezone.make_aware(datetime(2026, 6, 30, 9, 0, 0))
        orders = client.list_orders_created_after(cursor_at, last_order_id=10)

        assert [order.order_id for order in orders] == [11]
        assert session.request.call_args.kwargs["params"] == {
            "display": "full",
            "sort": "[id_ASC]",
            "filter[id]": "[11,]",
        }

    def test_get_order_snapshot_reads_lines_and_discounts(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><order><id>42</id><id_customer>7</id_customer>"
                "<payment>Redsys Card</payment><date_add>2026-06-30 11:00:00</date_add>"
                "<total_paid_tax_incl>100.00</total_paid_tax_incl>"
                "<total_shipping_tax_incl>12.10</total_shipping_tax_incl>"
                "<total_shipping_tax_excl>10.00</total_shipping_tax_excl>"
                "<associations><order_rows>"
                "<order_row><id>901</id><product_id>100</product_id><product_attribute_id>200</product_attribute_id>"
                "<product_name>Blue mug</product_name><product_quantity>2</product_quantity>"
                "<unit_price_tax_incl>24.20</unit_price_tax_incl>"
                "</order_row></order_rows></associations>"
                "</order></prestashop>"
            ),
            _response(
                "<prestashop><order_details>"
                "<order_detail><id>901</id><id_order>42</id_order>"
                "<unit_price_tax_incl>24.20</unit_price_tax_incl>"
                "<total_price_tax_incl>48.40</total_price_tax_incl>"
                "<total_price_tax_excl>40.00</total_price_tax_excl>"
                "</order_detail>"
                "</order_details></prestashop>"
            ),
            _response(
                "<prestashop><order_cart_rules>"
                "<order_cart_rule><name>Summer promo</name><value_tax_incl>6.05</value_tax_incl>"
                "<value_tax_excl>5.00</value_tax_excl></order_cart_rule>"
                "</order_cart_rules></prestashop>"
            ),
        ]
        client = PrestashopClient(session=session)

        snapshot = client.get_order_snapshot(42)

        assert snapshot.order_id == 42
        assert snapshot.customer_id == 7
        assert snapshot.payment == "Redsys Card"
        assert snapshot.total_paid_tax_incl == Decimal("100.00")
        assert len(snapshot.lines) == 1
        assert snapshot.lines[0].order_detail_id == 901
        assert snapshot.lines[0].combination_id == 200
        assert snapshot.lines[0].unit_price_tax_incl == Decimal("24.20")
        assert snapshot.lines[0].total_price_tax_incl == Decimal("48.40")
        assert snapshot.lines[0].vat_rate == Decimal("21.00")
        assert len(snapshot.discounts) == 1
        assert snapshot.discounts[0].vat_rate == Decimal("21.00")
        assert session.request.call_args_list[1].kwargs["params"] == {
            "display": "full",
            "filter[id_order]": "42",
        }
        assert session.request.call_args_list[2].kwargs["params"] == {
            "display": "full",
            "filter[id_order]": "42",
        }

    def test_get_order_snapshot_falls_back_to_order_rows_when_order_details_fail(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><order><id>42</id><id_customer>7</id_customer>"
                "<payment>Redsys Card</payment><date_add>2026-06-30 11:00:00</date_add>"
                "<total_paid_tax_incl>100.00</total_paid_tax_incl>"
                "<total_shipping_tax_incl>12.10</total_shipping_tax_incl>"
                "<total_shipping_tax_excl>10.00</total_shipping_tax_excl>"
                "<associations><order_rows>"
                "<order_row><id>901</id><product_id>100</product_id><product_attribute_id>200</product_attribute_id>"
                "<product_name>Blue mug</product_name><product_quantity>2</product_quantity>"
                "<unit_price_tax_incl>24.20</unit_price_tax_incl>"
                "<total_price_tax_incl>48.40</total_price_tax_incl><tax_rate>21.00</tax_rate>"
                "</order_row></order_rows></associations>"
                "</order></prestashop>"
            ),
            _response("<errors></errors>", status_code=403),
            _response(
                "<prestashop><order_cart_rules>"
                "<order_cart_rule><name>Summer promo</name><value_tax_incl>6.05</value_tax_incl>"
                "<value_tax_excl>5.00</value_tax_excl></order_cart_rule>"
                "</order_cart_rules></prestashop>"
            ),
        ]
        client = PrestashopClient(session=session)

        snapshot = client.get_order_snapshot(42)

        assert snapshot.lines[0].total_price_tax_incl == Decimal("48.40")
        assert snapshot.lines[0].vat_rate == Decimal("21.00")

    def test_get_order_snapshot_preserves_order_row_values_when_detail_fields_are_missing(
        self, settings
    ):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><order><id>42</id><id_customer>7</id_customer>"
                "<payment>Redsys Card</payment><date_add>2026-06-30 11:00:00</date_add>"
                "<total_paid_tax_incl>100.00</total_paid_tax_incl>"
                "<total_shipping_tax_incl>12.10</total_shipping_tax_incl>"
                "<total_shipping_tax_excl>10.00</total_shipping_tax_excl>"
                "<associations><order_rows>"
                "<order_row><id>901</id><product_id>100</product_id><product_attribute_id>200</product_attribute_id>"
                "<product_name>Blue mug</product_name><product_quantity>2</product_quantity>"
                "<unit_price_tax_incl>24.20</unit_price_tax_incl>"
                "<total_price_tax_incl>48.40</total_price_tax_incl><tax_rate>21.00</tax_rate>"
                "</order_row></order_rows></associations>"
                "</order></prestashop>"
            ),
            _response(
                "<prestashop><order_details>"
                "<order_detail><id>901</id><id_order>42</id_order></order_detail>"
                "</order_details></prestashop>"
            ),
            _response("<prestashop><order_cart_rules></order_cart_rules></prestashop>"),
        ]
        client = PrestashopClient(session=session)

        snapshot = client.get_order_snapshot(42)

        assert snapshot.lines[0].unit_price_tax_incl == Decimal("24.20")
        assert snapshot.lines[0].total_price_tax_incl == Decimal("48.40")
        assert snapshot.lines[0].vat_rate == Decimal("21.00")

    def test_get_order_snapshot_preserves_explicit_order_row_tax_rate_over_derived_rounded_value(
        self, settings
    ):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><order><id>42</id><id_customer>7</id_customer>"
                "<payment>Redsys Card</payment><date_add>2026-06-30 11:00:00</date_add>"
                "<total_paid_tax_incl>100.00</total_paid_tax_incl>"
                "<total_shipping_tax_incl>12.10</total_shipping_tax_incl>"
                "<total_shipping_tax_excl>10.00</total_shipping_tax_excl>"
                "<associations><order_rows>"
                "<order_row><id>901</id><product_id>100</product_id><product_attribute_id>200</product_attribute_id>"
                "<product_name>Blue mug</product_name><product_quantity>2</product_quantity>"
                "<unit_price_tax_incl>24.20</unit_price_tax_incl>"
                "<tax_rate>21.00</tax_rate>"
                "</order_row></order_rows></associations>"
                "</order></prestashop>"
            ),
            _response(
                "<prestashop><order_details>"
                "<order_detail><id>901</id><id_order>42</id_order>"
                "<unit_price_tax_incl>24.20</unit_price_tax_incl>"
                "<total_price_tax_incl>48.41</total_price_tax_incl>"
                "<total_price_tax_excl>40.00</total_price_tax_excl>"
                "</order_detail>"
                "</order_details></prestashop>"
            ),
            _response("<prestashop><order_cart_rules></order_cart_rules></prestashop>"),
        ]
        client = PrestashopClient(session=session)

        snapshot = client.get_order_snapshot(42)

        assert snapshot.lines[0].total_price_tax_incl == Decimal("48.41")
        assert snapshot.lines[0].vat_rate == Decimal("21.00")

    def test_get_latest_order_summary_uses_desc_sort(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        session = Mock()
        session.request.return_value = _response(
            "<prestashop><orders>"
            "<order><id>77</id><id_customer>42</id_customer><payment>Redsys Card</payment>"
            "<date_add>2026-07-05 13:00:00</date_add></order>"
            "</orders></prestashop>"
        )
        client = PrestashopClient(session=session)

        order = client.get_latest_order_summary()

        assert order is not None
        assert order.order_id == 77
        assert session.request.call_args.kwargs["params"] == {
            "display": "full",
            "sort": "[id_DESC]",
            "limit": "1",
        }
