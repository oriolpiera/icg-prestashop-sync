import json
from datetime import UTC

from django.utils import timezone

from apps.catalog.models import (
    AttributeGroup,
    AttributeValue,
    Combination,
    Manufacturer,
    PrestashopMapping,
    Price,
    Product,
    TaxRuleMapping,
)
from apps.prestashop.client import PrestashopClient, PrestashopError


def format_sync_error(exc: Exception) -> str:
    payload = {"message": str(exc)}
    if isinstance(exc, PrestashopError):
        if exc.status_code is not None:
            payload["status_code"] = exc.status_code
        if exc.body:
            payload["body"] = exc.body
    return json.dumps(payload, sort_keys=True)


def resolve_tax_rules_group(vat_rate, client: PrestashopClient | None = None) -> int:
    from decimal import Decimal

    from django.conf import settings

    client = client or PrestashopClient()
    rate = Decimal(str(vat_rate))

    mapping = TaxRuleMapping.objects.filter(vat_rate=rate).first()
    if mapping is not None:
        return mapping.prestashop_tax_rules_group_id

    default_id = getattr(settings, "PRESTASHOP_DEFAULT_TAX_RULES_GROUP_ID", None)
    if default_id is not None:
        return default_id

    raise PrestashopError(
        f"Unsupported VAT rate {rate}%: no tax rule mapping configured. "
        "Add a TaxRuleMapping entry in Django admin."
    )


def export_manufacturer(
    manufacturer_id: int, client: PrestashopClient | None = None
) -> dict[str, int]:
    manufacturer = Manufacturer.objects.get(pk=manufacturer_id)
    client = client or PrestashopClient()

    try:
        if manufacturer.prestashop_id is not None:
            client.update_manufacturer(manufacturer.prestashop_id, manufacturer.name)
            prestashop_id = manufacturer.prestashop_id
        else:
            prestashop_id = client.find_manufacturer_id_by_name(manufacturer.name)
            if prestashop_id is None:
                prestashop_id = client.create_manufacturer(manufacturer.name)
            manufacturer.prestashop_id = prestashop_id

        manufacturer.sync_required = False
        manufacturer.last_sync_error = ""
        manufacturer.last_synced_at = timezone.now().astimezone(UTC)
        manufacturer.save(
            update_fields=[
                "prestashop_id",
                "sync_required",
                "last_sync_error",
                "last_synced_at",
                "updated_at",
            ]
        )
        return {"manufacturer_id": manufacturer.pk, "prestashop_id": prestashop_id}
    except Exception as exc:
        manufacturer.sync_required = True
        manufacturer.last_sync_error = format_sync_error(exc)
        manufacturer.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise


def export_product(
    product_id: int,
    client: PrestashopClient | None = None,
    tax_rules_group_id: int | None = None,
) -> dict[str, int]:
    product = Product.objects.select_related("manufacturer").get(pk=product_id)
    mapping = PrestashopMapping.objects.filter(product=product).first()
    client = client or PrestashopClient()

    try:
        if product.manufacturer and product.manufacturer.prestashop_id is None:
            raise PrestashopError(
                "Manufacturer "
                f"{product.manufacturer.icg_code} must be exported before product sync."
            )

        prestashop_id = mapping.prestashop_product_id if mapping else None
        if prestashop_id is None:
            prestashop_id = client.find_product_id_by_reference(product.reference)

        prestashop_id = client.upsert_product(
            product, prestashop_id=prestashop_id, tax_rules_group_id=tax_rules_group_id
        )

        PrestashopMapping.objects.update_or_create(
            product=product,
            defaults={"prestashop_product_id": prestashop_id},
        )

        product.sync_required = False
        product.last_sync_error = ""
        product.last_synced_at = timezone.now().astimezone(UTC)
        product.save(
            update_fields=["sync_required", "last_sync_error", "last_synced_at", "updated_at"]
        )
        return {"product_id": product.pk, "prestashop_id": prestashop_id}
    except Exception as exc:
        product.sync_required = True
        product.last_sync_error = format_sync_error(exc)
        product.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise


def export_price(price_id: int, client: PrestashopClient | None = None) -> dict[str, int]:
    price = Price.objects.select_related(
        "combination", "combination__product", "combination__product__manufacturer"
    ).get(pk=price_id)
    client = client or PrestashopClient()

    try:
        tax_rules_group_id = resolve_tax_rules_group(price.vat_rate, client=client)

        combination = price.combination
        product = combination.product

        product_result = export_product(
            product.pk, client=client, tax_rules_group_id=tax_rules_group_id
        )
        comb_result = export_combination(combination.pk, client=client)

        price.sync_required = False
        price.last_sync_error = ""
        price.last_synced_at = timezone.now().astimezone(UTC)
        price.save(
            update_fields=["sync_required", "last_sync_error", "last_synced_at", "updated_at"]
        )
        return {
            "price_id": price.pk,
            "product_prestashop_id": product_result["prestashop_id"],
            "combination_prestashop_id": comb_result["prestashop_combination_id"],
        }
    except Exception as exc:
        price.sync_required = True
        price.last_sync_error = format_sync_error(exc)
        price.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise


