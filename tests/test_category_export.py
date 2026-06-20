import json
from unittest.mock import Mock

import pytest

from apps.catalog.models import Category, CategoryType, Product
from apps.prestashop.client import PrestashopClient, PrestashopError
from apps.prestashop.services import (
    export_category,
    resolve_default_category,
    resolve_hidden_category,
    resolve_product_categories,
)
from apps.sync.models import SyncJob, SyncJobType
from apps.sync.tasks import export_categories


def _make_category(**overrides):
    return Category.objects.create(
        prestashop_id=overrides.pop("prestashop_id", 100),
        name=overrides.pop("name", "Test Category"),
        parent=overrides.pop("parent", None),
        position=overrides.pop("position", 0),
        active=overrides.pop("active", True),
        category_type=overrides.pop("category_type", CategoryType.NORMAL),
    )


def _response(payload: str, status_code: int = 200):
    response = Mock()
    response.status_code = status_code
    response.text = payload
    return response


@pytest.fixture(autouse=True)
def _clean_db():
    SyncJob.objects.all().delete()
    Product.objects.all().delete()
    Category.objects.all().delete()


@pytest.mark.django_db
class TestCategoryModel:
    def test_category_str(self):
        cat = _make_category(prestashop_id=42, name="Shoes")
        assert str(cat) == "Shoes (PS #42)"

    def test_category_type_choices(self):
        assert CategoryType.DEFAULT == "default"
        assert CategoryType.HIDDEN == "hidden"
        assert CategoryType.NORMAL == "normal"

    def test_category_parent_self_fk(self):
        parent = _make_category(prestashop_id=10, name="Root")
        child = _make_category(prestashop_id=11, name="Child", parent=parent)
        assert child.parent == parent
        assert parent.children.count() == 1


@pytest.mark.django_db
class TestResolveHelpers:
    def test_resolve_default_category_returns_default(self):
        default = _make_category(
            prestashop_id=251, name="Default", category_type=CategoryType.DEFAULT
        )
        result = resolve_default_category()
        assert result == default

    def test_resolve_default_category_raises_when_missing(self):
        with pytest.raises(PrestashopError, match="No default category configured"):
            resolve_default_category()

    def test_resolve_hidden_category_returns_hidden(self):
        hidden = _make_category(prestashop_id=526, name="Hidden", category_type=CategoryType.HIDDEN)
        result = resolve_hidden_category()
        assert result == hidden

    def test_resolve_hidden_category_returns_none_when_missing(self):
        assert resolve_hidden_category() is None


@pytest.mark.django_db
class TestResolveProductCategories:
    def test_uses_product_default_category(self):
        default = _make_category(prestashop_id=251, category_type=CategoryType.DEFAULT)
        product = Product.objects.create(
            icg_id=1001,
            reference="REF001",
            name="Product",
            category_default=default,
        )
        resolved_default, ids = resolve_product_categories(product)
        assert resolved_default == default
        assert 251 in ids

    def test_falls_back_to_global_default(self):
        default = _make_category(prestashop_id=251, category_type=CategoryType.DEFAULT)
        product = Product.objects.create(
            icg_id=1001,
            reference="REF001",
            name="Product",
        )
        resolved_default, ids = resolve_product_categories(product)
        assert resolved_default == default
        assert 251 in ids

    def test_includes_additional_categories(self):
        default = _make_category(prestashop_id=251, category_type=CategoryType.DEFAULT)
        extra = _make_category(prestashop_id=300, name="Extra")
        product = Product.objects.create(
            icg_id=1001,
            reference="REF001",
            name="Product",
            category_default=default,
        )
        product.categories.add(extra)
        resolved_default, ids = resolve_product_categories(product)
        assert resolved_default == default
        assert set(ids) == {251, 300}

    def test_default_category_always_in_list(self):
        default = _make_category(prestashop_id=251, category_type=CategoryType.DEFAULT)
        product = Product.objects.create(
            icg_id=1001,
            reference="REF001",
            name="Product",
            category_default=default,
        )
        product.categories.add(default)
        _, ids = resolve_product_categories(product)
        assert ids.count(251) == 1


@pytest.mark.django_db
class TestCategoryExport:
    def test_export_creates_category(self):
        cat = _make_category(prestashop_id=0, name="New Cat")
        client = Mock()
        client.find_category_id_by_name.return_value = None
        client.create_category.return_value = 55

        result = export_category(cat.pk, client=client)

        cat.refresh_from_db()
        assert result == {"category_id": cat.pk, "prestashop_id": 55}
        assert cat.prestashop_id == 55

    def test_export_updates_existing_category(self):
        cat = _make_category(prestashop_id=55, name="Existing")
        client = Mock()
        client.find_category_id_by_name.return_value = 55

        result = export_category(cat.pk, client=client)

        assert result == {"category_id": cat.pk, "prestashop_id": 55}
        client.update_category.assert_called_once_with(55, "Existing", active=True)
        client.create_category.assert_not_called()

    def test_export_stores_error_on_failure(self):
        cat = _make_category(prestashop_id=0, name="Fail Cat")
        client = Mock()
        client.find_category_id_by_name.side_effect = PrestashopError("API error", status_code=500)

        with pytest.raises(PrestashopError):
            export_category(cat.pk, client=client)

        cat.refresh_from_db()
        payload = json.loads(cat.last_sync_error)
        assert payload["status_code"] == 500


@pytest.mark.django_db
class TestCategoryExportTask:
    def test_task_exports_active_categories(self, monkeypatch):
        _make_category(prestashop_id=10, name="Cat A")
        _make_category(prestashop_id=20, name="Cat B")
        _make_category(prestashop_id=30, name="Inactive", active=False)

        def fake_export(category_id: int):
            return {"category_id": category_id, "prestashop_id": 99}

        monkeypatch.setattr("apps.sync.tasks.export_category", fake_export)

        result = export_categories()

        assert result == {"status": "success", "processed": 2, "failed": 0}
        assert SyncJob.objects.filter(job_type=SyncJobType.EXPORT_CATEGORY).count() == 2


@pytest.mark.django_db
class TestPrestashopClientCategoryExport:
    def test_find_category_uses_exact_match_filter(self, settings):
        response = _response(
            "<prestashop><categories><category id='15' /></categories></prestashop>"
        )
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"

        client = PrestashopClient(session=session)

        category_id = client.find_category_id_by_name("Shoes")

        assert category_id == 15
        assert session.request.call_args.kwargs["params"]["filter[name]"] == "[Shoes]"

    def test_create_category_sends_correct_payload(self, settings):
        response = _response("<prestashop><category><id>88</id></category></prestashop>")
        session = Mock()
        session.request.return_value = response
        settings.PRESTASHOP_BASE_URL = "https://shop.example.com"
        settings.PRESTASHOP_API_KEY = "secret"
        settings.PRESTASHOP_DEFAULT_LANGUAGE_ID = 1

        client = PrestashopClient(session=session)

        category_id = client.create_category("New Category", parent_id=2, active=True)

        assert category_id == 88
        payload = session.request.call_args.kwargs["data"]
        assert "<id_parent>2</id_parent>" in payload
        assert "<active>1</active>" in payload
