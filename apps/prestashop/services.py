import json
from datetime import UTC

from django.utils import timezone

from apps.catalog.models import Manufacturer
from apps.prestashop.client import PrestashopClient, PrestashopError


def format_sync_error(exc: Exception) -> str:
    payload = {"message": str(exc)}
    if isinstance(exc, PrestashopError):
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