ATTRIBUTE_GROUP_NAMES = {
    "size": "Size",
    "color": "Color",
}


def ensure_attribute_group(icg_type: str, client: PrestashopClient | None = None) -> int:
    client = client or PrestashopClient()
    existing = AttributeGroup.objects.filter(icg_type=icg_type).first()
    if existing is not None:
        return existing.prestashop_id

    display_name = ATTRIBUTE_GROUP_NAMES.get(icg_type, icg_type.title())
    ps_id = client.find_attribute_group_id_by_name(display_name)
    if ps_id is None:
        ps_id = client.create_attribute_group(display_name)

    AttributeGroup.objects.update_or_create(
        icg_type=icg_type,
        defaults={"name": display_name, "prestashop_id": ps_id},
    )
    return ps_id


def ensure_attribute_value(
    group_ps_id: int,
    value_name: str,
    client: PrestashopClient | None = None,
) -> int:
    client = client or PrestashopClient()
    ag = AttributeGroup.objects.get(prestashop_id=group_ps_id)
    existing = AttributeValue.objects.filter(attribute_group=ag, icg_value=value_name).first()
    if existing is not None:
        return existing.prestashop_id

    ps_id = client.find_attribute_value_id(value_name, group_ps_id)
    if ps_id is None:
        ps_id = client.create_attribute_value(value_name, group_ps_id)

    AttributeValue.objects.update_or_create(
        attribute_group=ag,
        icg_value=value_name,
        defaults={"name": value_name, "prestashop_id": ps_id},
    )
    return ps_id


def export_combination(
    combination_id: int, client: PrestashopClient | None = None
) -> dict[str, int]:
    combination = Combination.objects.select_related("product", "product__manufacturer").get(
        pk=combination_id
    )
    client = client or PrestashopClient()

    try:
        if not combination.active:
            comb_mapping = PrestashopMapping.objects.filter(combination=combination).first()
            if comb_mapping and comb_mapping.prestashop_combination_id:
                client.deactivate_combination(comb_mapping.prestashop_combination_id)

            combination.sync_required = False
            combination.last_sync_error = ""
            combination.last_synced_at = timezone.now().astimezone(UTC)
            combination.save(
                update_fields=[
                    "sync_required",
                    "last_sync_error",
                    "last_synced_at",
                    "updated_at",
                ]
            )
            return {
                "combination_id": combination.pk,
                "prestashop_combination_id": (
                    comb_mapping.prestashop_combination_id if comb_mapping else 0
                ),
            }

        product_mapping = PrestashopMapping.objects.filter(product=combination.product).first()
        if not product_mapping or not product_mapping.prestashop_product_id:
            raise PrestashopError(
                f"Product {combination.product.reference} must be exported before combination sync."
            )

        product_ps_id = product_mapping.prestashop_product_id

        size_ps_ids = []
        color_ps_ids = []

        if combination.icg_size:
            size_group_ps_id = ensure_attribute_group("size", client=client)
            size_value_ps_id = ensure_attribute_value(
                size_group_ps_id, combination.icg_size, client=client
            )
            size_ps_ids = [size_value_ps_id]

        if combination.icg_color:
            color_group_ps_id = ensure_attribute_group("color", client=client)
            color_value_ps_id = ensure_attribute_value(
                color_group_ps_id, combination.icg_color, client=client
            )
            color_ps_ids = [color_value_ps_id]

        attribute_value_ps_ids = size_ps_ids + color_ps_ids

        if not attribute_value_ps_ids:
            raise PrestashopError(f"Combination {combination} has neither size nor color.")

        comb_mapping = PrestashopMapping.objects.filter(combination=combination).first()
        prestashop_combination_id = comb_mapping.prestashop_combination_id if comb_mapping else None

        price_obj = getattr(combination, "price", None)
        combination_price = str(price_obj.amount_ex_vat) if price_obj else "0"

        prestashop_combination_id = client.upsert_combination(
            product_ps_id,
            combination.ean13,
            combination.active,
            attribute_value_ps_ids,
            prestashop_id=prestashop_combination_id,
            price=combination_price,
        )

        PrestashopMapping.objects.update_or_create(
            combination=combination,
            defaults={"prestashop_combination_id": prestashop_combination_id},
        )

        combination.sync_required = False
        combination.last_sync_error = ""
        combination.last_synced_at = timezone.now().astimezone(UTC)
        combination.save(
            update_fields=[
                "sync_required",
                "last_sync_error",
                "last_synced_at",
                "updated_at",
            ]
        )
        return {
            "combination_id": combination.pk,
            "prestashop_combination_id": prestashop_combination_id,
        }
    except Exception as exc:
        combination.sync_required = True
        combination.last_sync_error = format_sync_error(exc)
        combination.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise
