from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
import requests
from django.utils import timezone

from apps.catalog.models import Manufacturer, Product
from apps.prestashop.client import PrestashopError
from apps.sales.models import PrestashopCustomer, PrestashopOrder
from apps.sync.errors import classify_error
from apps.sync.locking import LockAcquisitionError, sync_lock
from apps.sync.models import (
    MAX_SYNC_RETRIES,
    SyncError,
    SyncErrorType,
    SyncJob,
    SyncJobStatus,
    SyncJobType,
    SyncLock,
)
from apps.sync.tasks import (
    _record_sync_error,
    export_manufacturers,
    retry_entity,
    retry_failed_jobs,
)


@pytest.fixture(autouse=True)
def _clean_db(request):
    if request.node.get_closest_marker("django_db"):
        SyncError.objects.all().delete()
        SyncJob.objects.all().delete()
        SyncLock.objects.all().delete()
        PrestashopOrder.objects.all().delete()
        PrestashopCustomer.objects.all().delete()
        Manufacturer.objects.all().delete()
        Product.objects.all().delete()


# --- Error classification ---


class TestClassifyError:
    def test_transient_on_500(self):
        exc = PrestashopError("server error", status_code=500)
        assert classify_error(exc) == SyncErrorType.TRANSIENT

    def test_transient_on_503(self):
        exc = PrestashopError("service unavailable", status_code=503)
        assert classify_error(exc) == SyncErrorType.TRANSIENT

    def test_transient_on_429(self):
        exc = PrestashopError("rate limited", status_code=429)
        assert classify_error(exc) == SyncErrorType.TRANSIENT

    def test_permanent_on_400(self):
        exc = PrestashopError("bad request", status_code=400)
        assert classify_error(exc) == SyncErrorType.PERMANENT

    def test_permanent_on_401(self):
        exc = PrestashopError("unauthorized", status_code=401)
        assert classify_error(exc) == SyncErrorType.PERMANENT

    def test_permanent_on_403(self):
        exc = PrestashopError("forbidden", status_code=403)
        assert classify_error(exc) == SyncErrorType.PERMANENT

    def test_validation_on_404(self):
        exc = PrestashopError("not found", status_code=404)
        assert classify_error(exc) == SyncErrorType.VALIDATION

    def test_transient_on_connection_error(self):
        exc = requests.ConnectionError("connection refused")
        assert classify_error(exc) == SyncErrorType.TRANSIENT

    def test_transient_on_timeout(self):
        exc = requests.Timeout("request timed out")
        assert classify_error(exc) == SyncErrorType.TRANSIENT

    def test_permanent_on_unknown_exception(self):
        exc = ValueError("something wrong")
        assert classify_error(exc) == SyncErrorType.PERMANENT

    def test_permanent_on_prestashop_error_without_status(self):
        exc = PrestashopError("no status")
        assert classify_error(exc) == SyncErrorType.PERMANENT


# --- SyncError recording ---


@pytest.mark.django_db
class TestRecordSyncError:
    def test_creates_sync_error_record(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.RUNNING,
            attempts=1,
            started_at=timezone.now(),
        )

        exc = PrestashopError("boom", status_code=500)
        _record_sync_error(job, exc)

        error = SyncError.objects.get(job=job)
        assert error.entity_type == "product"
        assert error.entity_key == "REF001"
        assert error.error_type == SyncErrorType.TRANSIENT
        assert error.message == "boom"

    def test_transient_error_schedules_retry(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.RUNNING,
            attempts=1,
            started_at=timezone.now(),
        )

        exc = PrestashopError("server error", status_code=500)
        _record_sync_error(job, exc)

        job.refresh_from_db()
        assert job.status == SyncJobStatus.PENDING
        assert job.attempts == 2
        assert job.available_at > timezone.now()

    def test_permanent_error_does_not_retry(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.RUNNING,
            attempts=1,
            started_at=timezone.now(),
        )

        exc = PrestashopError("bad request", status_code=400)
        _record_sync_error(job, exc)

        job.refresh_from_db()
        assert job.status == SyncJobStatus.FAILED
        assert job.attempts == 1

    def test_max_retries_does_not_schedule(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.RUNNING,
            attempts=MAX_SYNC_RETRIES,
            started_at=timezone.now(),
        )

        exc = PrestashopError("server error", status_code=500)
        _record_sync_error(job, exc)

        job.refresh_from_db()
        assert job.status == SyncJobStatus.FAILED
        assert job.attempts == MAX_SYNC_RETRIES


