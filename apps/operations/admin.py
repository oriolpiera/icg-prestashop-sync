import json
from datetime import timedelta

from django.contrib import admin, messages
from django.contrib.admin import SimpleListFilter
from django.utils import timezone

from apps.catalog.models import (
    AttributeGroup,
    AttributeValue,
    Category,
    Combination,
    Manufacturer,
    PrestashopMapping,
    Price,
    Product,
    Stock,
    TaxRuleMapping,
)
from apps.sync.models import SyncCursor, SyncJob, SyncJobStatus


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


@admin.action(description="Retry selected failed jobs")
def retry_jobs(modeladmin, request, queryset):
    failed = queryset.filter(status=SyncJobStatus.FAILED)
    count = failed.update(status=SyncJobStatus.PENDING, last_error="")
    if count:
        modeladmin.message_user(request, f"Reset {count} job(s) to pending.", messages.SUCCESS)
    else:
        modeladmin.message_user(
            request,
            "No failed jobs were found in the selection. Only FAILED jobs can be retried.",
            messages.WARNING,
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


@admin.register(Manufacturer)
class ManufacturerAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "icg_code",
        "prestashop_id",
        "sync_required",
        "last_synced_at",
        _sync_error_display,
        "updated_at",
    )
    list_filter = ("sync_required", FailedSyncFilter)
    search_fields = ("name", "icg_code")
    actions = (mark_for_resync, retry_entity_sync)


@admin.register(Category)
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


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "name",
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
    list_filter = ("visible_web", "discontinued", "sync_required", FailedSyncFilter)
    search_fields = ("reference", "name", "icg_id")
    filter_horizontal = ("categories",)
    actions = (mark_for_resync, retry_entity_sync)


@admin.register(Combination)
class CombinationAdmin(admin.ModelAdmin):
    list_display = (
        "product",
        "icg_size",
        "icg_color",
        "ean13",
        "active",
        "sync_required",
        "last_synced_at",
        _sync_error_display,
    )
    list_filter = ("active", "sync_required", FailedSyncFilter)
    search_fields = ("product__reference", "icg_size", "icg_color", "ean13")
    actions = (mark_for_resync, retry_entity_sync)


@admin.register(Price)
class PriceAdmin(admin.ModelAdmin):
    list_display = (
        "combination",
        "amount_ex_vat",
        "vat_rate",
        "currency",
        "sync_required",
        "last_synced_at",
        _sync_error_display,
    )
    list_filter = ("currency", "sync_required", FailedSyncFilter)
    search_fields = ("combination__product__reference",)
    actions = (mark_for_resync, retry_entity_sync)


@admin.register(TaxRuleMapping)
class TaxRuleMappingAdmin(admin.ModelAdmin):
    list_display = ("vat_rate", "prestashop_tax_rules_group_id", "label", "updated_at")
    search_fields = ("vat_rate", "label")


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = (
        "combination",
        "warehouse_code",
        "quantity",
        "sync_required",
        "last_synced_at",
        _sync_error_display,
    )
    list_filter = ("warehouse_code", "sync_required", FailedSyncFilter)
    search_fields = ("combination__product__reference", "warehouse_code")
    actions = (mark_for_resync, retry_entity_sync)


@admin.register(AttributeGroup)
class AttributeGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "icg_type", "prestashop_id", "updated_at")
    search_fields = ("name", "icg_type")


@admin.register(AttributeValue)
class AttributeValueAdmin(admin.ModelAdmin):
    list_display = ("attribute_group", "icg_value", "name", "prestashop_id", "updated_at")
    list_filter = ("attribute_group__icg_type",)
    search_fields = ("icg_value", "name")


@admin.register(PrestashopMapping)
class PrestashopMappingAdmin(admin.ModelAdmin):
    list_display = (
        "product",
        "combination",
        "prestashop_product_id",
        "prestashop_combination_id",
        "updated_at",
    )
    search_fields = ("product__reference", "prestashop_product_id", "prestashop_combination_id")


@admin.register(SyncCursor)
class SyncCursorAdmin(admin.ModelAdmin):
    list_display = ("source", "last_modified_at", "last_source_key", "updated_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SyncJob)
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
