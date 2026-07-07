import hashlib
import json
import logging
import time as time_module
from datetime import UTC

import barcodenumber
from django.utils import timezone

from apps.catalog.models import (
    AttributeGroup,
    AttributeValue,
    Category,
    CategoryType,
    Combination,
    Manufacturer,
    Price,
    Product,
    Stock,
    TaxRuleMapping,
)
from apps.catalog.variants import effective_prestashop_variant_axes, is_placeholder_variant_axis
from apps.prestashop.attribute_groups import (
    expected_local_attribute_group_name,
    resolve_remote_attribute_group_match,
)
from apps.prestashop.client import PrestashopClient, PrestashopError
from apps.sync.locking import LOCK_TIMEOUT_MINUTES, LockAcquisitionError, sync_lock

logger = logging.getLogger(__name__)

_LOCK_RETRY_DELAY = 1  # seconds between lock retries


def _is_product_unsyncable(product: Product) -> bool:
    """Return True if the product should not be synced to Prestashop."""
    return product.discontinued or not product.visible_web


def _clear_sync_fields(
    entity,
    extra_fields: list[str] | None = None,
    sync_field: str = "sync_required",
) -> None:
    """Set the sync flag to False and clear error on an entity."""
    fields = [sync_field, "last_sync_error", "last_synced_at", "updated_at"]
    if extra_fields:
        fields.extend(extra_fields)
    setattr(entity, sync_field, False)
    entity.last_sync_error = ""
    entity.last_synced_at = timezone.now().astimezone(UTC)
    entity.save(update_fields=fields)


def format_sync_error(exc: Exception) -> str:
    payload = {"message": str(exc)}
    if isinstance(exc, PrestashopError):
        if exc.status_code is not None:
            payload["status_code"] = exc.status_code
        if exc.body:
            payload["body"] = exc.body
    return json.dumps(payload, sort_keys=True)


def resolve_tax_rules_group(vat_rate) -> int:
    from decimal import Decimal

    from django.conf import settings

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


def resolve_default_category() -> Category:
    category = Category.objects.filter(category_type=CategoryType.DEFAULT).first()
    if category is None:
        raise PrestashopError(
            "No default category configured. "
            "Set a Category with category_type='default' in Django admin."
        )
    return category


def resolve_hidden_category() -> Category | None:
    return Category.objects.filter(category_type=CategoryType.HIDDEN).first()


def resolve_product_categories(
    product: Product,
    client: PrestashopClient | None = None,
) -> tuple[Category, list[int]]:
    """Return (default_category, all_category_ps_ids) for product export.

    Falls back to the DEFAULT category when the product has no explicit
    category_default.  The full list always includes the default category.
    Filters out any categories with unsynced prestashop_id=None.
    """
    default = product.category_default or resolve_default_category()

    if default.prestashop_id is None:
        export_category(default.pk, client=client)
        default.refresh_from_db()

    all_ids = [
        ps_id
        for ps_id in product.categories.values_list("prestashop_id", flat=True)
        if ps_id is not None
    ]
    if default.prestashop_id not in all_ids:
        all_ids.append(default.prestashop_id)
    return default, all_ids


