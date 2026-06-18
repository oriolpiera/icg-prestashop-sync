import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import Combination, Manufacturer, Price, Product, Stock
from apps.icg.services import ICGCatalogReader
from apps.sync.cursor_service import advance_cursor, get_or_create_cursor
from apps.sync.models import SyncCursorSource, SyncJob, SyncJobType

logger = logging.getLogger(__name__)


def _make_aware(dt: datetime) -> datetime:
    if timezone.is_naive(dt):
        return timezone.make_aware(dt)
    return dt


def _escape(value: str) -> str:
    return value.replace("{", "").replace("}", "").replace("'", "")


def _persist_product_row(row) -> tuple[str | None, datetime]:
    icg_id = int(row[0])
    reference = str(row[1]).strip()
    modified_at = _make_aware(row[11])

    if not icg_id or not reference:
        logger.warning(
            "Skipping product row with missing icg_id=%s or reference=%s", icg_id, reference
        )
        return None, modified_at

    icg_size = _escape(str(row[2]))
    icg_color = _escape(str(row[3]))
    ean13 = str(row[4]).strip() if row[4] else ""
    name = str(row[6]).strip() if row[6] else ""
    visible_web = str(row[12]).strip().upper() == "T"
    manufacturer_code = str(row[13]).strip() if row[13] else ""
    manufacturer_name = str(row[14]).strip() if row[14] else ""
    discontinued = str(row[15]).strip().upper() == "T"

    with transaction.atomic():
        manufacturer = None
        if manufacturer_code:
            manufacturer, _ = Manufacturer.objects.get_or_create(
                icg_code=manufacturer_code,
                defaults={"name": manufacturer_name or manufacturer_code},
            )

        product, created = Product.objects.update_or_create(
            icg_id=icg_id,
            defaults={
                "reference": reference,
                "name": name,
                "manufacturer": manufacturer,
                "visible_web": visible_web,
                "discontinued": discontinued,
                "sync_required": True,
            },
        )
        if not created:
            changed = False
            if product.reference != reference:
                product.reference = reference
                changed = True
            if product.name != name:
                product.name = name
                changed = True
            if product.manufacturer != manufacturer:
                product.manufacturer = manufacturer
                changed = True
            if product.visible_web != visible_web:
                product.visible_web = visible_web
                changed = True
            if product.discontinued != discontinued:
                product.discontinued = discontinued
                changed = True
            if changed:
                product.sync_required = True
                product.save()

        combination, comb_created = Combination.objects.update_or_create(
            product=product,
            icg_size=icg_size,
            icg_color=icg_color,
            defaults={
                "ean13": ean13,
                "active": not discontinued,
                "sync_required": True,
            },
        )
        if not comb_created:
            comb_changed = False
            if combination.ean13 != ean13:
                combination.ean13 = ean13
                comb_changed = True
            if combination.active == discontinued:
                combination.active = not discontinued
                comb_changed = True
            if comb_changed:
                combination.sync_required = True
                combination.save()

    SyncJob.objects.create(
        job_type=SyncJobType.IMPORT_PRODUCTS,
        entity_type="combination",
        entity_key=f"{icg_id}/{icg_size}/{icg_color}",
        payload={
            "icg_id": icg_id,
            "reference": reference,
            "product_id": product.pk,
            "combination_id": combination.pk,
        },
    )

    return f"{icg_id}/{icg_size}/{icg_color}", modified_at


def _persist_price_row(row) -> tuple[str | None, datetime]:
    modified_at = _make_aware(row[12])
    icg_id = int(row[1])
    icg_size = _escape(str(row[2]))
    icg_color = _escape(str(row[3]))
    vat_rate = row[8]
    amount_ex_vat = row[10]

    if not icg_id:
        logger.warning("Skipping price row with missing icg_id")
        return None, modified_at

    with transaction.atomic():
        product = Product.objects.filter(icg_id=icg_id).first()
        if not product:
            logger.warning("Product icg_id=%s not found for price import, skipping", icg_id)
            return None, modified_at

        combination = Combination.objects.filter(
            product=product, icg_size=icg_size, icg_color=icg_color
        ).first()
        if not combination:
            logger.warning(
                "Combination not found for product %s size=%s color=%s, skipping",
                icg_id,
                icg_size,
                icg_color,
            )
            return None, modified_at

        price, created = Price.objects.update_or_create(
            combination=combination,
            defaults={
                "amount_ex_vat": amount_ex_vat,
                "vat_rate": vat_rate,
                "sync_required": True,
            },
        )
        if not created:
            changed = False
            if price.amount_ex_vat != amount_ex_vat:
                price.amount_ex_vat = amount_ex_vat
                changed = True
            if price.vat_rate != vat_rate:
                price.vat_rate = vat_rate
                changed = True
            if changed:
                price.sync_required = True
                price.save()

    SyncJob.objects.create(
        job_type=SyncJobType.IMPORT_PRICES,
        entity_type="price",
        entity_key=f"{icg_id}/{icg_size}/{icg_color}",
        payload={
            "icg_id": icg_id,
            "combination_id": combination.pk,
            "amount_ex_vat": str(amount_ex_vat),
        },
    )

    return f"{icg_id}/{icg_size}/{icg_color}", modified_at


