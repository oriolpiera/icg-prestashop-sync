from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from apps.icg.services import ClientesWebRow, ICGClientesWebWriter
from apps.prestashop.client import PrestashopClient, PrestashopCustomerSnapshot


@dataclass(slots=True)
class CustomerExportResult:
    customer_id: int
    inserted: bool


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

    return ClientesWebRow(
        cod_cliente_web=snapshot.customer_id,
        nombre_cliente=full_name or None,
        nombre_comercial=full_name or None,
        cif=cif,
        direccion=address.address1 if address else None,
        cp=address.postcode if address else None,
        poblacion=address.city if address else None,
        provincia=address.state if address else None,
        pais=address.country if address else None,
        telefono1=address.phone if address else None,
        telefono2=address.phone_mobile if address else None,
        fax=None,
        email=snapshot.email or None,
        estado=1,
        fecha_exportacion=exported_at,
        fecha_insercion=snapshot.date_add,
    )
