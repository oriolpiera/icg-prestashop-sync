from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


@pytest.mark.django_db
class TestRetryFailedSyncJobs:
    def test_retried_jobs(self):
        out = StringIO()
        with patch(
            "apps.sync.management.commands.retry_failed_sync_jobs.retry_failed_jobs",
            return_value={"status": "success", "retried": 3, "skipped": 1},
        ):
            call_command("retry_failed_sync_jobs", stdout=out)

        output = out.getvalue()
        assert "retried=3" in output
        assert "skipped=1" in output

    def test_reports_non_retryable_pending_jobs(self):
        out = StringIO()
        with patch(
            "apps.sync.management.commands.retry_failed_sync_jobs.retry_failed_jobs",
            return_value={
                "status": "success",
                "retried": 0,
                "skipped": 0,
                "non_retryable_pending": 5,
            },
        ):
            call_command("retry_failed_sync_jobs", stdout=out)

        output = out.getvalue()
        assert "Pending non-retryable jobs still due: 5" in output

    def test_lock_skipped(self):
        out = StringIO()
        with patch(
            "apps.sync.management.commands.retry_failed_sync_jobs.retry_failed_jobs",
            return_value={"status": "skipped", "reason": "lock_held"},
        ):
            call_command("retry_failed_sync_jobs", stdout=out)

        assert "Skipped: lock already held" in out.getvalue()

    def test_no_pending_jobs(self):
        out = StringIO()
        with patch(
            "apps.sync.management.commands.retry_failed_sync_jobs.retry_failed_jobs",
            return_value={"status": "success", "retried": 0, "skipped": 0},
        ):
            call_command("retry_failed_sync_jobs", stdout=out)

        assert "No pending retryable jobs found." in out.getvalue()

    def test_exception_raises_command_error(self):
        with patch(
            "apps.sync.management.commands.retry_failed_sync_jobs.retry_failed_jobs",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(CommandError, match="retry_failed_sync_jobs failed"):
                call_command("retry_failed_sync_jobs", stdout=StringIO())
