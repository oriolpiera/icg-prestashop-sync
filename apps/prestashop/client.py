from dataclasses import dataclass
from typing import Protocol, cast
from xml.etree import ElementTree

import requests
from django.conf import settings
from requests import Response, Session
from requests.auth import HTTPBasicAuth


class PrestashopSettings(Protocol):
    PRESTASHOP_BASE_URL: str
    PRESTASHOP_API_KEY: str


@dataclass(slots=True)
class PrestashopCredentials:
    base_url: str
    api_key: str


class PrestashopError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class PrestashopClient:
    def __init__(self, session: Session | None = None) -> None:
        self.session = session or requests.Session()

    def credentials(self) -> PrestashopCredentials:
        typed_settings = cast(PrestashopSettings, settings)
        return PrestashopCredentials(
            base_url=typed_settings.PRESTASHOP_BASE_URL,
            api_key=typed_settings.PRESTASHOP_API_KEY,
        )

    def _api_url(self, resource: str, resource_id: int | None = None) -> str:
        credentials = self.credentials()
        base_url = credentials.base_url.rstrip("/")
        if not base_url or not credentials.api_key:
            raise PrestashopError("Prestashop credentials are not configured.")

        url = f"{base_url}/api/{resource}"
        if resource_id is not None:
            url = f"{url}/{resource_id}"
        return url

    def _auth(self) -> HTTPBasicAuth:
        return HTTPBasicAuth(self.credentials().api_key, "")

    def _request(
        self,
        method: str,
        resource: str,
        *,
        resource_id: int | None = None,
        params: dict[str, str] | None = None,
        data: str | None = None,
    ) -> Response:
        try:
            response = self.session.request(
                method,
                self._api_url(resource, resource_id),
                params=params,
                data=data,
                auth=self._auth(),
                headers={"Content-Type": "application/xml"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise PrestashopError(f"Prestashop request failed: {exc}") from exc

        if response.status_code >= 400:
            raise PrestashopError(
                f"Prestashop returned HTTP {response.status_code} for {resource}.",
                status_code=response.status_code,
                body=response.text,
            )
        return response

    def find_manufacturer_id_by_name(self, name: str) -> int | None:
        response = self._request(
            "GET",
            "manufacturers",
            params={"filter[name]": f"[{name}]", "limit": "1"},
        )
        root = self._parse_xml(response.text)
        manufacturer = root.find("./manufacturers/manufacturer")
        if manufacturer is None:
            return None

        manufacturer_id = manufacturer.attrib.get("id")
        if not manufacturer_id:
            manufacturer_id = manufacturer.findtext("id")
        if not manufacturer_id:
            raise PrestashopError("Prestashop manufacturer search response did not include an id.")
        return int(manufacturer_id)

    def get_manufacturer_xml(self, manufacturer_id: int) -> ElementTree.Element:
        response = self._request("GET", "manufacturers", resource_id=manufacturer_id)
        return self._parse_xml(response.text)

    def create_manufacturer(self, name: str) -> int:
        response = self._request("POST", "manufacturers", data=self._manufacturer_xml(name))
        root = self._parse_xml(response.text)
        manufacturer_id = root.findtext("./manufacturer/id")
        if not manufacturer_id:
            raise PrestashopError("Prestashop create manufacturer response did not include an id.")
        return int(manufacturer_id)

    def update_manufacturer(self, manufacturer_id: int, name: str) -> None:
        root = self.get_manufacturer_xml(manufacturer_id)
        name_node = root.find("./manufacturer/name")
        if name_node is None:
            raise PrestashopError("Prestashop manufacturer payload did not include a name node.")
        name_node.text = name
        payload = ElementTree.tostring(root, encoding="unicode")
        self._request("PUT", "manufacturers", resource_id=manufacturer_id, data=payload)

    def _manufacturer_xml(self, name: str) -> str:
        root = ElementTree.Element("prestashop", {"xmlns:xlink": "http://www.w3.org/1999/xlink"})
        manufacturer = ElementTree.SubElement(root, "manufacturer")
        name_node = ElementTree.SubElement(manufacturer, "name")
        name_node.text = name
        active = ElementTree.SubElement(manufacturer, "active")
        active.text = "1"
        return ElementTree.tostring(root, encoding="unicode")

    def _parse_xml(self, payload: str) -> ElementTree.Element:
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise PrestashopError("Prestashop returned invalid XML.", body=payload) from exc
        for node in root.iter():
            if "}" in node.tag:
                node.tag = node.tag.split("}", 1)[1]
        return root

    def upsert_product(self, product: object) -> None:
        raise NotImplementedError("Prestashop product sync is not implemented yet.")

    def upsert_price(self, price: object) -> None:
        raise NotImplementedError("Prestashop price sync is not implemented yet.")

    def upsert_stock(self, stock: object) -> None:
        raise NotImplementedError("Prestashop stock sync is not implemented yet.")
