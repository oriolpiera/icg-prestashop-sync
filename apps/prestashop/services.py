import json
from datetime import UTC

from django.utils import timezone

from apps.catalog.models import Manufacturer, PrestashopMapping, Product
from apps.prestashop.client import PrestashopClient, PrestashopError


def format_sync_error(exc: Exception) -> str:
    payload = {"message": str(exc)}
    if isinstance(exc, PrestashopError):
        if exc.status_code is not None:
            payload["status_code"] = exc.status_code
        if exc.body:
            payload["body"] = exc.body
    return json.dumps(payload, sort_keys=True)


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


def export_product(product_id: int, client: PrestashopClient | None = None) -> dict[str, int]:
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

        prestashop_id = client.upsert_product(product, prestashop_id=prestashop_id)

        if mapping is None:
            mapping = PrestashopMapping(product=product)
        mapping.prestashop_product_id = prestashop_id
        mapping.save()

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
