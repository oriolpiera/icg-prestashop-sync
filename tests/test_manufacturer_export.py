import json
from unittest.mock import Mock

import pytest

from apps.catalog.models import Manufacturer
from apps.prestashop.client import PrestashopClient, PrestashopError
from apps.prestashop.services import export_manufacturer, format_sync_error
from apps.sync.models import SyncJob, SyncJobStatus, SyncJobType
from apps.sync.tasks import export_manufacturers


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    Manufacturer.objects.all().delete()


@pytest.mark.django_db
class TestManufacturerExport:
    def test_format_sync_error_omits_null_status_code(self):
        payload = json.loads(format_sync_error(PrestashopError("connection dropped")))

        assert payload == {"message": "connection dropped"}

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

    def test_export_reclaims_stale_mapping_from_other_manufacturer(self):
        stale = Manufacturer.objects.create(icg_code="27500", name="CKREUL", prestashop_id=334)
        manufacturer = Manufacturer.objects.create(icg_code="95500", name="TULIP")
        client = Mock()
        client.find_manufacturer_id_by_name.return_value = 334

        result = export_manufacturer(manufacturer.pk, client=client)

        stale.refresh_from_db()
        manufacturer.refresh_from_db()
        assert result == {"manufacturer_id": manufacturer.pk, "prestashop_id": 334}
        assert stale.prestashop_id is None
        assert stale.sync_required is True
        assert manufacturer.prestashop_id == 334
        client.create_manufacturer.assert_not_called()

    def test_export_resets_stale_existing_mapping_before_update(self):
        stale = Manufacturer.objects.create(icg_code="27500", name="CKREUL", prestashop_id=334)
        Manufacturer.objects.create(icg_code="95500", name="TULIP")
        client = Mock()
        client.get_manufacturer_name.return_value = "TULIP"
        client.find_manufacturer_id_by_name.return_value = None
        client.create_manufacturer.return_value = 999

        result = export_manufacturer(stale.pk, client=client)

        stale.refresh_from_db()
        assert result == {"manufacturer_id": stale.pk, "prestashop_id": 999}
        assert stale.prestashop_id == 999
        client.update_manufacturer.assert_not_called()
        client.create_manufacturer.assert_called_once_with("CKREUL")

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

    def test_export_recovers_when_manufacturer_deleted_from_prestashop(self):
        manufacturer = Manufacturer.objects.create(
            icg_code="18000", name="Deleted Brand", prestashop_id=44
        )
        client = Mock()
        client.update_manufacturer.side_effect = PrestashopError(
            "Prestashop returned HTTP 404 for manufacturers.",
            status_code=404,
        )
        client.find_manufacturer_id_by_name.return_value = 55

        result = export_manufacturer(manufacturer.pk, client=client)

        manufacturer.refresh_from_db()
        assert result == {"manufacturer_id": manufacturer.pk, "prestashop_id": 55}
        assert manufacturer.prestashop_id == 55
        assert manufacturer.sync_required is False
        client.update_manufacturer.assert_called_once_with(44, "Deleted Brand")
        client.find_manufacturer_id_by_name.assert_called_once_with("Deleted Brand")

    def test_export_does_not_recover_manufacturer_on_non_404_error(self):
        manufacturer = Manufacturer.objects.create(
            icg_code="19000", name="Failing Brand", prestashop_id=44
        )
        client = Mock()
        client.update_manufacturer.side_effect = PrestashopError(
            "Prestashop returned HTTP 500 for manufacturers.",
            status_code=500,
        )

        with pytest.raises(PrestashopError):
            export_manufacturer(manufacturer.pk, client=client)

        manufacturer.refresh_from_db()
        assert manufacturer.prestashop_id == 44
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
            manufacturer = Manufacturer.objects.get(pk=manufacturer_id)
            manufacturer.last_sync_error = format_sync_error(
                PrestashopError("boom", status_code=503)
            )
            manufacturer.save(update_fields=["last_sync_error", "updated_at"])
            raise PrestashopError("boom", status_code=503)

        monkeypatch.setattr("apps.sync.tasks.export_manufacturer", fake_export)

        result = export_manufacturers()

        manufacturer.refresh_from_db()
        job = SyncJob.objects.get(job_type=SyncJobType.EXPORT_MANUFACTURER)
        assert result == {"status": "success", "processed": 0, "failed": 1}
        assert job.status == SyncJobStatus.PENDING
        assert job.attempts == 2
        assert json.loads(manufacturer.last_sync_error)["status_code"] == 503


@pytest.mark.django_db
class TestPrestashopClient:
    def test_find_manufacturer_uses_exact_match_filter(self, settings):
        response = Mock(
            status_code=200,
            text="<prestashop><manufacturers><manufacturer id='12' /></manufacturers></prestashop>",
        )
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)

        manufacturer_id = client.find_manufacturer_id_by_name("Nike")

        assert manufacturer_id == 12
        assert session.request.call_args.kwargs["params"] == {
            "filter[name]": "[Nike]",
            "limit": "1",
        }

    def test_find_manufacturer_rejects_reserved_filter_characters(self, settings):
        session = Mock()
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)

        with pytest.raises(PrestashopError, match="Unsupported manufacturer name characters"):
            client.find_manufacturer_id_by_name("Brand|Name")

        session.request.assert_not_called()

    def test_update_manufacturer_removes_link_rewrite(self, settings):
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        response_get = Mock(
            status_code=200,
            text=(
                "<prestashop><manufacturer><id>12</id><name>Old</name>"
                "<link_rewrite>old</link_rewrite></manufacturer></prestashop>"
            ),
        )
        response_put = Mock(status_code=200, text="<prestashop />")
        session = Mock()
        session.request.side_effect = [response_get, response_put]

        client = PrestashopClient(session=session)
        client.update_manufacturer(12, "New")

        put_payload = session.request.call_args_list[1].kwargs["data"]
        assert "<link_rewrite>" not in put_payload
        assert "<name>New</name>" in put_payload
