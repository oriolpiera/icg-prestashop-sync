from datetime import datetime
from unittest.mock import Mock

from django.utils import timezone

from apps.prestashop.client import PrestashopClient


def _response(payload: str, status_code: int = 200):
    response = Mock()
    response.status_code = status_code
    response.text = payload
    return response


class TestPrestashopCustomerClient:
    def test_list_customers_created_after_filters_and_sorts(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        xml = (
            "<prestashop><customers>"
            "<customer><id>9</id><firstname>A</firstname><lastname>Late</lastname>"
            "<email>a@example.com</email><date_add>2026-06-30 10:00:00</date_add></customer>"
            "<customer><id>8</id><firstname>B</firstname><lastname>Skip</lastname>"
            "<email>b@example.com</email><date_add>2026-06-30 09:00:00</date_add></customer>"
            "<customer><id>7</id><firstname>C</firstname><lastname>Older</lastname>"
            "<email>c@example.com</email><date_add>2026-06-29 09:00:00</date_add></customer>"
            "<customer><id>11</id><firstname>D</firstname><lastname>Same date</lastname>"
            "<email>d@example.com</email><date_add>2026-06-30 09:00:00</date_add></customer>"
            "</customers></prestashop>"
        )

        session = Mock()
        session.request.return_value = _response(xml)
        client = PrestashopClient(session=session)

        cursor_at = timezone.make_aware(datetime(2026, 6, 30, 9, 0, 0))
        customers = client.list_customers_created_after(cursor_at, last_customer_id=10)

        assert [customer.customer_id for customer in customers] == [11, 9]
        assert session.request.call_args.kwargs["params"] == {
            "display": "full",
            "sort": "[date_add_ASC,id_ASC]",
            "date": "1",
            "filter[date_add]": "[2026-06-30 09:00:00,]",
        }

    def test_get_customer_snapshot_returns_none_address_when_missing(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        session = Mock()
        session.request.side_effect = [
            _response(
                "<prestashop><customer><id>42</id><firstname>Oriol</firstname>"
                "<lastname>Piera</lastname><email>oriol@example.com</email>"
                "<date_add>2026-06-30 11:00:00</date_add></customer></prestashop>"
            ),
            _response("<prestashop><addresses></addresses></prestashop>"),
        ]
        client = PrestashopClient(session=session)

        snapshot = client.get_customer_snapshot(42)

        assert snapshot.customer_id == 42
        assert snapshot.email == "oriol@example.com"
        assert snapshot.address is None
        assert session.request.call_args_list[1].kwargs["params"] == {
            "display": "full",
            "filter[id_customer]": "42",
        }

    def test_get_latest_customer_summary_uses_desc_sort(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        session = Mock()
        session.request.return_value = _response(
            "<prestashop><customers>"
            "<customer><id>42</id><firstname>Ada</firstname><lastname>Lovelace</lastname>"
            "<email>ada@example.com</email><date_add>2026-07-05 12:00:00</date_add></customer>"
            "</customers></prestashop>"
        )
        client = PrestashopClient(session=session)

        customer = client.get_latest_customer_summary()

        assert customer is not None
        assert customer.customer_id == 42
        assert session.request.call_args.kwargs["params"] == {
            "display": "full",
            "sort": "[date_add_DESC,id_DESC]",
            "limit": "1",
        }