# --- SyncJob retry logic ---


@pytest.mark.django_db
class TestSyncJobRetry:
    def test_is_retryable_when_transient_and_under_max(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.PENDING,
            attempts=1,
        )
        SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF001",
            error_type=SyncErrorType.TRANSIENT,
            message="server error",
        )
        assert job.is_retryable is True

    def test_is_not_retryable_when_permanent(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.PENDING,
            attempts=1,
        )
        SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF001",
            error_type=SyncErrorType.PERMANENT,
            message="bad request",
        )
        assert job.is_retryable is False

    def test_is_not_retryable_when_max_reached(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.PENDING,
            attempts=MAX_SYNC_RETRIES,
        )
        SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF001",
            error_type=SyncErrorType.TRANSIENT,
            message="server error",
        )
        assert job.is_retryable is False

    def test_schedule_retry_increments_attempts(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.FAILED,
            attempts=1,
        )

        job.schedule_retry()

        job.refresh_from_db()
        assert job.attempts == 2
        assert job.status == SyncJobStatus.PENDING

    def test_schedule_retry_uses_backoff(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.FAILED,
            attempts=1,
        )

        before = timezone.now()
        job.schedule_retry()
        after = timezone.now()

        job.refresh_from_db()
        expected_min = before + timedelta(seconds=300)
        expected_max = after + timedelta(seconds=300)
        assert expected_min <= job.available_at <= expected_max

    def test_error_type_returns_latest_error_type(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.FAILED,
        )
        SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF001",
            error_type=SyncErrorType.TRANSIENT,
            message="first",
        )
        SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF001",
            error_type=SyncErrorType.PERMANENT,
            message="second",
        )

        assert job.error_type == SyncErrorType.PERMANENT

    def test_error_type_returns_none_when_no_errors(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.FAILED,
        )
        assert job.error_type is None


# --- Locking ---


@pytest.mark.django_db
class TestSyncLock:
    def test_acquire_lock(self):
        with sync_lock("test-lock") as lock:
            assert lock.lock_key == "test-lock"
            assert SyncLock.objects.filter(lock_key="test-lock").exists()

    def test_release_lock(self):
        with sync_lock("test-lock"):
            pass
        assert not SyncLock.objects.filter(lock_key="test-lock").exists()

    def test_cannot_acquire_same_lock_twice(self):
        with sync_lock("test-lock"):
            with pytest.raises(LockAcquisitionError):
                with sync_lock("test-lock"):
                    pass

    def test_stale_lock_can_be_acquired(self):
        old_lock = SyncLock.objects.create(
            lock_key="stale-lock",
            locked_by="old-worker",
            locked_at=timezone.now() - timedelta(minutes=60),
        )

        with sync_lock("stale-lock") as lock:
            assert lock.locked_by != "old-worker"
            assert lock.locked_at > old_lock.locked_at

    def test_different_keys_can_coexist(self):
        with sync_lock("lock-a"):
            with sync_lock("lock-b"):
                assert SyncLock.objects.count() == 2


# --- Export tasks with new error handling ---


@pytest.mark.django_db
class TestExportTaskErrorHandling:
    def test_export_records_sync_error_on_failure(self, monkeypatch):
        Manufacturer.objects.create(icg_code="999", name="Failing Brand")

        def fake_export(manufacturer_id: int):
            raise PrestashopError("boom", status_code=500)

        monkeypatch.setattr("apps.sync.tasks.export_manufacturer", fake_export)

        export_manufacturers()

        error = SyncError.objects.first()
        assert error is not None
        assert error.entity_type == "manufacturer"
        assert error.entity_key == "999"
        assert error.error_type == SyncErrorType.TRANSIENT
        assert error.message == "boom"

    def test_export_skips_when_lock_held(self, monkeypatch):
        from apps.sync.locking import LockAcquisitionError

        Manufacturer.objects.create(icg_code="14000", name="ARTECREATION")

        def failing_lock(lock_key, timeout_minutes=30):
            raise LockAcquisitionError("lock held")

        monkeypatch.setattr("apps.sync.tasks.sync_lock", failing_lock)

        result = export_manufacturers()

        assert result["status"] == "skipped"
        assert result["reason"] == "lock_held"


# --- Retry entity task ---


