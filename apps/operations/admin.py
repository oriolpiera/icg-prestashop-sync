import json
import logging
from datetime import timedelta

from django.contrib import admin, messages
from django.contrib.admin import SimpleListFilter, register
from django.db import models
from django.utils import timezone

from apps.catalog.models import (
    AttributeGroup,
    AttributeValue,
    Category,
    Combination,
    Manufacturer,
    Price,
    Product,
    Stock,
    TaxRuleMapping,
)
from apps.operations.sites import admin_site
from apps.sales.models import (
    ExportStatus,
    PrestashopCustomer,
    PrestashopOrder,
    PrestashopOrderDiscountLine,
    PrestashopOrderLine,
)
from apps.sync.cursor_service import advance_cursor
from apps.sync.models import (
    SyncCursor,
    SyncCursorSource,
    SyncError,
    SyncJob,
    SyncJobStatus,
    SyncJobType,
)
from apps.sync.tasks import STALE_RUNNING_JOB_TIMEOUT

logger = logging.getLogger(__name__)


def _has_open_export_job(job_type: str, entity_type: str, entity_key: str) -> bool:
    return (
        SyncJob.objects.filter(
            job_type=job_type,
            entity_type=entity_type,
            entity_key=entity_key,
        )
        .filter(
            models.Q(status=SyncJobStatus.PENDING)
            | models.Q(
                status=SyncJobStatus.RUNNING,
                started_at__gte=timezone.now() - STALE_RUNNING_JOB_TIMEOUT,
            )
        )
        .exists()
    )


