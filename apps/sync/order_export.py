from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from apps.catalog.models import Combination, Product
from apps.icg.services import FacturasWebRow, ICGFacturasWebWriter
from apps.prestashop.client import (
    PrestashopClient,
    PrestashopError,
    PrestashopOrderLine,
    PrestashopOrderSnapshot,
)
from apps.sync.customer_export import map_prestashop_customer_id_to_icg_web_code

TIPO_DOCUMENTO_TICKET = 13
ESTADO_INSERCION = 1
COD_CLIENTE_WEB_DEFAULT = 0
FORMA_DE_PAGO_CARD = 2
FORMA_DE_PAGO_TRANSFER = 4
TRANSPORT_ARTICLE_CODE = 14873
DISCOUNT_ARTICLE_CODE = 14089


def export_order_to_icg(
    order_id: int,
    *,
    client: PrestashopClient | None = None,
    writer: ICGFacturasWebWriter | None = None,
    exported_at: datetime | None = None,
) -> dict[str, int]:
    client = client or PrestashopClient()
    writer = writer or ICGFacturasWebWriter()
    snapshot = client.get_order_snapshot(order_id)
    exported_at = exported_at or timezone.now()
    rows = map_snapshot_to_facturas_web(snapshot, exported_at=exported_at)
    inserted_rows = writer.insert_order_rows(rows)
    return {
        "order_id": snapshot.order_id,
        "inserted_rows": inserted_rows,
    }


def export_order_to_icg_from_job(entity_id: int | str) -> dict[str, int]:
    return export_order_to_icg(int(entity_id))


def map_snapshot_to_facturas_web(
    snapshot: PrestashopOrderSnapshot,
    *,
    exported_at: datetime,
) -> list[FacturasWebRow]:
    forma_de_pago = _map_payment_method(snapshot.payment)
    total_document = _to_money(snapshot.total_paid_tax_incl)
    rows: list[FacturasWebRow] = []

    for line in snapshot.lines:
        combination = _resolve_catalog_combination(line)
        rows.append(
            FacturasWebRow(
                tipo_documento=TIPO_DOCUMENTO_TICKET,
                num_documento=snapshot.order_id,
                num_lin=len(rows) + 1,
                cod_cliente=COD_CLIENTE_WEB_DEFAULT,
                cod_cliente_web=map_prestashop_customer_id_to_icg_web_code(snapshot.customer_id),
                cod_articulo=combination.product.icg_id,
                talla=_variant_value(combination.icg_size),
                color=_variant_value(combination.icg_color),
                descripcion=_trim(line.description, 255),
                unidades_total=line.quantity,
                precio_iva=_to_money(line.unit_price_tax_incl),
                dto=None,
                total=total_document,
                fecha_documento=snapshot.date_add,
                estado=ESTADO_INSERCION,
                forma_de_pago=forma_de_pago,
                total_iva=_to_money(line.total_price_tax_incl),
                tipo_iva=_map_tipo_iva(line.vat_rate),
                fecha_exportacion=exported_at,
                fecha_insercion=None,
                num_documento_mng=None,
                total_lin=0,
                cod_barras=_trim(combination.ean13, 50),
            )
        )

    if snapshot.total_shipping_tax_incl > 0:
        shipping_total = _to_money(snapshot.total_shipping_tax_incl)
        rows.append(
            FacturasWebRow(
                tipo_documento=TIPO_DOCUMENTO_TICKET,
                num_documento=snapshot.order_id,
                num_lin=len(rows) + 1,
                cod_cliente=COD_CLIENTE_WEB_DEFAULT,
                cod_cliente_web=map_prestashop_customer_id_to_icg_web_code(snapshot.customer_id),
                cod_articulo=TRANSPORT_ARTICLE_CODE,
                talla=".",
                color=".",
                descripcion="Gastos de transporte",
                unidades_total=1,
                precio_iva=shipping_total,
                dto=None,
                total=total_document,
                fecha_documento=snapshot.date_add,
                estado=ESTADO_INSERCION,
                forma_de_pago=forma_de_pago,
                total_iva=shipping_total,
                tipo_iva=_map_tipo_iva(
                    _derive_vat_rate(
                        snapshot.total_shipping_tax_incl,
                        snapshot.total_shipping_tax_excl,
                    )
                ),
                fecha_exportacion=exported_at,
                fecha_insercion=None,
                num_documento_mng=None,
                total_lin=0,
                cod_barras=None,
            )
        )

    for discount in snapshot.discounts:
        amount = _to_money(discount.amount_tax_incl)
        rows.append(
            FacturasWebRow(
                tipo_documento=TIPO_DOCUMENTO_TICKET,
                num_documento=snapshot.order_id,
                num_lin=len(rows) + 1,
                cod_cliente=COD_CLIENTE_WEB_DEFAULT,
                cod_cliente_web=map_prestashop_customer_id_to_icg_web_code(snapshot.customer_id),
                cod_articulo=DISCOUNT_ARTICLE_CODE,
                talla=".",
                color=".",
                descripcion=_trim(discount.description, 255),
                unidades_total=-1,
                precio_iva=amount,
                dto=None,
                total=total_document,
                fecha_documento=snapshot.date_add,
                estado=ESTADO_INSERCION,
                forma_de_pago=forma_de_pago,
                total_iva=-amount,
                tipo_iva=_map_tipo_iva(discount.vat_rate),
                fecha_exportacion=exported_at,
                fecha_insercion=None,
                num_documento_mng=None,
                total_lin=0,
                cod_barras=None,
            )
        )

    total_lines = len(rows)
    for row in rows:
        row.total_lin = total_lines
    return rows


