from unittest.mock import Mock

from apps.prestashop.client import PrestashopClient


def _response(payload: str, status_code: int = 200):
    response = Mock()
    response.status_code = status_code
    response.text = payload
    return response


class TestFindAttributeValueId:
    def _list_values_xml(self) -> str:
        return (
            "<prestashop>"
            "<product_option_values>"
            "<product_option_value id='42'>"
            "<id>42</id>"
            "<id_attribute_group>10</id_attribute_group>"
            "<name>"
            "<language id='1'>1,3X5,5CM</language>"
            "</name>"
            "</product_option_value>"
            "</product_option_values>"
            "</prestashop>"
        )

    def test_with_commas_falls_back_to_list(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        session = Mock()
        session.request.return_value = _response(self._list_values_xml())
        client = PrestashopClient(session=session)

        ps_id = client.find_attribute_value_id("1,3X5,5CM", 10)
        assert ps_id == 42

        assert session.request.call_count == 1
        call_args = session.request.call_args
        assert "product_option_values" in str(call_args[0][1])
        assert call_args[1]["params"] == {
            "filter[id_attribute_group]": "10",
            "display": "full",
        }

    def test_with_brackets_falls_back_to_list(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        xml = (
            "<prestashop>"
            "<product_option_values>"
            "<product_option_value id='77'>"
            "<id>77</id>"
            "<id_attribute_group>10</id_attribute_group>"
            "<name>"
            "<language id='1'>[ 30P]</language>"
            "</name>"
            "</product_option_value>"
            "</product_option_values>"
            "</prestashop>"
        )
        session = Mock()
        session.request.return_value = _response(xml)
        client = PrestashopClient(session=session)

        ps_id = client.find_attribute_value_id("[ 30P]", 10)
        assert ps_id == 77

    def test_with_commas_returns_none_when_not_found(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        xml = (
            "<prestashop>"
            "<product_option_values>"
            "<product_option_value id='42'>"
            "<id>42</id>"
            "<id_attribute_group>10</id_attribute_group>"
            "<name>"
            "<language id='1'>M</language>"
            "</name>"
            "</product_option_value>"
            "</product_option_values>"
            "</prestashop>"
        )
        session = Mock()
        session.request.return_value = _response(xml)
        client = PrestashopClient(session=session)

        ps_id = client.find_attribute_value_id("1,3X5,5CM", 10)
        assert ps_id is None

    def test_normal_values_still_use_filter(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        xml = (
            "<prestashop>"
            "<product_option_values>"
            "<product_option_value id='42'>"
            "<id>42</id>"
            "<name>"
            "<language id='1'>M</language>"
            "</name>"
            "</product_option_value>"
            "</product_option_values>"
            "</prestashop>"
        )
        session = Mock()
        session.request.return_value = _response(xml)
        client = PrestashopClient(session=session)

        ps_id = client.find_attribute_value_id("M", 10)
        assert ps_id == 42

        assert session.request.call_count == 1
        call_args = session.request.call_args
        assert call_args[1]["params"] == {
            "filter[id_attribute_group]": "10",
            "filter[name]": "[M]",
            "limit": "1",
        }


class TestFindAttributeGroupIdByName:
    def _list_groups_xml(self) -> str:
        return (
            "<prestashop>"
            "<product_options>"
            "<product_option id='5'>"
            "<id>5</id>"
            "<name>"
            "<language id='1'>Size,Color</language>"
            "</name>"
            "</product_option>"
            "</product_options>"
            "</prestashop>"
        )

    def test_with_commas_falls_back_to_list(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        session = Mock()
        session.request.return_value = _response(self._list_groups_xml())
        client = PrestashopClient(session=session)

        ps_id = client.find_attribute_group_id_by_name("Size,Color")
        assert ps_id == 5

        assert session.request.call_count == 1
        call_args = session.request.call_args
        assert "product_options" in str(call_args[0][1])
        assert call_args[1]["params"] == {"display": "full"}

    def test_with_reserved_chars_returns_none_when_not_found(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        xml = (
            "<prestashop>"
            "<product_options>"
            "<product_option id='5'>"
            "<id>5</id>"
            "<name>"
            "<language id='1'>Size</language>"
            "</name>"
            "</product_option>"
            "</product_options>"
            "</prestashop>"
        )
        session = Mock()
        session.request.return_value = _response(xml)
        client = PrestashopClient(session=session)

        ps_id = client.find_attribute_group_id_by_name("Color,Size")
        assert ps_id is None

    def test_normal_values_still_use_filter(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        xml = (
            "<prestashop>"
            "<product_options>"
            "<product_option id='5'>"
            "<id>5</id>"
            "<name>"
            "<language id='1'>Size</language>"
            "</name>"
            "</product_option>"
            "</product_options>"
            "</prestashop>"
        )
        session = Mock()
        session.request.return_value = _response(xml)
        client = PrestashopClient(session=session)

        ps_id = client.find_attribute_group_id_by_name("Size")
        assert ps_id == 5

        assert session.request.call_count == 1
        call_args = session.request.call_args
        assert call_args[1]["params"] == {"filter[name]": "[Size]", "limit": "1"}
