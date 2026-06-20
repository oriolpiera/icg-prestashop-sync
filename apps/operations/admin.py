from django.contrib import admin

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


@admin.action(description="Mark selected records for resynchronization")
def mark_for_resync(modeladmin, request, queryset):
    queryset.update(sync_required=True, last_sync_error="")


@admin.action(description="Retry selected jobs")
def retry_jobs(modeladmin, request, queryset):
    queryset.update(status=SyncJobStatus.PENDING, last_error="")


@admin.register(Manufacturer)
class ManufacturerAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "icg_code",
        "prestashop_id",
        "sync_required",
        "last_synced_at",
        "updated_at",
    )
    list_filter = ("sync_required",)
    search_fields = ("name", "icg_code")
    actions = (mark_for_resync,)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "prestashop_id",
        "parent",
        "position",
        "active",
        "category_type",
        "updated_at",
    )
    list_filter = ("active", "category_type")
    search_fields = ("name", "prestashop_id")
    actions = (mark_for_resync,)


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
    )
    list_filter = ("visible_web", "discontinued", "sync_required")
    search_fields = ("reference", "name", "icg_id")
    filter_horizontal = ("categories",)
    actions = (mark_for_resync,)


@admin.register(Combination)
class CombinationAdmin(admin.ModelAdmin):
    list_display = (
        "product",
        "icg_size",
        "icg_color",
        "ean13",
        "active",
        "sync_required",
    )
    list_filter = ("active", "sync_required")
    search_fields = ("product__reference", "icg_size", "icg_color", "ean13")
    actions = (mark_for_resync,)


@admin.register(Price)
class PriceAdmin(admin.ModelAdmin):
    list_display = ("combination", "amount_ex_vat", "vat_rate", "currency", "sync_required")
    list_filter = ("currency", "sync_required")
    search_fields = ("combination__product__reference",)
    actions = (mark_for_resync,)


@admin.register(TaxRuleMapping)
class TaxRuleMappingAdmin(admin.ModelAdmin):
    list_display = ("vat_rate", "prestashop_tax_rules_group_id", "label", "updated_at")
    search_fields = ("vat_rate", "label")


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("combination", "warehouse_code", "quantity", "sync_required")
    list_filter = ("warehouse_code", "sync_required")
    search_fields = ("combination__product__reference", "warehouse_code")
    actions = (mark_for_resync,)


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
    )
    list_filter = ("job_type", "status")
    search_fields = ("entity_type", "entity_key", "last_error")
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")
    actions = (retry_jobs,)