def _resolve_catalog_combination(line: PrestashopOrderLine) -> Combination:
    override_combination_id = getattr(line, "override_combination_id", None)
    if override_combination_id:
        combination = (
            Combination.objects.select_related("product").filter(pk=override_combination_id).first()
        )
        if combination is None:
            raise PrestashopError(
                f"Override combination {override_combination_id} not found in catalog.",
                status_code=400,
            )
        return combination

    if line.combination_id:
        combination = (
            Combination.objects.select_related("product")
            .filter(prestashop_id=line.combination_id)
            .first()
        )
        if combination is None:
            raise PrestashopError(
                f"No catalog combination found for Prestashop combination {line.combination_id}.",
                status_code=400,
            )
        return combination

    product = Product.objects.filter(prestashop_id=line.product_id).first()
    if product is None:
        raise PrestashopError(
            f"No catalog product found for Prestashop product {line.product_id}.",
            status_code=400,
        )

    combinations = list(Combination.objects.select_related("product").filter(product=product))
    if len(combinations) != 1:
        raise PrestashopError(
            (
                f"Prestashop product {line.product_id} needs exactly one catalog combination "
                "for order export."
            ),
            status_code=400,
        )
    return combinations[0]


def _map_payment_method(payment: str) -> int:
    normalized = (payment or "").strip().lower()
    if any(token in normalized for token in ["transfer", "transferencia"]):
        return FORMA_DE_PAGO_TRANSFER
    if any(token in normalized for token in ["card", "targeta", "tarjeta", "redsys", "visa"]):
        return FORMA_DE_PAGO_CARD
    raise PrestashopError(
        f"Unsupported Prestashop payment method for FacturasWeb: {payment or '<empty>'}",
        status_code=400,
    )


def _map_tipo_iva(vat_rate: Decimal) -> int:
    normalized = vat_rate.quantize(Decimal("0.01"))
    if normalized == Decimal("21.00"):
        return 1
    if normalized == Decimal("10.00"):
        return 2
    if normalized == Decimal("0.00"):
        return 0
    raise PrestashopError(f"Unsupported VAT rate for FacturasWeb: {normalized}", status_code=400)


def _derive_vat_rate(amount_tax_incl: Decimal, amount_tax_excl: Decimal) -> Decimal:
    if amount_tax_incl <= 0 or amount_tax_excl <= 0:
        return Decimal("0.00")
    return ((amount_tax_incl - amount_tax_excl) / amount_tax_excl * Decimal("100")).quantize(
        Decimal("0.01")
    )


def _to_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _variant_value(value: str | None) -> str:
    cleaned = _trim(value, 10)
    return cleaned or "."


def _trim(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_length]