def export_category(
    category_id: int,
    client: PrestashopClient | None = None,
    _ancestors: set[int] | None = None,
) -> dict[str, int]:
    from django.conf import settings

    category = Category.objects.get(pk=category_id)
    client = client or PrestashopClient()

    try:
        parent_ps_id = (
            category.parent.prestashop_id
            if category.parent
            else getattr(settings, "PRESTASHOP_ROOT_CATEGORY_ID", 2)
        )

        if category.parent and category.parent.prestashop_id is None:
            if _ancestors is None:
                _ancestors = set()
            if category.pk in _ancestors:
                raise PrestashopError(
                    f"Cyclic parent detected for category {category.name} "
                    f"(pk={category.pk}). Fix the parent chain in Django admin."
                )
            _ancestors.add(category.pk)
            export_category(category.parent.pk, client=client, _ancestors=_ancestors)
            category.parent.refresh_from_db()
            parent_ps_id = category.parent.prestashop_id

        if category.prestashop_id is not None:
            client.update_category(
                category.prestashop_id,
                category.name,
                active=category.active,
                parent_id=parent_ps_id,
            )
            ps_id = category.prestashop_id
        else:
            existing = client.find_category_id_by_name(category.name, parent_id=parent_ps_id)
            if existing is not None:
                ps_id = existing
                client.update_category(
                    ps_id,
                    category.name,
                    active=category.active,
                    parent_id=parent_ps_id,
                )
            else:
                ps_id = client.create_category(
                    category.name, parent_id=parent_ps_id, active=category.active
                )

        category.prestashop_id = ps_id
        category.sync_required = False
        category.last_sync_error = ""
        category.last_synced_at = timezone.now().astimezone(UTC)
        category.save(
            update_fields=[
                "prestashop_id",
                "sync_required",
                "last_sync_error",
                "last_synced_at",
                "updated_at",
            ]
        )
        return {"category_id": category.pk, "prestashop_id": ps_id}
    except PrestashopError as exc:
        if exc.status_code == 404 and category.prestashop_id is not None:
            logger.warning(
                "Category %s (prestashop_id=%d) not found in PrestaShop, resetting and recreating.",
                category.name,
                category.prestashop_id,
            )
            category.prestashop_id = None
            category.save(update_fields=["prestashop_id", "updated_at"])
            return export_category(category_id, client=client, _ancestors=_ancestors)
        category.sync_required = True
        category.last_sync_error = format_sync_error(exc)
        category.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise
    except Exception as exc:
        category.sync_required = True
        category.last_sync_error = format_sync_error(exc)
        category.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise


def export_manufacturer(
    manufacturer_id: int, client: PrestashopClient | None = None
) -> dict[str, int]:
    manufacturer = Manufacturer.objects.get(pk=manufacturer_id)
    client = client or PrestashopClient()

    try:
        if manufacturer.prestashop_id is not None:
            manufacturer_root = client.get_manufacturer_xml(manufacturer.prestashop_id)
            prestashop_name = client._manufacturer_name_from_root(manufacturer_root)
            if (
                isinstance(prestashop_name, str)
                and prestashop_name
                and prestashop_name != manufacturer.name
            ):
                other = (
                    Manufacturer.objects.filter(name=prestashop_name)
                    .exclude(pk=manufacturer.pk)
                    .first()
                )
                if other is not None:
                    logger.warning(
                        (
                            "Manufacturer %s had stale Prestashop mapping %s -> %s; "
                            "resetting local mapping."
                        ),
                        manufacturer.icg_code,
                        manufacturer.prestashop_id,
                        prestashop_name,
                    )
                    manufacturer.prestashop_id = None
                    manufacturer.save(update_fields=["prestashop_id", "updated_at"])
                    return export_manufacturer(manufacturer_id, client=client)

            client.update_manufacturer(
                manufacturer.prestashop_id,
                manufacturer.name,
                root=manufacturer_root,
            )
            prestashop_id = manufacturer.prestashop_id
        else:
            prestashop_id = client.find_manufacturer_id_by_name(manufacturer.name)
            if prestashop_id is None:
                prestashop_id = client.create_manufacturer(manufacturer.name)
            else:
                existing = (
                    Manufacturer.objects.filter(prestashop_id=prestashop_id)
                    .exclude(pk=manufacturer.pk)
                    .first()
                )
                if existing is not None:
                    if existing.name == manufacturer.name:
                        raise PrestashopError(
                            "Prestashop manufacturer "
                            f"{prestashop_id} is already mapped to local manufacturer "
                            f"{existing.icg_code} with the same name {manufacturer.name!r}."
                        )

                    logger.warning(
                        "Reassigning stale Prestashop manufacturer mapping %s from %s to %s.",
                        prestashop_id,
                        existing.icg_code,
                        manufacturer.icg_code,
                    )
                    existing.prestashop_id = None
                    existing.sync_required = True
                    existing.save(update_fields=["prestashop_id", "sync_required", "updated_at"])
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
    except PrestashopError as exc:
        if exc.status_code == 404 and manufacturer.prestashop_id is not None:
            logger.warning(
                "Manufacturer %s (prestashop_id=%d) not found in PrestaShop, "
                "resetting and recreating.",
                manufacturer.name,
                manufacturer.prestashop_id,
            )
            manufacturer.prestashop_id = None
            manufacturer.save(update_fields=["prestashop_id", "updated_at"])
            return export_manufacturer(manufacturer_id, client=client)
        manufacturer.sync_required = True
        manufacturer.last_sync_error = format_sync_error(exc)
        manufacturer.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise
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
    product = Product.objects.select_related("manufacturer", "category_default").get(pk=product_id)
    client = client or PrestashopClient()

    try:
        if not product.visible_web and product.prestashop_id is None:
            product.sync_required = False
            product.save(update_fields=["sync_required", "updated_at"])
            return {"product_id": product.pk, "prestashop_id": None}

        if product.manufacturer and product.manufacturer.prestashop_id is None:
            raise PrestashopError(
                "Manufacturer "
                f"{product.manufacturer.icg_code} must be exported before product sync."
            )

        default_category, category_ids = resolve_product_categories(product, client=client)

        prestashop_id = product.prestashop_id
        if prestashop_id is None:
            prestashop_id = client.find_product_id_by_reference(product.reference)

        prestashop_id = client.upsert_product(
            product,
            prestashop_id=prestashop_id,
            tax_rules_group_id=tax_rules_group_id,
            category_default_id=default_category.prestashop_id,
            category_ids=category_ids,
        )

        product.prestashop_id = prestashop_id
        product.sync_required = False
        product.last_sync_error = ""
        product.last_synced_at = timezone.now().astimezone(UTC)
        product.save(
            update_fields=[
                "prestashop_id",
                "sync_required",
                "last_sync_error",
                "last_synced_at",
                "updated_at",
            ]
        )

        has_colors = (
            product.combinations.filter(icg_color__isnull=False).exclude(icg_color="").exists()
        )
        if has_colors:
            ensure_attribute_group("color", client=client, product=product)

        return {"product_id": product.pk, "prestashop_id": prestashop_id}
    except PrestashopError as exc:
        if exc.status_code == 404 and product.prestashop_id is not None:
            logger.warning(
                "Product %s (prestashop_id=%d) not found in PrestaShop, resetting and recreating.",
                product.reference,
                product.prestashop_id,
            )
            product.prestashop_id = None
            product.save(update_fields=["prestashop_id", "updated_at"])
            return export_product(product_id, client=client, tax_rules_group_id=tax_rules_group_id)
        product.sync_required = True
        product.last_sync_error = format_sync_error(exc)
        product.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise
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
        product = price.combination.product

        if _is_product_unsyncable(product) or not price.combination.active:
            _clear_sync_fields(price)
            return {
                "price_id": price.pk,
                "product_prestashop_id": product.prestashop_id or 0,
                "combination_prestashop_id": price.combination.prestashop_id or 0,
            }

        tax_rules_group_id = resolve_tax_rules_group(price.vat_rate)

        combination = price.combination
        product = combination.product

        product_result = (
            export_product(product.pk, client=client, tax_rules_group_id=tax_rules_group_id)
            if product.sync_required or product.prestashop_id is None
            else {"product_id": product.pk, "prestashop_id": product.prestashop_id}
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
}