class FailedSyncFilter(SimpleListFilter):
    title = "sync status"
    parameter_name = "has_error"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Failed (has error)"),
            ("no", "OK (no error)"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.exclude(last_sync_error="")
        if self.value() == "no":
            return queryset.filter(last_sync_error="")
        return queryset


class LastICGModifiedDatePresenceFilter(SimpleListFilter):
    title = "ICG modified date"
    parameter_name = "has_last_icg_modified_date"

    def lookups(self, request, model_admin):
        return (
            ("yes", "With date"),
            ("no", "Without date"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(last_icg_modified_date__isnull=False)
        if self.value() == "no":
            return queryset.filter(last_icg_modified_date__isnull=True)
        return queryset


class PrestashopIdFilter(SimpleListFilter):
    title = "PS ID status"
    parameter_name = "has_prestashop_id"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Synced (has PS ID)"),
            ("no", "Not synced (no PS ID)"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(prestashop_id__isnull=False)
        if self.value() == "no":
            return queryset.filter(prestashop_id__isnull=True)
        return queryset


class SpecificPriceFilter(SimpleListFilter):
    title = "specific price"
    parameter_name = "has_specific_price"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Has specific price"),
            ("no", "No specific price"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(prestashop_specific_price_id__isnull=False)
        if self.value() == "no":
            return queryset.filter(prestashop_specific_price_id__isnull=True)
        return queryset


class ExportStatusFilter(SimpleListFilter):
    title = "export status"
    parameter_name = "export_status"

    def lookups(self, request, model_admin):
        return ExportStatus.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(export_status=self.value())
        return queryset


class StuckJobFilter(SimpleListFilter):
    title = "stuck jobs"
    parameter_name = "is_stuck"

    def lookups(self, request, model_admin):
        return (("yes", "Stuck (> 30 min running)"),)

    def queryset(self, request, queryset):
        if self.value() == "yes":
            threshold = timezone.now() - timedelta(minutes=30)
            return queryset.filter(status=SyncJobStatus.RUNNING, started_at__lt=threshold)
        return queryset


@admin.action(description="Mark selected records for resynchronization")
def mark_for_resync(modeladmin, request, queryset):
    queryset.update(sync_required=True, last_sync_error="")


@admin.action(description="Retry selected entities now")
def retry_entity_sync(modeladmin, request, queryset):
    from apps.sync.tasks import retry_entity

    model = queryset.model
    entity_type = model.__name__.lower()

    entity_map = {
        "manufacturer": lambda obj: (obj.pk, obj.icg_code),
        "category": lambda obj: (obj.pk, obj.name),
        "product": lambda obj: (obj.pk, obj.reference),
        "combination": lambda obj: (
            obj.pk,
            f"{obj.product.reference}/{obj.icg_size}/{obj.icg_color}",
        ),
        "price": lambda obj: (
            obj.pk,
            f"{obj.combination.product.reference}/{obj.combination.icg_size}/{obj.combination.icg_color}",
        ),
        "stock": lambda obj: (
            obj.pk,
            f"{obj.combination.product.reference}/{obj.combination.icg_size}/{obj.combination.icg_color}",
        ),
    }

    getter = entity_map.get(entity_type)
    if getter is None:
        modeladmin.message_user(request, f"Retry not supported for {entity_type}", messages.WARNING)
        return

    select_related_map = {
        "manufacturer": [],
        "category": [],
        "product": [],
        "combination": ["product"],
        "price": ["combination__product"],
        "stock": ["combination__product"],
    }
    related = select_related_map.get(entity_type, [])

    count = 0
    for obj in queryset.select_related(*related):
        entity_id, entity_key = getter(obj)
        retry_entity.delay(entity_type, entity_id, entity_key)
        count += 1

    modeladmin.message_user(
        request,
        f"Dispatched {count} {entity_type}(s) for retry.",
        messages.SUCCESS,
    )


@admin.action(description="Update selected records from ICG")
def update_from_icg(modeladmin, request, queryset):
    from apps.icg.importer import (
        refresh_combination_from_icg,
        refresh_price_from_icg,
        refresh_product_from_icg,
        refresh_stock_from_icg,
    )

    refresh_map = {
        Product: refresh_product_from_icg,
        Combination: refresh_combination_from_icg,
        Price: refresh_price_from_icg,
        Stock: refresh_stock_from_icg,
    }

    refresh_fn = refresh_map.get(queryset.model)
    if refresh_fn is None:
        modeladmin.message_user(
            request, "Update from ICG is not supported for this model.", messages.WARNING
        )
        return

    updated = 0
    skipped = 0
    failed = 0
    for obj in queryset:
        try:
            result = refresh_fn(obj.pk)
        except Exception:
            logger.exception("Failed to refresh %s pk=%s from ICG", queryset.model.__name__, obj.pk)
            failed += 1
            continue

        if result.get("status") == "updated":
            updated += 1
        elif result.get("status") == "skipped":
            skipped += 1
        else:
            failed += 1

    level = messages.WARNING if failed else messages.SUCCESS
    modeladmin.message_user(
        request,
        f"Updated {updated} record(s) from ICG. Skipped {skipped}. Failed {failed}.",
        level,
    )


@admin.action(description="Retry selected discounts now")
def retry_discount_sync(modeladmin, request, queryset):
    from apps.sync.tasks import retry_entity

    eligible = queryset.filter(
        discontinued=False,
        prestashop_id__isnull=False,
    )
    skipped = queryset.count() - eligible.count()

    count = 0
    for obj in eligible:
        retry_entity.delay("discount", obj.pk, obj.reference)
        count += 1

    msg = f"Dispatched {count} discount(s) for retry."
    if skipped:
        msg += f" Skipped {skipped} (discontinued or not synced to PS)."
    modeladmin.message_user(request, msg, messages.SUCCESS)


@admin.action(description="Retry selected failed jobs")
def retry_jobs(modeladmin, request, queryset):
    failed = queryset.filter(status=SyncJobStatus.FAILED)
    count = failed.update(
        status=SyncJobStatus.PENDING,
        last_error="",
        attempts=0,
        available_at=timezone.now(),
    )
    if count:
        modeladmin.message_user(request, f"Reset {count} job(s) to pending.", messages.SUCCESS)
    else:
        modeladmin.message_user(
            request,
            "No failed jobs were found in the selection. Only FAILED jobs can be retried.",
            messages.WARNING,
        )


@admin.action(description="Refresh selected records from Prestashop")
def refresh_sales_from_prestashop(modeladmin, request, queryset):
    from apps.sync.tasks import refresh_prestashop_customer, refresh_prestashop_order

    if queryset.model is PrestashopCustomer:
        task = refresh_prestashop_customer
        id_attr = "prestashop_id"
        entity_name = "customer"
    elif queryset.model is PrestashopOrder:
        task = refresh_prestashop_order
        id_attr = "prestashop_id"
        entity_name = "order"
    else:
        modeladmin.message_user(
            request,
            "Refresh from Prestashop is not supported for this model.",
            messages.WARNING,
        )
        return

    count = 0
    for obj in queryset:
        task.delay(getattr(obj, id_attr))
        count += 1

    modeladmin.message_user(
        request,
        f"Dispatched {count} {entity_name}(s) for refresh from Prestashop.",
        messages.SUCCESS,
    )


@admin.action(description="Export selected records to ICG now")
def export_sales_to_icg(modeladmin, request, queryset):
    from apps.sync.tasks import retry_entity

    if queryset.model is PrestashopCustomer:
        entity_type = "prestashop_customer"
        entity_name = "customer"
        job_type = SyncJobType.EXPORT_CUSTOMER
    elif queryset.model is PrestashopOrder:
        entity_type = "prestashop_order"
        entity_name = "order"
        job_type = SyncJobType.EXPORT_ORDER
    else:
        modeladmin.message_user(
            request,
            "Export to ICG is not supported for this model.",
            messages.WARNING,
        )
        return

    count = 0
    skipped_open = 0
    for obj in queryset:
        entity_key = str(obj.prestashop_id)
        if _has_open_export_job(job_type, entity_type, entity_key):
            skipped_open += 1
            continue

        retry_entity.delay(entity_type, obj.prestashop_id, str(obj.prestashop_id))
        count += 1

    message = f"Dispatched {count} {entity_name}(s) for export to ICG."
    if skipped_open:
        message += f" Skipped {skipped_open} with an open export job."

    modeladmin.message_user(
        request,
        message,
        messages.WARNING if skipped_open else messages.SUCCESS,
    )


@admin.action(description="Set sync cursor to selected record")
def set_sales_sync_cursor(modeladmin, request, queryset):
    if queryset.model is PrestashopCustomer:
        source = SyncCursorSource.CUSTOMERS
        entity_name = "customer"
    elif queryset.model is PrestashopOrder:
        source = SyncCursorSource.ORDERS
        entity_name = "order"
    else:
        modeladmin.message_user(
            request,
            "Setting the sync cursor is not supported for this model.",
            messages.WARNING,
        )
        return

    selected = list(queryset.order_by("date_add", "prestashop_id"))
    if not selected:
        modeladmin.message_user(request, "No records selected.", messages.WARNING)
        return

    target = selected[-1]
    advance_cursor(source, target.date_add, str(target.prestashop_id))
    modeladmin.message_user(
        request,
        (
            f"Set {source.value} cursor to {entity_name} #{target.prestashop_id} "
            f"({target.date_add.isoformat()}). "
            "The next automatic export will start after this record."
        ),
        messages.SUCCESS,
    )


def _sync_error_display(obj):
    raw = getattr(obj, "last_sync_error", "")
    if not raw:
        return "-"
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("message", raw)[:80]
        return raw[:80]
    except (json.JSONDecodeError, TypeError, AttributeError):
        return raw[:80]


_sync_error_display.short_description = "last error"  # type: ignore[attr-defined]


class _CaseSensitiveSearchMixin:
    def get_search_results(self, request, queryset, search_term):
        if not search_term:
            return super().get_search_results(request, queryset, search_term)
        if search_term.startswith('"') and search_term.endswith('"'):
            term = search_term[1:-1]
            text_lookup = "contains"
        else:
            term = search_term
            text_lookup = "icontains"
        filters = models.Q()
        for field in self.search_fields:
            if field == "prestashop_id":
                filters |= models.Q(**{f"{field}__exact": term})
            else:
                filters |= models.Q(**{f"{field}__{text_lookup}": term})
        queryset = queryset.filter(filters)
        return queryset, False


@register(Manufacturer, site=admin_site)
class ManufacturerAdmin(_CaseSensitiveSearchMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "icg_code",
        "prestashop_id",
        "last_icg_modified_date",
        "sync_required",
        "last_synced_at",
        _sync_error_display,
        "updated_at",
    )
    list_filter = (
        "last_icg_modified_date",
        LastICGModifiedDatePresenceFilter,
        "sync_required",
        FailedSyncFilter,
    )
    search_fields = ("name", "icg_code", "prestashop_id")
    actions = (mark_for_resync, retry_entity_sync)


@register(Category, site=admin_site)
class CategoryAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "prestashop_id",
        "parent",
        "position",
        "active",
        "category_type",
        "sync_required",
        "last_synced_at",
        _sync_error_display,
        "updated_at",
    )
    list_filter = ("active", "category_type", "sync_required", FailedSyncFilter)
    search_fields = ("name", "prestashop_id")
    actions = (mark_for_resync, retry_entity_sync)


class CombinationInline(admin.TabularInline):
    model = Combination
    extra = 0
    readonly_fields = (
        "prestashop_id",
        "last_icg_modified_date",
        "ean13",
        "active",
        "sync_required",
        "last_synced_at",
        _sync_error_display,
    )
    fields = (
        "icg_size",
        "icg_color",
        "prestashop_id",
        "last_icg_modified_date",
        "ean13",
        "active",
        "sync_required",
        "last_synced_at",
        _sync_error_display,
    )


@register(Product, site=admin_site)
class ProductAdmin(_CaseSensitiveSearchMixin, admin.ModelAdmin):
    list_display = (
        "reference",
        "name",
        "prestashop_id",
        "last_icg_modified_date",
        "manufacturer",
        "category_default",
        "visible_web",
        "discontinued",
        "discount_percent",
        "prestashop_specific_price_id",
        "sync_required",
        "last_synced_at",
        _sync_error_display,
    )
    list_filter = (
        "last_icg_modified_date",
        LastICGModifiedDatePresenceFilter,
        "visible_web",
        "discontinued",
        "sync_required",
        "last_synced_at",
        PrestashopIdFilter,
        SpecificPriceFilter,
        FailedSyncFilter,
    )
    search_fields = ("reference", "name", "icg_id", "prestashop_id")
    filter_horizontal = ("categories",)
    actions = (mark_for_resync, retry_entity_sync, retry_discount_sync, update_from_icg)
    inlines = [CombinationInline]


def _product_visible_web(obj):
    return obj.product.visible_web


_product_visible_web.boolean = True  # type: ignore[attr-defined]
_product_visible_web.short_description = "Product visible"  # type: ignore[attr-defined]


def _product_discontinued(obj):
    return obj.product.discontinued


_product_discontinued.boolean = True  # type: ignore[attr-defined]
_product_discontinued.short_description = "Product discontinued"  # type: ignore[attr-defined]


@register(Combination, site=admin_site)
class CombinationAdmin(_CaseSensitiveSearchMixin, admin.ModelAdmin):
    list_display = (
        "product",
        "prestashop_id",
        "last_icg_modified_date",
        "icg_size",
        "icg_color",
        "ean13",
        "active",
        _product_visible_web,
        _product_discontinued,
        "sync_required",
        "last_synced_at",
        _sync_error_display,
    )
    list_filter = (
        "last_icg_modified_date",
        LastICGModifiedDatePresenceFilter,
        "active",
        "product__visible_web",
        "product__discontinued",
        "sync_required",
        "last_synced_at",
        PrestashopIdFilter,
        FailedSyncFilter,
    )
    search_fields = ("product__reference", "icg_size", "icg_color", "ean13", "prestashop_id")
    actions = (mark_for_resync, retry_entity_sync, update_from_icg)


def _combination_ps_id(obj):
    return obj.combination.prestashop_id or "-"


_combination_ps_id.short_description = "Combination PS ID"  # type: ignore[attr-defined]


def _combination_active(obj):
    return obj.combination.active


_combination_active.boolean = True  # type: ignore[attr-defined]
_combination_active.short_description = "Combination active"  # type: ignore[attr-defined]


def _price_product_visible_web(obj):
    return obj.combination.product.visible_web


_price_product_visible_web.boolean = True  # type: ignore[attr-defined]
_price_product_visible_web.short_description = "Product visible"  # type: ignore[attr-defined]


def _price_product_discontinued(obj):
    return obj.combination.product.discontinued


_price_product_discontinued.boolean = True  # type: ignore[attr-defined]
_price_product_discontinued.short_description = "Product discontinued"  # type: ignore[attr-defined]


@register(Price, site=admin_site)
class PriceAdmin(admin.ModelAdmin):
    list_select_related = ("combination", "combination__product")
    list_display = (
        "combination",
        "last_icg_modified_date",
        "amount_ex_vat",
        "vat_rate",
        "currency",
        _combination_active,
        _price_product_visible_web,
        _price_product_discontinued,
        "sync_required",
        "last_synced_at",
        _sync_error_display,
    )
    list_filter = (
        "last_icg_modified_date",
        LastICGModifiedDatePresenceFilter,
        "currency",
        "combination__active",
        "combination__product__visible_web",
        "combination__product__discontinued",
        "sync_required",
        "last_synced_at",
        FailedSyncFilter,
    )
    search_fields = ("combination__product__reference",)
    fields = (
        "combination",
        "last_icg_modified_date",
        "amount_ex_vat",
        "vat_rate",
        "currency",
        _combination_ps_id,
        _combination_active,
        "sync_required",
        "last_synced_at",
        "updated_at",
    )
    readonly_fields = (
        "combination",
        "last_icg_modified_date",
        _combination_ps_id,
        _combination_active,
        "last_synced_at",
        "updated_at",
    )
    actions = (mark_for_resync, retry_entity_sync, update_from_icg)


@register(TaxRuleMapping, site=admin_site)
class TaxRuleMappingAdmin(admin.ModelAdmin):
    list_display = ("vat_rate", "prestashop_tax_rules_group_id", "label", "updated_at")
    search_fields = ("vat_rate", "label")


def _stock_product_visible_web(obj):
    return obj.combination.product.visible_web


_stock_product_visible_web.boolean = True  # type: ignore[attr-defined]
_stock_product_visible_web.short_description = "Product visible"  # type: ignore[attr-defined]


def _stock_product_discontinued(obj):
    return obj.combination.product.discontinued


_stock_product_discontinued.boolean = True  # type: ignore[attr-defined]
_stock_product_discontinued.short_description = "Product discontinued"  # type: ignore[attr-defined]


@register(Stock, site=admin_site)
class StockAdmin(admin.ModelAdmin):
    list_select_related = ("combination", "combination__product")
    list_display = (
        "combination",
        "last_icg_modified_date",
        "warehouse_code",
        "quantity",
        _combination_active,
        _stock_product_visible_web,
        _stock_product_discontinued,
        "sync_required",
        "last_synced_at",
        _sync_error_display,
    )
    list_filter = (
        "last_icg_modified_date",
        LastICGModifiedDatePresenceFilter,
        "warehouse_code",
        "combination__active",
        "combination__product__visible_web",
        "combination__product__discontinued",
        "sync_required",
        "last_synced_at",
        FailedSyncFilter,
    )
    search_fields = ("combination__product__reference", "warehouse_code")
    fields = (
        "combination",
        "last_icg_modified_date",
        "warehouse_code",
        "quantity",
        _combination_ps_id,
        _combination_active,
        "sync_required",
        "last_synced_at",
        "updated_at",
    )
    readonly_fields = (
        "combination",
        "last_icg_modified_date",
        _combination_ps_id,
        _combination_active,
        "last_synced_at",
        "updated_at",
    )
    actions = (mark_for_resync, retry_entity_sync, update_from_icg)


@register(AttributeGroup, site=admin_site)
class AttributeGroupAdmin(_CaseSensitiveSearchMixin, admin.ModelAdmin):
    list_display = ("name", "icg_type", "product", "prestashop_id", "updated_at")
    search_fields = ("name", "icg_type", "product__reference", "prestashop_id")
    list_filter = ("icg_type",)


@register(AttributeValue, site=admin_site)
class AttributeValueAdmin(_CaseSensitiveSearchMixin, admin.ModelAdmin):
    list_display = (
        "attribute_group",
        "icg_value",
        "name",
        "prestashop_id",
        "texture_synced",
        "updated_at",
    )
    list_filter = ("attribute_group__icg_type", "texture_synced")
    search_fields = ("icg_value", "name", "attribute_group__product__reference", "prestashop_id")
    readonly_fields = ("texture_synced",)


@register(SyncCursor, site=admin_site)
class SyncCursorAdmin(admin.ModelAdmin):
    list_display = ("source", "last_modified_at", "last_source_key", "updated_at")
    readonly_fields = ("created_at", "updated_at")


@register(SyncJob, site=admin_site)
class SyncJobAdmin(admin.ModelAdmin):
    list_display = (
        "job_type",
        "entity_type",
        "entity_key",
        "status",
        "attempts",
        "available_at",
        "finished_at",
        "last_error_short",
    )
    list_filter = ("job_type", "status", StuckJobFilter)
    search_fields = ("entity_type", "entity_key", "last_error")
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")
    actions = (retry_jobs,)

    def last_error_short(self, obj):
        if not obj.last_error:
            return "-"
        return obj.last_error[:80]

    last_error_short.short_description = "last error"


@register(SyncError, site=admin_site)
class SyncErrorAdmin(admin.ModelAdmin):
    list_display = (
        "entity_type",
        "entity_key",
        "error_type",
        "message_short",
        "resolved",
        "created_at",
    )
    fields = (
        "job",
        "entity_type",
        "entity_key",
        "error_type",
        "message",
        "details",
        "resolved",
        "created_at",
        "updated_at",
    )
    list_filter = ("error_type", "resolved", "entity_type")
    search_fields = ("entity_type", "entity_key", "message")
    readonly_fields = ("job", "created_at", "updated_at")
    actions = ("mark_resolved",)

    def message_short(self, obj):
        return obj.message[:80]

    message_short.short_description = "message"

    @admin.action(description="Mark selected errors as resolved")
    def mark_resolved(self, request, queryset):
        count = queryset.update(resolved=True)
        self.message_user(request, f"Marked {count} error(s) as resolved.", messages.SUCCESS)


class PrestashopOrderLineInline(admin.TabularInline):
    model = PrestashopOrderLine
    extra = 0
    can_delete = False
    readonly_fields = (
        "position",
        "prestashop_product_id",
        "prestashop_combination_id",
        "description",
        "quantity",
        "unit_price_tax_incl",
        "total_price_tax_incl",
        "vat_rate",
    )
    raw_id_fields = ("override_combination",)


class PrestashopOrderDiscountLineInline(admin.TabularInline):
    model = PrestashopOrderDiscountLine
    extra = 0
    can_delete = False
    readonly_fields = (
        "position",
        "description",
        "amount_tax_incl",
        "amount_tax_excl",
        "vat_rate",
    )


@register(PrestashopCustomer, site=admin_site)
class PrestashopCustomerAdmin(admin.ModelAdmin):
    list_display = (
        "prestashop_id",
        "firstname",
        "lastname",
        "email",
        "city",
        "phone_or_mobile",
        "date_add",
        "export_status",
        "exported_to_icg_at",
        "last_export_error_short",
    )
    list_filter = (ExportStatusFilter, "country", "state", "date_add")
    search_fields = ("prestashop_id", "firstname", "lastname", "email", "city")
    readonly_fields = (
        "prestashop_id",
        "date_add",
        "last_snapshot_at",
        "export_status",
        "exported_to_icg_at",
        "last_export_error",
        "last_export_inserted",
        "created_at",
        "updated_at",
    )
    actions = (refresh_sales_from_prestashop, export_sales_to_icg, set_sales_sync_cursor)

    def phone_or_mobile(self, obj):
        return obj.phone or obj.phone_mobile or "-"

    phone_or_mobile.short_description = "phone"

    def last_export_error_short(self, obj):
        return (obj.last_export_error or "-")[:80]

    last_export_error_short.short_description = "last export error"


@register(PrestashopOrder, site=admin_site)
class PrestashopOrderAdmin(admin.ModelAdmin):
    list_display = (
        "prestashop_id",
        "customer",
        "payment",
        "total_paid_tax_incl",
        "total_shipping_tax_incl",
        "date_add",
        "inserted_rows",
        "export_status",
        "exported_to_icg_at",
        "last_export_error_short",
    )
    list_filter = (ExportStatusFilter, "payment", "date_add")
    search_fields = ("prestashop_id", "customer__firstname", "customer__lastname", "payment")
    readonly_fields = (
        "prestashop_id",
        "customer",
        "payment",
        "date_add",
        "total_paid_tax_incl",
        "total_shipping_tax_incl",
        "total_shipping_tax_excl",
        "last_snapshot_at",
        "export_status",
        "exported_to_icg_at",
        "inserted_rows",
        "last_export_error",
        "created_at",
        "updated_at",
    )
    inlines = [PrestashopOrderLineInline, PrestashopOrderDiscountLineInline]
    actions = (refresh_sales_from_prestashop, export_sales_to_icg, set_sales_sync_cursor)

    def last_export_error_short(self, obj):
        return (obj.last_export_error or "-")[:80]

    last_export_error_short.short_description = "last export error"
