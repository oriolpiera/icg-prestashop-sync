import json
from unittest.mock import Mock

import pytest

from apps.catalog.models import Manufacturer
from apps.prestashop.client import PrestashopError
from apps.prestashop.services import export_manufacturer
from apps.sync.models import SyncJob, SyncJobStatus, SyncJobType
from apps.sync.tasks import export_manufacturers


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    Manufacturer.objects.all().delete()


@pytest.mark.django_db
class TestManufacturerExport:
    def test_export_creates_and_maps_new_manufacturer(self):
        manufacturer = Manufacturer.objects.create(icg_code="14000", name="ARTECREATION")
        client = Mock()
        client.find_manufacturer_id_by_name.return_value = None
        client.create_manufacturer.return_value = 77

        result = export_manufacturer(manufacturer.pk, client=client)

        manufacturer.refresh_from_db()
        assert result == {"manufacturer_id": manufacturer.pk, "prestashop_id": 77}
        assert manufacturer.prestashop_id == 77
        assert manufacturer.sync_required is False
        assert manufacturer.last_sync_error == ""
        client.create_manufacturer.assert_called_once_with("ARTECREATION")

    def test_export_reuses_existing_prestashop_manufacturer(self):
        manufacturer = Manufacturer.objects.create(icg_code="15000", name="BRAND X")
        client = Mock()
        client.find_manufacturer_id_by_name.return_value = 12

        export_manufacturer(manufacturer.pk, client=client)

        manufacturer.refresh_from_db()
        assert manufacturer.prestashop_id == 12
        assert manufacturer.sync_required is False
        client.create_manufacturer.assert_not_called()

    def test_export_updates_already_mapped_manufacturer(self):
        manufacturer = Manufacturer.objects.create(
            icg_code="16000",
            name="Updated Brand",
            prestashop_id=34,
            sync_required=True,
        )
        client = Mock()

        export_manufacturer(manufacturer.pk, client=client)

        manufacturer.refresh_from_db()
        client.update_manufacturer.assert_called_once_with(34, "Updated Brand")
        assert manufacturer.sync_required is False

    def test_export_stores_structured_error(self):
        manufacturer = Manufacturer.objects.create(icg_code="17000", name="Broken Brand")
        client = Mock()
        client.find_manufacturer_id_by_name.side_effect = PrestashopError(
            "Prestashop returned HTTP 500 for manufacturers.",
            status_code=500,
            body="<errors />",
        )

        with pytest.raises(PrestashopError):
            export_manufacturer(manufacturer.pk, client=client)

        manufacturer.refresh_from_db()
        payload = json.loads(manufacturer.last_sync_error)
        assert payload["status_code"] == 500
        assert manufacturer.sync_required is True


@pytest.mark.django_db
class TestManufacturerExportTask:
    def test_task_exports_pending_manufacturers_and_tracks_jobs(self, monkeypatch):
        first = Manufacturer.objects.create(icg_code="14000", name="ARTECREATION")
        second = Manufacturer.objects.create(icg_code="15000", name="BRAND X")

        def fake_export(manufacturer_id: int):
            manufacturer = Manufacturer.objects.get(pk=manufacturer_id)
            manufacturer.prestashop_id = manufacturer.pk + 100
            manufacturer.sync_required = False
            manufacturer.last_sync_error = ""
            manufacturer.last_synced_at = manufacturer.updated_at
            manufacturer.save()
            return {"manufacturer_id": manufacturer_id, "prestashop_id": manufacturer.prestashop_id}

        monkeypatch.setattr("apps.sync.tasks.export_manufacturer", fake_export)

        result = export_manufacturers()

        assert result == {"status": "success", "processed": 2, "failed": 0}
        assert SyncJob.objects.filter(job_type=SyncJobType.EXPORT_MANUFACTURER).count() == 2
        assert SyncJob.objects.filter(status=SyncJobStatus.SUCCEEDED).count() == 2
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.sync_required is False
        assert second.sync_required is False

    def test_task_marks_job_failed_when_export_raises(self, monkeypatch):
        manufacturer = Manufacturer.objects.create(icg_code="999", name="Failing Brand")

        def fake_export(manufacturer_id: int):
            raise PrestashopError("boom", status_code=503)

        monkeypatch.setattr("apps.sync.tasks.export_manufacturer", fake_export)

        result = export_manufacturers()

        manufacturer.refresh_from_db()
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_MANUFACTURER)
        assert result == {"status": "success", "processed": 0, "failed": 1}
        assert job.status == SyncJobStatus.FAILED
        assert json.loads(manufacturer.last_sync_error)["status_code"] == 503
