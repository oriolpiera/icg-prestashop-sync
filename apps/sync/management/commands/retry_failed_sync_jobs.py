import logging

from django.core.management.base import BaseCommand

from apps.sync.tasks import retry_failed_jobs

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Retry sync jobs that failed with transient errors and are past their backoff window."

    def handle(self, *args, **options):
        self.stdout.write("Retrying failed sync jobs…")
        try:
            result = retry_failed_jobs()
        except Exception:
            logger.exception("retry_failed_sync_jobs failed")
            self.stderr.write(self.style.ERROR("retry_failed_sync_jobs failed. See logs."))
            self.returncode = 1
            return

        status = result.get("status", "unknown")
        retried = result.get("retried", 0)
        skipped = result.get("skipped", 0)

        if status == "skipped":
            self.stdout.write(
                self.style.WARNING(f"Skipped: lock already held ({result.get('reason', '')}).")
            )
            return

        self.stdout.write(self.style.SUCCESS(f"Done. retried={retried}, skipped={skipped}"))

        if retried == 0 and skipped == 0:
            self.stdout.write("No pending retryable jobs found.")
