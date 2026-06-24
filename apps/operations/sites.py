from datetime import timedelta

from django.contrib.admin import AdminSite as DjangoAdminSite
from django.db.models import Count
from django.utils import timezone

from apps.catalog.models import (
    Category,
    Combination,
    Manufacturer,
    Price,
    Product,
    Stock,
)
from apps.sync.models import SyncCursor, SyncError, SyncJob


class AdminSite(DjangoAdminSite):
    site_header = "ICG → PrestaShop Sync"
    site_title = "ICG Sync"
    index_title = "Dashboard"

    def index(self, request, extra_context=None):
        extra_context = extra_context or {}

        entity_defs = [
            ("Products", Product),
            ("Combinations", Combination),
            ("Prices", Price),
            ("Stock", Stock),
            ("Categories", Category),
            ("Manufacturers", Manufacturer),
        ]

        totals = {"total": 0, "synced_ok": 0, "pending": 0, "error": 0, "never_synced": 0}
        entity_stats = []

        for label, model in entity_defs:
            total = model.objects.count()
            synced_ok = model.objects.filter(
                sync_required=False, last_sync_error="", last_synced_at__isnull=False
            ).count()
            pending = model.objects.filter(sync_required=True).count()
            error = model.objects.exclude(last_sync_error="").count()
            never_synced = model.objects.filter(last_synced_at__isnull=True).count()

            inactive = None
            if hasattr(model, "active"):
                inactive = model.objects.filter(active=False).count()
            elif hasattr(model, "visible_web"):
                inactive = model.objects.filter(visible_web=False, discontinued=False).count()

            entity_stats.append(
                {
                    "label": label,
                    "total": total,
                    "synced_ok": synced_ok,
                    "pending": pending,
                    "error": error,
                    "never_synced": never_synced,
                    "inactive": inactive,
                }
            )
            totals["total"] += total
            totals["synced_ok"] += synced_ok
            totals["pending"] += pending
            totals["error"] += error
            totals["never_synced"] += never_synced

        extra_context["entity_stats"] = entity_stats
        extra_context["totals"] = totals

        job_status_counts = (
            SyncJob.objects.values("status").annotate(count=Count("id")).order_by("status")
        )
        extra_context["job_status_counts"] = {s["status"]: s["count"] for s in job_status_counts}
        extra_context["stuck_jobs"] = SyncJob.objects.filter(
            status="running",
            started_at__lte=timezone.now() - timedelta(minutes=30),
        ).count()
        extra_context["unresolved_errors"] = SyncError.objects.filter(resolved=False).count()
        extra_context["cursors"] = SyncCursor.objects.all()

        return super().index(request, extra_context=extra_context)


admin_site = AdminSite(name="operations_admin")
