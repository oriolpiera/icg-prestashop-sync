from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, cast
from xml.etree import ElementTree

import requests
from django.conf import settings
from django.utils.text import slugify
from requests import Response, Session
from requests.auth import HTTPBasicAuth


class PrestashopSettings(Protocol):
    PRESTASHOP_BASE_URL: str
    PRESTASHOP_API_KEY: str
    PRESTASHOP_HOST: str
    PRESTASHOP_DEFAULT_LANGUAGE_ID: int
    PRESTASHOP_DEFAULT_CATEGORY_ID: int


class ProductManufacturer(Protocol):
    prestashop_id: int | None


class ProductPayload(Protocol):
    reference: str
    name: str
    visible_web: bool
    discontinued: bool
    manufacturer: ProductManufacturer | None


@dataclass(slots=True)
class PrestashopCredentials:
    base_url: str
    api_key: str
    host: str
    default_language_id: int
    default_category_id: int


class PrestashopError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class PrestashopClient:
    _FILTER_RESERVED_CHARS = frozenset("[]|,")

    def __init__(self, session: Session | None = None) -> None:
        self.session = session or requests.Session()

    def credentials(self) -> PrestashopCredentials:
        typed_settings = cast(PrestashopSettings, settings)
        return PrestashopCredentials(
            base_url=typed_settings.PRESTASHOP_BASE_URL,
            api_key=typed_settings.PRESTASHOP_API_KEY,
            host=typed_settings.PRESTASHOP_HOST,
            default_language_id=typed_settings.PRESTASHOP_DEFAULT_LANGUAGE_ID,
            default_category_id=typed_settings.PRESTASHOP_DEFAULT_CATEGORY_ID,
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

    def _api_image_url(self, resource_type: str, resource_id: int) -> str:
        """Build URL for image endpoints like /api/images/product_option_values/{id}."""
        credentials = self.credentials()
        base_url = credentials.base_url.rstrip("/")
        if not base_url or not credentials.api_key:
            raise PrestashopError("Prestashop credentials are not configured.")

        return f"{base_url}/api/images/{resource_type}/{resource_id}"

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
                headers={
                    "Content-Type": "application/xml",
                    "Host": self.credentials().host,
                },
                timeout=30,
                allow_redirects=False,
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
        self._validate_exact_filter_value(name, field_name="manufacturer name")
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

    def find_category_id_by_name(self, name: str, parent_id: int | None = None) -> int | None:
        self._validate_exact_filter_value(name, field_name="category name")
        params: dict[str, str] = {"filter[name]": f"[{name}]", "limit": "1"}
        if parent_id is not None:
            params["filter[id_parent]"] = str(parent_id)
        response = self._request("GET", "categories", params=params)
        root = self._parse_xml(response.text)
        category = root.find("./categories/category")
        if category is None:
            return None

        category_id = category.attrib.get("id")
        if not category_id:
            category_id = category.findtext("id")
        if not category_id:
            raise PrestashopError("Prestashop category search response did not include an id.")
        return int(category_id)

    def get_category_xml(self, category_id: int) -> ElementTree.Element:
        response = self._request("GET", "categories", resource_id=category_id)
        return self._parse_xml(response.text)

    def create_category(
        self,
        name: str,
        parent_id: int,
        active: bool = True,
    ) -> int:
        root = ElementTree.Element("prestashop", {"xmlns:xlink": "http://www.w3.org/1999/xlink"})
        category = ElementTree.SubElement(root, "category")
        self._set_multilang_text(category, "name", name)
        self._set_multilang_text(category, "link_rewrite", slugify(name) or "category")
        self._set_text(category, "id_parent", str(parent_id))
        self._set_text(category, "active", "1" if active else "0")
        response = self._request(
            "POST",
            "categories",
            data=ElementTree.tostring(root, encoding="unicode"),
        )
        created_root = self._parse_xml(response.text)
        category_id = created_root.findtext("./category/id")
        if not category_id:
            raise PrestashopError("Prestashop create category response did not include an id.")
        return int(category_id)

    def update_category(
        self,
        category_id: int,
        name: str,
        active: bool = True,
        parent_id: int | None = None,
    ) -> None:
        root = self.get_category_xml(category_id)
        cat_node = root.find("./category")
        if cat_node is None:
            raise PrestashopError("Prestashop category payload did not include a category node.")
        if parent_id is not None:
            self._set_text(cat_node, "id_parent", str(parent_id))
        self._set_multilang_text(cat_node, "name", name)
        self._set_multilang_text(cat_node, "link_rewrite", slugify(name) or "category")
        self._set_text(cat_node, "active", "1" if active else "0")
        payload = ElementTree.tostring(root, encoding="unicode")
        self._request("PUT", "categories", resource_id=category_id, data=payload)

    def find_product_id_by_reference(self, reference: str) -> int | None:
        self._validate_exact_filter_value(reference, field_name="product reference")
        response = self._request(
            "GET",
            "products",
            params={"filter[reference]": f"[{reference}]", "limit": "1"},
        )
        root = self._parse_xml(response.text)
        product = root.find("./products/product")
        if product is None:
            return None

        product_id = product.attrib.get("id")
        if not product_id:
            product_id = product.findtext("id")
        if not product_id:
            raise PrestashopError("Prestashop product search response did not include an id.")
        return int(product_id)

    def get_product_xml(self, product_id: int) -> ElementTree.Element:
        response = self._request("GET", "products", resource_id=product_id)
        return self._parse_xml(response.text)

    def get_blank_product_xml(self) -> ElementTree.Element:
        response = self._request("GET", "products", params={"schema": "blank"})
        return self._parse_xml(response.text)

    def upsert_product(
        self,
        product: ProductPayload,
        *,
        prestashop_id: int | None = None,
        tax_rules_group_id: int | None = None,
        category_default_id: int | None = None,
        category_ids: list[int] | None = None,
    ) -> int:
        if prestashop_id is None:
            root = self.get_blank_product_xml()
            self._populate_product_xml(
                root,
                product,
                is_create=True,
                tax_rules_group_id=tax_rules_group_id,
                category_default_id=category_default_id,
                category_ids=category_ids,
            )
            response = self._request(
                "POST",
                "products",
                data=ElementTree.tostring(root, encoding="unicode"),
            )
            created_root = self._parse_xml(response.text)
            product_id = created_root.findtext("./product/id")
            if not product_id:
                raise PrestashopError("Prestashop create product response did not include an id.")
            return int(product_id)

        root = self.get_product_xml(prestashop_id)
        self._populate_product_xml(
            root,
            product,
            is_create=False,
            tax_rules_group_id=tax_rules_group_id,
            category_default_id=category_default_id,
            category_ids=category_ids,
        )
        self._request(
            "PUT",
            "products",
            resource_id=prestashop_id,
            data=ElementTree.tostring(root, encoding="unicode"),
        )
        return prestashop_id

    def _manufacturer_xml(self, name: str) -> str:
        root = ElementTree.Element("prestashop", {"xmlns:xlink": "http://www.w3.org/1999/xlink"})
        manufacturer = ElementTree.SubElement(root, "manufacturer")
        name_node = ElementTree.SubElement(manufacturer, "name")
        name_node.text = name
        active = ElementTree.SubElement(manufacturer, "active")
        active.text = "1"
        return ElementTree.tostring(root, encoding="unicode")

    def _populate_product_xml(
        self,
        root: ElementTree.Element,
        product: ProductPayload,
        *,
        is_create: bool,
        tax_rules_group_id: int | None = None,
        category_default_id: int | None = None,
        category_ids: list[int] | None = None,
    ) -> None:
        product_node = root.find("./product")
        if product_node is None:
            raise PrestashopError("Prestashop product payload did not include a product node.")

        active, visibility = self._product_status(product)
        manufacturer_id = "0"
        if product.manufacturer and product.manufacturer.prestashop_id is not None:
            manufacturer_id = str(product.manufacturer.prestashop_id)

        self._set_text(product_node, "id_manufacturer", manufacturer_id)
        self._remove_node(product_node, "manufacturer_name")
        self._remove_node(product_node, "quantity")
        if tax_rules_group_id is not None:
            self._set_text(product_node, "id_tax_rules_group", str(tax_rules_group_id))
        self._set_text(product_node, "reference", product.reference)
        self._set_text(product_node, "price", product_node.findtext("price") or "0")
        self._set_text(product_node, "state", "1")
        self._set_text(product_node, "active", active)
        self._set_text(product_node, "available_for_order", "0" if product.discontinued else "1")
        self._set_text(product_node, "show_price", "1")
        self._set_text(product_node, "visibility", visibility)
        if is_create:
            self._remove_node(product_node, "position_in_category")
            self._remove_node(product_node, "position")
            self._set_text(product_node, "active", "0")
        else:
            self._set_text(
                product_node,
                "position_in_category",
                product_node.findtext("position_in_category") or "1",
            )
        self._set_text(
            product_node,
            "minimal_quantity",
            product_node.findtext("minimal_quantity") or "1",
        )
        self._set_multilang_text(product_node, "name", product.name, fill_all_languages=is_create)
        self._set_multilang_text(
            product_node, "link_rewrite", self._slug(product), fill_all_languages=is_create
        )

        effective_default = (
            str(category_default_id)
            if category_default_id is not None
            else product_node.findtext("id_category_default")
            or str(self.credentials().default_category_id)
        )
        self._set_text(product_node, "id_category_default", effective_default)

        effective_categories = category_ids or [int(effective_default)]
        self._set_category_association(product_node, effective_categories)

    def _set_category_association(
        self, product_node: ElementTree.Element, category_ids: list[int]
    ) -> None:
        associations = product_node.find("./associations")
        if associations is None:
            associations = ElementTree.SubElement(product_node, "associations")
        categories = associations.find("./categories")
        if categories is None:
            categories = ElementTree.SubElement(associations, "categories")
        categories.clear()
        for cat_id in category_ids:
            category = ElementTree.SubElement(categories, "category")
            category_id = ElementTree.SubElement(category, "id")
            category_id.text = str(cat_id)

    def _set_text(self, parent: ElementTree.Element, tag: str, value: str) -> None:
        node = parent.find(f"./{tag}")
        if node is None:
            node = ElementTree.SubElement(parent, tag)
        node.text = value

    def _remove_node(self, parent: ElementTree.Element, tag: str) -> None:
        node = parent.find(f"./{tag}")
        if node is not None:
            parent.remove(node)

    def _set_multilang_text(
        self,
        parent: ElementTree.Element,
        tag: str,
        value: str,
        *,
        fill_all_languages: bool = False,
    ) -> None:
        node = parent.find(f"./{tag}")
        if node is None:
            node = ElementTree.SubElement(parent, tag)

        if fill_all_languages:
            languages = node.findall("./language")
            if languages:
                for language in languages:
                    language.text = value
                return

        default_language_id = str(self.credentials().default_language_id)
        language = None
        for candidate in node.findall("./language"):
            if candidate.attrib.get("id") == default_language_id:
                language = candidate
                break

        if language is None:
            language = ElementTree.SubElement(node, "language", id=default_language_id)

        language.text = value

    def _validate_exact_filter_value(self, value: str, *, field_name: str) -> None:
        invalid_chars = sorted({char for char in value if char in self._FILTER_RESERVED_CHARS})
        if invalid_chars:
            formatted_chars = " ".join(invalid_chars)
            raise PrestashopError(
                f"Unsupported {field_name} characters for exact-match filter: {formatted_chars}"
            )

    def _product_status(self, product: ProductPayload) -> tuple[str, str]:
        if product.discontinued:
            return "0", "none"
        if product.visible_web:
            return "1", "both"
        return "1", "none"

    def _slug(self, product: ProductPayload) -> str:
        return slugify(product.name) or slugify(product.reference) or "product"

    def _parse_xml(self, payload: str) -> ElementTree.Element:
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise PrestashopError("Prestashop returned invalid XML.", body=payload) from exc
        for node in root.iter():
            if "}" in node.tag:
                node.tag = node.tag.split("}", 1)[1]
        return root

    def find_attribute_group_id_by_name(self, name: str) -> int | None:
        self._validate_exact_filter_value(name, field_name="attribute group name")
        response = self._request(
            "GET",
            "product_options",
            params={"filter[name]": f"[{name}]", "limit": "1"},
        )
        root = self._parse_xml(response.text)
        group = root.find("./product_options/product_option")
        if group is None:
            return None

        group_id = group.attrib.get("id")
        if not group_id:
            group_id = group.findtext("id")
        if not group_id:
            raise PrestashopError(
                "Prestashop attribute group search response did not include an id."
            )
        return int(group_id)

    def get_attribute_group_xml(self, group_id: int) -> ElementTree.Element:
        response = self._request("GET", "product_options", resource_id=group_id)
        return self._parse_xml(response.text)

    def get_blank_attribute_group_xml(self) -> ElementTree.Element:
        response = self._request("GET", "product_options", params={"schema": "blank"})
        return self._parse_xml(response.text)

    def create_attribute_group(self, name: str, *, is_color_group: bool = False) -> int:
        root = self.get_blank_attribute_group_xml()
        group = root.find("./product_option")
        if group is None:
            raise PrestashopError(
                "Prestashop product option payload did not include a product_option node."
            )
        self._set_text(group, "is_color_group", "1" if is_color_group else "0")
        self._set_text(group, "group_type", "select" if not is_color_group else "color")
        self._set_text(group, "position", group.findtext("position") or "1")
        self._set_multilang_text(group, "name", name, fill_all_languages=True)
        self._set_multilang_text(group, "public_name", name, fill_all_languages=True)
        response = self._request(
            "POST",
            "product_options",
            data=ElementTree.tostring(root, encoding="unicode"),
        )
        created_root = self._parse_xml(response.text)
        group_id = created_root.findtext("./product_option/id")
        if not group_id:
            raise PrestashopError(
                "Prestashop create attribute group response did not include an id."
            )
        return int(group_id)

    def find_attribute_value_id(self, name: str, group_ps_id: int) -> int | None:
        self._validate_exact_filter_value(name, field_name="attribute value name")
        response = self._request(
            "GET",
            "product_option_values",
            params={
                "filter[id_attribute_group]": str(group_ps_id),
                "filter[name]": f"[{name}]",
                "limit": "1",
            },
        )
        root = self._parse_xml(response.text)
        value = root.find("./product_option_values/product_option_value")
        if value is None:
            return None

        value_id = value.attrib.get("id")
        if not value_id:
            value_id = value.findtext("id")
        if not value_id:
            raise PrestashopError(
                "Prestashop attribute value search response did not include an id."
            )
        return int(value_id)

    def get_blank_attribute_value_xml(self) -> ElementTree.Element:
        response = self._request("GET", "product_option_values", params={"schema": "blank"})
        return self._parse_xml(response.text)

    def create_attribute_value(self, name: str, group_ps_id: int) -> int:
        root = self.get_blank_attribute_value_xml()
        value = root.find("./product_option_value")
        if value is None:
            raise PrestashopError(
                "Prestashop product option value payload did not include a product_option_value node."  # noqa: E501
            )
        self._set_text(value, "id_attribute_group", str(group_ps_id))
        self._set_text(value, "color", "")
        self._set_text(value, "position", value.findtext("position") or "1")
        self._set_multilang_text(value, "name", name, fill_all_languages=True)
        response = self._request(
            "POST",
            "product_option_values",
            data=ElementTree.tostring(root, encoding="unicode"),
        )
        created_root = self._parse_xml(response.text)
        value_id = created_root.findtext("./product_option_value/id")
        if not value_id:
            raise PrestashopError(
                "Prestashop create attribute value response did not include an id."
            )
        return int(value_id)

    def upload_attribute_value_image(self, value_ps_id: int, image_path: str) -> None:
        """Upload a texture/color swatch image to an existing attribute value."""
        try:
            with open(image_path, "rb") as image_file:
                response = self.session.request(
                    "POST",
                    self._api_image_url("product_option_values", value_ps_id),
                    files={"image": image_file},
                    auth=self._auth(),
                    headers={"Host": self.credentials().host},
                    timeout=30,
                    allow_redirects=False,
                )
        except (requests.RequestException, OSError) as exc:
            raise PrestashopError(f"Failed to upload attribute value image: {exc}") from exc

        if response.status_code >= 400:
            raise PrestashopError(
                f"Prestashop returned HTTP {response.status_code}"
                " for attribute value image upload.",
                status_code=response.status_code,
                body=response.text,
            )

    def delete_attribute_value_image(self, value_ps_id: int) -> None:
        """Delete the image associated with an attribute value."""
        try:
            response = self.session.request(
                "DELETE",
                self._api_image_url("product_option_values", value_ps_id),
                auth=self._auth(),
                headers={"Host": self.credentials().host},
                timeout=30,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PrestashopError(f"Failed to delete attribute value image: {exc}") from exc

        if response.status_code >= 400:
            raise PrestashopError(
                f"Prestashop returned HTTP {response.status_code}"
                " for attribute value image delete.",
                status_code=response.status_code,
                body=response.text,
            )

    # Utility for admin/discovery — not called by service layer (which uses
    # TaxRuleMapping ORM lookups instead). Useful for validating tax rule names
    # or seeding TaxRuleMapping entries.
    def find_tax_rules_group_id_by_name(self, name: str) -> int | None:
        self._validate_exact_filter_value(name, field_name="tax rules group name")
        response = self._request(
            "GET",
            "tax_rules_groups",
            params={"filter[name]": f"[{name}]", "limit": "1"},
        )
        root = self._parse_xml(response.text)
        group = root.find("./tax_rules_groups/tax_rules_group")
        if group is None:
            return None

        group_id = group.attrib.get("id")
        if not group_id:
            group_id = group.findtext("id")
        if not group_id:
            raise PrestashopError(
                "Prestashop tax rules group search response did not include an id."
            )
        return int(group_id)

    def get_blank_combination_xml(self) -> ElementTree.Element:
        response = self._request("GET", "combinations", params={"schema": "blank"})
        return self._parse_xml(response.text)

    def get_combination_xml(self, combination_id: int) -> ElementTree.Element:
        response = self._request("GET", "combinations", resource_id=combination_id)
        return self._parse_xml(response.text)

    def upsert_combination(
        self,
        product_ps_id: int,
        ean13: str,
        active: bool,
        attribute_value_ps_ids: list[int],
        *,
        prestashop_id: int | None = None,
        price: str = "0",
    ) -> int:
        if prestashop_id is None:
            root = self.get_blank_combination_xml()
            self._populate_combination_xml(
                root,
                product_ps_id=product_ps_id,
                ean13=ean13,
                active=active,
                attribute_value_ps_ids=attribute_value_ps_ids,
                price=price,
            )
            response = self._request(
                "POST",
                "combinations",
                data=ElementTree.tostring(root, encoding="unicode"),
            )
            created_root = self._parse_xml(response.text)
            comb_id = created_root.findtext("./combination/id")
            if not comb_id:
                raise PrestashopError(
                    "Prestashop create combination response did not include an id."
                )
            return int(comb_id)

        root = self.get_combination_xml(prestashop_id)
        self._populate_combination_xml(
            root,
            product_ps_id=product_ps_id,
            ean13=ean13,
            active=active,
            attribute_value_ps_ids=attribute_value_ps_ids,
            price=price,
        )
        self._request(
            "PUT",
            "combinations",
            resource_id=prestashop_id,
            data=ElementTree.tostring(root, encoding="unicode"),
        )
        return prestashop_id

    def deactivate_combination(self, prestashop_id: int) -> None:
        root = self.get_combination_xml(prestashop_id)
        comb_node = root.find("./combination")
        if comb_node is None:
            raise PrestashopError(
                "Prestashop combination payload did not include a combination node."
            )
        self._set_text(comb_node, "active", "0")
        self._request(
            "PUT",
            "combinations",
            resource_id=prestashop_id,
            data=ElementTree.tostring(root, encoding="unicode"),
        )

    def _populate_combination_xml(
        self,
        root: ElementTree.Element,
        *,
        product_ps_id: int,
        ean13: str,
        active: bool,
        attribute_value_ps_ids: list[int],
        price: str,
    ) -> None:
        comb_node = root.find("./combination")
        if comb_node is None:
            raise PrestashopError(
                "Prestashop combination payload did not include a combination node."
            )

        self._set_text(comb_node, "id_product", str(product_ps_id))
        self._set_text(comb_node, "ean13", ean13)
        self._set_text(comb_node, "active", "1" if active else "0")
        self._set_text(comb_node, "price", price)
        self._set_text(comb_node, "minimal_quantity", "1")

        associations = comb_node.find("./associations")
        if associations is None:
            associations = ElementTree.SubElement(comb_node, "associations")
        pov_node = associations.find("./product_option_values")
        if pov_node is None:
            pov_node = ElementTree.SubElement(associations, "product_option_values")
        pov_node.clear()
        for vs_id in attribute_value_ps_ids:
            pov_item = ElementTree.SubElement(pov_node, "product_option_value")
            pov_id = ElementTree.SubElement(pov_item, "id")
            pov_id.text = str(vs_id)

    def find_specific_price_by_product(self, product_ps_id: int) -> int | None:
        response = self._request(
            "GET",
            "specific_prices",
            params={
                "filter[id_product]": str(product_ps_id),
                "filter[id_product_attribute]": "0",
                "limit": "1",
            },
        )
        root = self._parse_xml(response.text)
        sp = root.find("./specific_prices/specific_price")
        if sp is None:
            return None

        sp_id = sp.attrib.get("id")
        if not sp_id:
            sp_id = sp.findtext("id")
        if not sp_id:
            raise PrestashopError(
                "Prestashop specific_price search response did not include an id."
            )
        return int(sp_id)

    def get_specific_price_xml(self, specific_price_id: int) -> ElementTree.Element:
        response = self._request("GET", "specific_prices", resource_id=specific_price_id)
        return self._parse_xml(response.text)

    def get_blank_specific_price_xml(self) -> ElementTree.Element:
        response = self._request("GET", "specific_prices", params={"schema": "blank"})
        return self._parse_xml(response.text)

    def upsert_specific_price(
        self,
        product_ps_id: int,
        reduction_percent: Decimal,
        *,
        prestashop_id: int | None = None,
    ) -> int:
        if prestashop_id is None:
            root = self.get_blank_specific_price_xml()
            self._populate_specific_price_xml(
                root,
                product_ps_id=product_ps_id,
                reduction_percent=reduction_percent,
            )
            response = self._request(
                "POST",
                "specific_prices",
                data=ElementTree.tostring(root, encoding="unicode"),
            )
            created_root = self._parse_xml(response.text)
            sp_id = created_root.findtext("./specific_price/id")
            if not sp_id:
                raise PrestashopError(
                    "Prestashop create specific_price response did not include an id."
                )
            return int(sp_id)

        root = self.get_specific_price_xml(prestashop_id)
        self._populate_specific_price_xml(
            root,
            product_ps_id=product_ps_id,
            reduction_percent=reduction_percent,
        )
        self._request(
            "PUT",
            "specific_prices",
            resource_id=prestashop_id,
            data=ElementTree.tostring(root, encoding="unicode"),
        )
        return prestashop_id

    def delete_specific_price(self, specific_price_id: int) -> None:
        self._request("DELETE", "specific_prices", resource_id=specific_price_id)

    def _populate_specific_price_xml(
        self,
        root: ElementTree.Element,
        *,
        product_ps_id: int,
        reduction_percent: Decimal,
    ) -> None:
        sp_node = root.find("./specific_price")
        if sp_node is None:
            raise PrestashopError(
                "Prestashop specific_price payload did not include a specific_price node."
            )

        self._set_text(sp_node, "id_product", str(product_ps_id))
        self._set_text(sp_node, "id_product_attribute", "0")
        self._set_text(sp_node, "id_shop", "0")
        self._set_text(sp_node, "id_cart", "0")
        self._set_text(sp_node, "id_currency", "0")
        self._set_text(sp_node, "id_country", "0")
        self._set_text(sp_node, "id_group", "0")
        self._set_text(sp_node, "id_customer", "0")
        self._set_text(sp_node, "price", "0")
        self._set_text(sp_node, "reduction_tax", "0")
        self._set_text(sp_node, "from", "0000-00-00 00:00:00")
        self._set_text(sp_node, "to", "0000-00-00 00:00:00")
        self._set_text(sp_node, "reduction", str(reduction_percent / 100))
        self._set_text(sp_node, "reduction_type", "percentage")
        self._set_text(sp_node, "from_quantity", "1")

    def get_stock_available_xml(self, stock_available_id: int) -> ElementTree.Element:
        response = self._request("GET", "stock_availables", resource_id=stock_available_id)
        return self._parse_xml(response.text)

    def find_stock_available_id_by_combination_id(self, combination_ps_id: int) -> int | None:
        response = self._request(
            "GET",
            "stock_availables",
            params={
                "filter[id_product_attribute]": str(combination_ps_id),
                "limit": "1",
            },
        )
        root = self._parse_xml(response.text)
        stock_available = root.find("./stock_availables/stock_available")
        if stock_available is None:
            return None

        stock_available_id = stock_available.attrib.get("id")
        if not stock_available_id:
            stock_available_id = stock_available.findtext("id")
        if not stock_available_id:
            raise PrestashopError(
                "Prestashop stock_available search response did not include an id."
            )
        return int(stock_available_id)

    def upsert_stock(self, combination_ps_id: int, quantity: int) -> None:
        root = self.get_combination_xml(combination_ps_id)
        comb_node = root.find("./combination")
        if comb_node is None:
            raise PrestashopError(
                "Prestashop combination payload did not include a combination node."
            )

        stock_available_node = comb_node.find("./associations/stock_availables/stock_available/id")
        if stock_available_node is None or not stock_available_node.text:
            stock_available_id = self.find_stock_available_id_by_combination_id(combination_ps_id)
            if stock_available_id is None:
                raise PrestashopError(
                    f"Prestashop combination {combination_ps_id} has no stock_available association."  # noqa: E501
                )
        else:
            stock_available_id = int(stock_available_node.text)

        if stock_available_id is None:
            raise PrestashopError(
                f"Prestashop combination {combination_ps_id} has no stock_available association."
            )

        sa_root = self.get_stock_available_xml(stock_available_id)
        sa_node = sa_root.find("./stock_available")
        if sa_node is None:
            raise PrestashopError(
                "Prestashop stock_available payload did not include a stock_available node."
            )

        self._set_text(sa_node, "quantity", str(quantity))
        self._request(
            "PUT",
            "stock_availables",
            resource_id=stock_available_id,
            data=ElementTree.tostring(sa_root, encoding="unicode"),
        )