@pytest.mark.django_db
class TestRetryEntityTask:
    def test_retry_records_sync_error_on_failure(self):
        product = Product.objects.create(
            icg_id=1001,
            reference="REF001",
            name="Test Product",
        )

        mock_export = Mock(side_effect=PrestashopError("API down", status_code=503))
        with patch.dict(
            "apps.sync.tasks._EXPORT_DISPATCH",
            {"product": (SyncJobType.EXPORT_PRODUCT, mock_export)},
        ):
            result = retry_entity("product", product.pk, product.reference)

        assert result["status"] == "failed"
        error = SyncError.objects.first()
        assert error is not None
        assert error.entity_type == "product"
        assert error.message == "API down"

    def test_successful_retry_entity_closes_superseded_jobs_for_same_entity(self):
        product = Product.objects.create(
            icg_id=1011,
            reference="REF011",
            name="Test Product 11",
        )
        stale_job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key=product.reference,
            status=SyncJobStatus.FAILED,
            attempts=2,
            payload={"entity_id": product.pk},
        )
        stale_error = SyncError.objects.create(
            job=stale_job,
            entity_type="product",
            entity_key=product.reference,
            error_type=SyncErrorType.TRANSIENT,
            message="old error",
        )

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 42})
        with patch.dict(
            "apps.sync.tasks._EXPORT_DISPATCH",
            {"product": (SyncJobType.EXPORT_PRODUCT, mock_export)},
        ):
            result = retry_entity("product", product.pk, product.reference)

        assert result["status"] == "succeeded"
        stale_job.refresh_from_db()
        stale_error.refresh_from_db()
        assert stale_job.status == SyncJobStatus.SUCCEEDED
        assert stale_job.last_error == ""
        assert stale_error.resolved is True

    def test_retry_entity_skips_when_pending_job_already_exists(self):
        product = Product.objects.create(
            icg_id=1012,
            reference="REF012",
            name="Test Product 12",
        )
        SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key=product.reference,
            status=SyncJobStatus.PENDING,
            attempts=2,
            payload={"entity_id": product.pk},
        )

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 42})
        with patch.dict(
            "apps.sync.tasks._EXPORT_DISPATCH",
            {"product": (SyncJobType.EXPORT_PRODUCT, mock_export)},
        ):
            result = retry_entity("product", product.pk, product.reference)

        assert result["status"] == "skipped"
        assert result["reason"] == "job_already_open"
        mock_export.assert_not_called()
        assert SyncJob.objects.filter(job_type=SyncJobType.EXPORT_PRODUCT).count() == 1

    def test_retry_entity_skips_sales_export_when_icg_lock_is_held(self):
        with patch(
            "apps.sync.tasks._maybe_icg_sales_export_lock",
            side_effect=LockAcquisitionError("lock held"),
        ):
            result = retry_entity("prestashop_customer", 42, "42")

        assert result == {
            "status": "skipped",
            "reason": "lock_held",
            "entity_type": "prestashop_customer",
            "entity_id": 42,
        }
        assert not SyncJob.objects.filter(
            job_type=SyncJobType.EXPORT_CUSTOMER,
            entity_type="prestashop_customer",
            entity_key="42",
        ).exists()


# --- Retry failed jobs task ---


