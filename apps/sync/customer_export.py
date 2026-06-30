from __future__ import annotations

from datetime import datetime

from django.utils import timezone

from apps.icg.services import ClientesWebRow, ICGClientesWebWriter
from apps.prestashop.client import PrestashopClient, PrestashopCustomerSnapshot


def export_customer_to_icg(
    customer_id: int,
    *,
    client: PrestashopClient | None = None,
    writer: ICGClientesWebWriter | None = None,
    exported_at: datetime | None = None,
) -> dict[str, int | bool]:
    client = client or PrestashopClient()
    writer = writer or ICGClientesWebWriter()
    snapshot = client.get_customer_snapshot(customer_id)
    exported_at = exported_at or timezone.now()
    row = map_snapshot_to_clientes_web(snapshot, exported_at=exported_at)
    inserted = writer.insert_customer(row)
    return {
        "customer_id": snapshot.customer_id,
        "inserted": inserted,
    }


def export_customer_to_icg_from_job(entity_id: int | str) -> dict[str, int | bool]:
    return export_customer_to_icg(int(entity_id))


def map_snapshot_to_clientes_web(
    snapshot: PrestashopCustomerSnapshot,
    *,
    exported_at: datetime,
) -> ClientesWebRow:
    full_name = " ".join(
        part for part in [snapshot.firstname.strip(), snapshot.lastname.strip()] if part
    )
    address = snapshot.address
    cif = None
    if address is not None:
        cif = address.dni or address.vat_number

    primary_phone = None
    secondary_phone = None
    if address is not None:
        primary_phone = address.phone or address.phone_mobile
        if address.phone and address.phone_mobile and address.phone != address.phone_mobile:
            secondary_phone = address.phone_mobile

    # Match the agreed contract for ClientesWeb: NombreComercial is empty,
    # FechaExportacion is the current export timestamp in Django's configured
    # timezone, and the partner owns FechaInsercion, so we leave it as NULL.

    return ClientesWebRow(
        cod_cliente_web=snapshot.customer_id,
        nombre_cliente=_trim(full_name, 255),
        nombre_comercial="",
        cif=_trim(cif, 12),
        direccion=_trim(address.address1 if address else None, 255),
        cp=_trim(address.postcode if address else None, 8),
        poblacion=_trim(address.city if address else None, 100),
        provincia=_trim(address.state if address else None, 100),
        pais=_trim(address.country if address else None, 100),
        telefono1=_trim(primary_phone, 15),
        telefono2=_trim(secondary_phone, 15),
        fax=None,
        email=_trim(snapshot.email or None, 255),
        estado=1,
        fecha_exportacion=exported_at,
        fecha_insercion=None,
    )


def _trim(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_length]