def ensure_attribute_group(
    icg_type: str,
    *,
    client: PrestashopClient | None = None,
    product: Product | None = None,
) -> int:
    client = client or PrestashopClient()

    if icg_type == "color":
        if product is None:
            raise PrestashopError("Color attribute groups require a product reference.")
        is_color_group = True
        existing = AttributeGroup.objects.filter(icg_type="color", product=product).first()
        product_specific = True
    else:
        is_color_group = False
        existing = None
        if product is not None:
            existing = AttributeGroup.objects.filter(icg_type=icg_type, product=product).first()
        product_specific = False

    if existing is not None:
        return existing.prestashop_id

    display_name = expected_local_attribute_group_name(icg_type, product)
    remote_groups = client.list_attribute_groups()
    remote_match = resolve_remote_attribute_group_match(
        remote_groups,
        icg_type=icg_type,
        product=product,
    )

    if icg_type != "color" and remote_match is None:
        existing = AttributeGroup.objects.filter(icg_type=icg_type, product__isnull=True).first()
        if existing is not None:
            return existing.prestashop_id

    if remote_match is not None:
        ps_id = remote_match.prestashop_id
        local_name = display_name if icg_type == "color" else remote_match.name
        local_product = product if remote_match.product_specific else None
    else:
        ps_id = client.find_attribute_group_id_by_name(display_name)
        if ps_id is None:
            ps_id = client.create_attribute_group(display_name, is_color_group=is_color_group)
        local_name = display_name
        local_product = product if product_specific else None

    AttributeGroup.objects.update_or_create(
        icg_type=icg_type,
        product=local_product,
        defaults={"name": local_name, "prestashop_id": ps_id},
    )
    return ps_id