def _persist_stock_row(row) -> tuple[str | None, datetime]:
    modified_at = _make_aware(row[8])
    icg_id = int(row[0])
    icg_size = _escape(str(row[1]))
    icg_color = _escape(str(row[2]))
    warehouse_code = str(row[3]).strip() if row[3] else ""
    quantity = int(row[7]) if row[7] else 0

    if not icg_id:
        logger.warning("Skipping stock row with missing icg_id")
        return None, modified_at

    if warehouse_code != "01":
        logger.debug("Skipping stock for warehouse %s (icg_id=%s)", warehouse_code, icg_id)
        return None, modified_at

    with transaction.atomic():
        product = Product.objects.filter(icg_id=icg_id).first()
        if not product:
            logger.warning("Product icg_id=%s not found for stock import, skipping", icg_id)
            return None, modified_at

        combination = Combination.objects.filter(
            product=product, icg_size=icg_size, icg_color=icg_color
        ).first()
        if not combination:
            logger.warning(
                "Combination not found for product %s size=%s color=%s, skipping",
                icg_id,
                icg_size,
                icg_color,
            )
            return None, modified_at

        stock, created = Stock.objects.update_or_create(
            combination=combination,
            defaults={
                "warehouse_code": warehouse_code,
                "quantity": quantity if quantity > 0 else 0,
                "sync_required": True,
            },
        )
        if not created:
            changed = False
            if stock.quantity != quantity:
                stock.quantity = quantity if quantity > 0 else 0
                changed = True
            if stock.warehouse_code != warehouse_code:
                stock.warehouse_code = warehouse_code
                changed = True
            if changed:
                stock.sync_required = True
                stock.save()

    SyncJob.objects.create(
        job_type=SyncJobType.IMPORT_STOCK,
        entity_type="stock",
        entity_key=f"{icg_id}/{icg_size}/{icg_color}",
        payload={
            "icg_id": icg_id,
            "combination_id": combination.pk,
            "quantity": quantity,
        },
    )

    return f"{icg_id}/{icg_size}/{icg_color}", modified_at


def _import_source(
    source: SyncCursorSource,
    fetch_fn,
    persist_row_fn,
    max_rows: int = 5000,
) -> dict:
    cursor = get_or_create_cursor(source)
    cursor_at = cursor.last_modified_at

    processed = 0
    skipped = 0
    all_hard = True

    while True:
        try:
            rows, has_more = fetch_fn(cursor_at, limit=max_rows)
        except Exception:
            logger.exception(
                "Failed to fetch %s rows from ICG at cursor %s", source.value, cursor_at
            )
            break

        if not rows:
            logger.info("No new %s to import from ICG", source.value)
            break

        last_row_date: datetime | None = cursor_at

        for row in rows:
            try:
                result, row_date = persist_row_fn(row)
                last_row_date = row_date
                if result is not None:
                    processed += 1
                else:
                    skipped += 1
            except Exception:
                logger.exception(
                    "Hard failure persisting %s row, cursor will not advance past this point: %s",
                    source.value,
                    row[0] if row else "?",
                )
                all_hard = False
                break

        if last_row_date is not None and last_row_date != cursor_at and all_hard:
            advance_cursor(source, last_row_date)
            cursor_at = last_row_date

        if not has_more or not all_hard:
            break

    logger.info(
        "ICG %s import: processed=%d skipped=%d cursor=%s",
        source.value,
        processed,
        skipped,
        cursor_at,
    )

    return {
        "status": "success",
        "source": source.value,
        "processed": processed,
        "skipped": skipped,
    }


def import_products() -> dict:
    logger.info("Starting ICG product import")
    reader = ICGCatalogReader()

    def fetch_fn(cursor_at, limit=5000):
        return reader.fetch_products_after(cursor_at, limit=limit)

    return _import_source(SyncCursorSource.PRODUCTS, fetch_fn, _persist_product_row)


def import_prices() -> dict:
    logger.info("Starting ICG price import")
    reader = ICGCatalogReader()

    def fetch_fn(cursor_at, limit=5000):
        return reader.fetch_prices_after(cursor_at, limit=limit)

    return _import_source(SyncCursorSource.PRICES, fetch_fn, _persist_price_row)


def import_stock() -> dict:
    logger.info("Starting ICG stock import")
    reader = ICGCatalogReader()

    def fetch_fn(cursor_at, limit=5000):
        return reader.fetch_stock_after(cursor_at, limit=limit)

    return _import_source(SyncCursorSource.STOCK, fetch_fn, _persist_stock_row)