@pytest.mark.django_db
class TestRetryFailedJobs:
    def test_retries_transient_failed_jobs(self):
        product = Product.objects.create(
            icg_id=1001,
            reference="REF001",
            name="Test Product",
        )
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF001",
            status=SyncJobStatus.PENDING,
            attempts=1,
            available_at=timezone.now() - timedelta(minutes=1),
            payload={"entity_id": product.pk},
        )
        error = SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF001",
            error_type=SyncErrorType.TRANSIENT,
            message="server error",
        )

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 42})
        with patch.dict(
            "apps.sync.tasks._RETRYABLE_EXPORT_MAP",
            {SyncJobType.EXPORT_PRODUCT: mock_export},
        ):
            result = retry_failed_jobs()

        assert result["retried"] == 1
        job.refresh_from_db()
        error.refresh_from_db()
        assert job.status == SyncJobStatus.SUCCEEDED
        assert error.resolved is True

    def test_successful_retry_closes_superseded_jobs_for_same_entity(self):
        product = Product.objects.create(
            icg_id=1010,
            reference="REF010",
            name="Test Product 10",
        )
        stale_job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF010",
            status=SyncJobStatus.FAILED,
            attempts=2,
            available_at=timezone.now() - timedelta(hours=1),
            payload={"entity_id": product.pk},
        )
        stale_error = SyncError.objects.create(
            job=stale_job,
            entity_type="product",
            entity_key="REF010",
            error_type=SyncErrorType.TRANSIENT,
            message="old server error",
        )
        retry_job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF010",
            status=SyncJobStatus.PENDING,
            attempts=1,
            available_at=timezone.now() - timedelta(minutes=1),
            payload={"entity_id": product.pk},
        )
        retry_error = SyncError.objects.create(
            job=retry_job,
            entity_type="product",
            entity_key="REF010",
            error_type=SyncErrorType.TRANSIENT,
            message="latest server error",
        )

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 42})
        with patch.dict(
            "apps.sync.tasks._RETRYABLE_EXPORT_MAP",
            {SyncJobType.EXPORT_PRODUCT: mock_export},
        ):
            result = retry_failed_jobs()

        assert result["retried"] == 1
        stale_job.refresh_from_db()
        retry_job.refresh_from_db()
        stale_error.refresh_from_db()
        retry_error.refresh_from_db()
        assert retry_job.status == SyncJobStatus.SUCCEEDED
        assert stale_job.status == SyncJobStatus.SUCCEEDED
        assert stale_job.last_error == ""
        assert stale_error.resolved is True
        assert retry_error.resolved is True

    def test_skips_permanent_errors(self):
        product = Product.objects.create(
            icg_id=1002,
            reference="REF002",
            name="Test Product 2",
        )
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF002",
            status=SyncJobStatus.PENDING,
            attempts=1,
            available_at=timezone.now() - timedelta(minutes=1),
            payload={"entity_id": product.pk},
        )
        SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF002",
            error_type=SyncErrorType.PERMANENT,
            message="bad request",
        )

        result = retry_failed_jobs()

        assert result["skipped"] >= 1
        job.refresh_from_db()
        assert job.status == SyncJobStatus.PENDING

    def test_skips_jobs_not_yet_available(self):
        product = Product.objects.create(
            icg_id=1003,
            reference="REF003",
            name="Test Product 3",
        )
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF003",
            status=SyncJobStatus.PENDING,
            attempts=1,
            available_at=timezone.now() + timedelta(hours=1),
            payload={"entity_id": product.pk},
        )
        SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF003",
            error_type=SyncErrorType.TRANSIENT,
            message="server error",
        )

        result = retry_failed_jobs()

        assert result["retried"] == 0
        job.refresh_from_db()
        assert job.status == SyncJobStatus.PENDING

    def test_marks_job_failed_when_max_retries_exhausted(self):
        product = Product.objects.create(
            icg_id=1005,
            reference="REF005",
            name="Test Product 5",
        )
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF005",
            status=SyncJobStatus.PENDING,
            attempts=MAX_SYNC_RETRIES + 1,
            available_at=timezone.now() - timedelta(minutes=1),
            payload={"entity_id": product.pk},
        )
        SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF005",
            error_type=SyncErrorType.TRANSIENT,
            message="server error",
        )

        result = retry_failed_jobs()

        assert result["skipped"] >= 1
        job.refresh_from_db()
        assert job.status == SyncJobStatus.FAILED
        assert job.finished_at is not None

    def test_retries_job_with_batch_payload_entity_id(self):
        product = Product.objects.create(
            icg_id=1004,
            reference="REF004",
            name="Test Product 4",
        )
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_PRODUCT,
            entity_type="product",
            entity_key="REF004",
            status=SyncJobStatus.PENDING,
            attempts=1,
            available_at=timezone.now() - timedelta(minutes=1),
            payload={
                "entity_id": product.pk,
                "product_id": product.pk,
                "icg_id": product.icg_id,
                "reference": product.reference,
            },
        )
        SyncError.objects.create(
            job=job,
            entity_type="product",
            entity_key="REF004",
            error_type=SyncErrorType.TRANSIENT,
            message="server error",
        )

        mock_export = Mock(return_value={"product_id": product.pk, "prestashop_id": 42})
        with patch.dict(
            "apps.sync.tasks._RETRYABLE_EXPORT_MAP",
            {SyncJobType.EXPORT_PRODUCT: mock_export},
        ):
            result = retry_failed_jobs()

        assert result["retried"] == 1
        mock_export.assert_called_once_with(product.pk)

    def test_returns_skipped_when_lock_held(self):
        from apps.sync.locking import LockAcquisitionError

        with patch(
            "apps.sync.tasks.sync_lock",
            side_effect=LockAcquisitionError("lock held"),
        ):
            result = retry_failed_jobs()

        assert result["status"] == "skipped"
        assert result["reason"] == "lock_held"

    def test_retries_customer_job_by_refreshing_when_mirror_is_missing(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_CUSTOMER,
            entity_type="prestashop_customer",
            entity_key="42",
            status=SyncJobStatus.PENDING,
            attempts=1,
            available_at=timezone.now() - timedelta(minutes=1),
            payload={"entity_id": 42, "customer_id": 42},
        )
        SyncError.objects.create(
            job=job,
            entity_type="prestashop_customer",
            entity_key="42",
            error_type=SyncErrorType.TRANSIENT,
            message="prestashop timeout",
        )

        with (
            patch("apps.sync.tasks.refresh_customer_from_prestashop") as refresh_mock,
            patch("apps.sync.tasks.export_customer_to_icg_from_mirror") as export_mock,
        ):
            refresh_mock.return_value = Mock()
            export_mock.return_value = {"customer_id": 42, "inserted": True}

            result = retry_failed_jobs()

        assert result["retried"] == 1
        refresh_mock.assert_called_once_with(42)
        export_mock.assert_called_once_with(42)
        job.refresh_from_db()
        assert job.status == SyncJobStatus.SUCCEEDED

    def test_retries_order_job_by_refreshing_when_mirror_is_missing(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_ORDER,
            entity_type="prestashop_order",
            entity_key="77",
            status=SyncJobStatus.PENDING,
            attempts=1,
            available_at=timezone.now() - timedelta(minutes=1),
            payload={"entity_id": 77, "order_id": 77},
        )
        SyncError.objects.create(
            job=job,
            entity_type="prestashop_order",
            entity_key="77",
            error_type=SyncErrorType.TRANSIENT,
            message="prestashop timeout",
        )

        with (
            patch("apps.sync.tasks.refresh_order_from_prestashop") as refresh_mock,
            patch("apps.sync.tasks.export_order_to_icg_from_mirror") as export_mock,
        ):
            refresh_mock.return_value = Mock()
            export_mock.return_value = {"order_id": 77, "inserted_rows": 3}

            result = retry_failed_jobs()

        assert result["retried"] == 1
        refresh_mock.assert_called_once_with(77)
        export_mock.assert_called_once_with(77)
        job.refresh_from_db()
        assert job.status == SyncJobStatus.SUCCEEDED

    def test_requeues_sales_job_when_icg_lock_is_held(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_CUSTOMER,
            entity_type="prestashop_customer",
            entity_key="42",
            status=SyncJobStatus.PENDING,
            attempts=1,
            available_at=timezone.now() - timedelta(minutes=1),
            payload={"entity_id": 42, "customer_id": 42},
        )
        SyncError.objects.create(
            job=job,
            entity_type="prestashop_customer",
            entity_key="42",
            error_type=SyncErrorType.TRANSIENT,
            message="sql timeout",
        )

        with patch(
            "apps.sync.tasks._maybe_icg_sales_export_lock",
            side_effect=LockAcquisitionError("lock held"),
        ):
            result = retry_failed_jobs()

        assert result["status"] == "success"
        assert result["retried"] == 0
        assert result["skipped"] == 1
        job.refresh_from_db()
        assert job.status == SyncJobStatus.PENDING
        assert job.attempts == 1
        assert job.started_at is None
        assert job.available_at > timezone.now()
        assert job.payload["icg_sales_export_lock_contention_count"] == 1

    def test_clears_lock_contention_counter_when_retry_runs_export(self):
        job = SyncJob.objects.create(
            job_type=SyncJobType.EXPORT_CUSTOMER,
            entity_type="prestashop_customer",
            entity_key="42",
            status=SyncJobStatus.PENDING,
            attempts=1,
            available_at=timezone.now() - timedelta(minutes=1),
            payload={
                "entity_id": 42,
                "customer_id": 42,
                "icg_sales_export_lock_contention_count": 2,
            },
        )
        SyncError.objects.create(
            job=job,
            entity_type="prestashop_customer",
            entity_key="42",
            error_type=SyncErrorType.TRANSIENT,
            message="sql timeout",
        )

        with patch.dict(
            "apps.sync.tasks._RETRYABLE_EXPORT_MAP",
            {SyncJobType.EXPORT_CUSTOMER: Mock(return_value={"customer_id": 42, "inserted": True})},
        ):
            result = retry_failed_jobs()

        assert result["status"] == "success"
        assert result["retried"] == 1
        assert result["skipped"] == 0
        job.refresh_from_db()
        assert job.status == SyncJobStatus.SUCCEEDED
        assert "icg_sales_export_lock_contention_count" not in job.payload