def _attr_val_lock_key(group_ps_id: int, value_name: str) -> str:
    raw = f"attr_val:{group_ps_id}:{value_name}"
    return hashlib.md5(raw.encode()).hexdigest()


def ensure_attribute_value(
    group_ps_id: int,
    value_name: str,
    *,
    client: PrestashopClient | None = None,
    texture_image_path: str | None = None,
) -> int:
    from django.conf import settings

    client = client or PrestashopClient()
    ag = AttributeGroup.objects.get(prestashop_id=group_ps_id)

    existing = AttributeValue.objects.filter(attribute_group=ag, icg_value=value_name).first()
    if existing is not None:
        if (
            texture_image_path
            and not existing.texture_synced
            and getattr(settings, "PRESTASHOP_SYNC_TEXTURE_IMAGES", False)
        ):
            client.upload_attribute_value_image(existing.prestashop_id, texture_image_path)
            existing.texture_synced = True
            existing.save(update_fields=["texture_synced", "updated_at"])
        return existing.prestashop_id

    lock_key = _attr_val_lock_key(group_ps_id, value_name)
    deadline = time_module.monotonic() + LOCK_TIMEOUT_MINUTES * 60

    while True:
        try:
            with sync_lock(lock_key):
                existing = AttributeValue.objects.filter(
                    attribute_group=ag, icg_value=value_name
                ).first()
                if existing is not None:
                    if (
                        texture_image_path
                        and not existing.texture_synced
                        and getattr(settings, "PRESTASHOP_SYNC_TEXTURE_IMAGES", False)
                    ):
                        client.upload_attribute_value_image(
                            existing.prestashop_id, texture_image_path
                        )
                        existing.texture_synced = True
                        existing.save(update_fields=["texture_synced", "updated_at"])
                    return existing.prestashop_id

                ps_id = client.find_attribute_value_id(value_name, group_ps_id)
                if ps_id is None:
                    ps_id = client.create_attribute_value(value_name, group_ps_id)

                sync_images = getattr(settings, "PRESTASHOP_SYNC_TEXTURE_IMAGES", False)
                if texture_image_path and sync_images:
                    client.upload_attribute_value_image(ps_id, texture_image_path)

                AttributeValue.objects.update_or_create(
                    attribute_group=ag,
                    icg_value=value_name,
                    defaults={
                        "name": value_name,
                        "prestashop_id": ps_id,
                        "texture_synced": bool(texture_image_path and sync_images),
                    },
                )
                return ps_id
        except LockAcquisitionError:
            if time_module.monotonic() >= deadline:
                raise LockAcquisitionError(
                    f"Cannot acquire lock for attribute value {value_name} "
                    f"(group PS ID {group_ps_id}) after "
                    f"{LOCK_TIMEOUT_MINUTES} minute(s)."
                ) from None
            time_module.sleep(_LOCK_RETRY_DELAY)


def export_combination(
    combination_id: int, client: PrestashopClient | None = None
) -> dict[str, int]:
    combination = Combination.objects.select_related("product", "product__manufacturer").get(
        pk=combination_id
    )
    client = client or PrestashopClient()

    try:
        product = combination.product

        if _is_product_unsyncable(product):
            if combination.prestashop_id:
                client.deactivate_combination(combination.prestashop_id)
            _clear_sync_fields(combination)
            return {
                "combination_id": combination.pk,
                "prestashop_combination_id": combination.prestashop_id or 0,
            }

        if not combination.active:
            if combination.prestashop_id:
                client.deactivate_combination(combination.prestashop_id)

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
                "prestashop_combination_id": combination.prestashop_id or 0,
            }

        if not combination.product.prestashop_id:
            raise PrestashopError(
                f"Product {combination.product.reference} must be exported before combination sync."
            )

        product_ps_id = combination.product.prestashop_id
        normalized_size, normalized_color = effective_prestashop_variant_axes(
            combination.icg_size,
            combination.icg_color,
        )
        size_placeholder = is_placeholder_variant_axis(combination.icg_size)
        color_placeholder = is_placeholder_variant_axis(combination.icg_color)

        # Keep legacy Prestashop placeholder attributes intact on already-mapped
        # combinations that use non-*** placeholder values. For ***/*** combinations,
        # we must always send the price on every sync since price can change.
        both_placeholders = size_placeholder and color_placeholder
        is_asterisk_asterisk = (
            str(combination.icg_size).strip() == "***"
            and str(combination.icg_color).strip() == "***"
        )
        if combination.prestashop_id and both_placeholders and not is_asterisk_asterisk:
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
                "prestashop_combination_id": combination.prestashop_id,
            }

        if both_placeholders:
            size_group_ps_id = ensure_attribute_group(
                "size", client=client, product=combination.product
            )
            size_value_ps_id = ensure_attribute_value(size_group_ps_id, "***", client=client)
            attribute_value_ps_ids = [size_value_ps_id]
            price_obj = getattr(combination, "price", None)
            combination_price = str(price_obj.amount_ex_vat) if price_obj else "0"
            ean13_clean = combination.ean13 and barcodenumber.check_code("ean13", combination.ean13)
            ean13 = combination.ean13 if ean13_clean else ""
            prestashop_combination_id = client.upsert_combination(
                product_ps_id,
                ean13,
                combination.active,
                attribute_value_ps_ids,
                prestashop_id=combination.prestashop_id,
                price=combination_price,
            )
            if not combination.prestashop_id:
                combination.prestashop_id = prestashop_combination_id
            combination.sync_required = False
            combination.last_sync_error = ""
            combination.last_synced_at = timezone.now().astimezone(UTC)
            combination.save(
                update_fields=[
                    "prestashop_id",
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

        size_ps_ids = []
        color_ps_ids = []

        if normalized_size:
            size_group_ps_id = ensure_attribute_group(
                "size", client=client, product=combination.product
            )
            size_value_ps_id = ensure_attribute_value(
                size_group_ps_id, normalized_size, client=client
            )
            size_ps_ids = [size_value_ps_id]

        if normalized_color:
            color_group_ps_id = ensure_attribute_group(
                "color", client=client, product=combination.product
            )

            texture_image_path = None
            color_value = AttributeValue.objects.filter(
                attribute_group__prestashop_id=color_group_ps_id,
                icg_value=normalized_color,
            ).first()
            if color_value and color_value.texture_image:
                texture_image_path = color_value.texture_image.path

            color_value_ps_id = ensure_attribute_value(
                color_group_ps_id,
                normalized_color,
                client=client,
                texture_image_path=texture_image_path,
            )
            color_ps_ids = [color_value_ps_id]

        attribute_value_ps_ids = size_ps_ids + color_ps_ids

        if not attribute_value_ps_ids:
            raise PrestashopError(f"Combination {combination} has neither size nor color.")

        prestashop_combination_id = combination.prestashop_id

        price_obj = getattr(combination, "price", None)
        combination_price = str(price_obj.amount_ex_vat) if price_obj else "0"

        ean13_clean = combination.ean13 and barcodenumber.check_code("ean13", combination.ean13)
        ean13 = combination.ean13 if ean13_clean else ""

        prestashop_combination_id = client.upsert_combination(
            product_ps_id,
            ean13,
            combination.active,
            attribute_value_ps_ids,
            prestashop_id=prestashop_combination_id,
            price=combination_price,
        )

        combination.prestashop_id = prestashop_combination_id
        combination.sync_required = False
        combination.last_sync_error = ""
        combination.last_synced_at = timezone.now().astimezone(UTC)
        combination.save(
            update_fields=[
                "prestashop_id",
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
    except PrestashopError as exc:
        if exc.status_code == 404 and combination.prestashop_id is not None:
            logger.warning(
                "Combination %s (prestashop_id=%d) not found in PrestaShop, "
                "resetting and recreating.",
                combination,
                combination.prestashop_id,
            )
            combination.prestashop_id = None
            combination.save(update_fields=["prestashop_id", "updated_at"])
            return export_combination(combination_id, client=client)
        combination.sync_required = True
        combination.last_sync_error = format_sync_error(exc)
        combination.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise
    except Exception as exc:
        combination.sync_required = True
        combination.last_sync_error = format_sync_error(exc)
        combination.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise


def export_stock(stock_id: int, client: PrestashopClient | None = None) -> dict[str, int]:
    stock = Stock.objects.select_related("combination", "combination__product").get(pk=stock_id)
    client = client or PrestashopClient()

    try:
        product = stock.combination.product

        if _is_product_unsyncable(product) or not stock.combination.active:
            _clear_sync_fields(stock)
            return {
                "stock_id": stock.pk,
                "prestashop_combination_id": stock.combination.prestashop_id or 0,
                "quantity": stock.quantity,
            }

        if not stock.combination.prestashop_id:
            raise PrestashopError(
                f"Combination {stock.combination} must be exported before stock sync."
            )

        client.upsert_stock(stock.combination.prestashop_id, stock.quantity)

        stock.sync_required = False
        stock.last_sync_error = ""
        stock.last_synced_at = timezone.now().astimezone(UTC)
        stock.save(
            update_fields=[
                "sync_required",
                "last_sync_error",
                "last_synced_at",
                "updated_at",
            ]
        )
        return {
            "stock_id": stock.pk,
            "prestashop_combination_id": stock.combination.prestashop_id,
            "quantity": stock.quantity,
        }
    except Exception as exc:
        stock.sync_required = True
        stock.last_sync_error = format_sync_error(exc)
        stock.save(update_fields=["sync_required", "last_sync_error", "updated_at"])
        raise


def export_discount(
    product_id: int,
    client: PrestashopClient | None = None,
) -> dict[str, int | None]:
    product = Product.objects.select_related("manufacturer").get(pk=product_id)
    client = client or PrestashopClient()

    try:
        existing_ps_id = product.prestashop_specific_price_id

        if _is_product_unsyncable(product):
            if product.prestashop_id is not None:
                existing_specific_price_ids = client.list_all_specific_price_ids_by_product(
                    product.prestashop_id
                )
                for specific_price_id in existing_specific_price_ids:
                    client.delete_specific_price(specific_price_id)
            elif existing_ps_id is not None:
                client.delete_specific_price(existing_ps_id)

            if existing_ps_id is not None:
                product.prestashop_specific_price_id = None
                product.save(update_fields=["prestashop_specific_price_id", "updated_at"])
            _clear_sync_fields(product, sync_field="discount_sync_required")
            return {
                "product_id": product.pk,
                "prestashop_product_id": product.prestashop_id or 0,
                "prestashop_specific_price_id": product.prestashop_specific_price_id,
                "discount_percent": str(product.discount_percent),
            }

        if not product.prestashop_id:
            raise PrestashopError(
                f"Product {product.reference} must be exported before discount sync."
            )

        product_ps_id = product.prestashop_id
        discount = product.discount_percent

        existing_specific_price_ids = client.list_all_specific_price_ids_by_product(product_ps_id)

        for specific_price_id in existing_specific_price_ids:
            client.delete_specific_price(specific_price_id)

        if existing_ps_id is not None:
            product.prestashop_specific_price_id = None
            product.save(update_fields=["prestashop_specific_price_id", "updated_at"])

        if discount > 0:
            ps_id = client.upsert_specific_price(product_ps_id, discount, prestashop_id=None)
            product.prestashop_specific_price_id = ps_id
            product.save(update_fields=["prestashop_specific_price_id", "updated_at"])

        product.discount_sync_required = False
        product.last_sync_error = ""
        product.last_synced_at = timezone.now().astimezone(UTC)
        product.save(
            update_fields=[
                "prestashop_specific_price_id",
                "discount_sync_required",
                "last_sync_error",
                "last_synced_at",
                "updated_at",
            ]
        )
        return {
            "product_id": product.pk,
            "prestashop_product_id": product_ps_id,
            "prestashop_specific_price_id": product.prestashop_specific_price_id,
            "discount_percent": str(discount),
        }
    except PrestashopError as exc:
        if exc.status_code == 404 and product.prestashop_specific_price_id is not None:
            logger.warning(
                "Specific price for product %s (prestashop_id=%d) not found in "
                "PrestaShop, resetting and recreating.",
                product.reference,
                product.prestashop_specific_price_id,
            )
            product.prestashop_specific_price_id = None
            product.save(update_fields=["prestashop_specific_price_id", "updated_at"])
            return export_discount(product_id, client=client)
        product.last_sync_error = format_sync_error(exc)
        product.save(update_fields=["last_sync_error", "updated_at"])
        raise
    except Exception as exc:
        product.last_sync_error = format_sync_error(exc)
        product.save(update_fields=["last_sync_error", "updated_at"])
        raise
