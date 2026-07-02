import csv
import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.catalog.models import Manufacturer, Product


@pytest.fixture(autouse=True)
def _clean_db():
    Product.objects.all().delete()
    Manufacturer.objects.all().delete()


def _make_product(reference: str, name: str, manufacturer_name: str = "Brand"):
    manufacturer = Manufacturer.objects.create(icg_code=f"{reference}-M", name=manufacturer_name)
    return Product.objects.create(
        icg_id=1000 + Product.objects.count(),
        reference=reference,
        name=name,
        manufacturer=manufacturer,
        visible_web=True,
        discontinued=False,
        prestashop_id=22 + Product.objects.count(),
    )


@pytest.mark.django_db
class TestReportProblemReferencesForShop:
    def test_builds_ordered_csv_with_name_and_counts(self, tmp_path: Path):
        _make_product("0170063", "Tela Polycotton")
        _make_product("0090837", "Copic Sketch")

        conflicts_path = tmp_path / "conflicts.json"
        conflicts_path.write_text(
            json.dumps(
                [
                    {"reference": "0170063"},
                    {"reference": "0170063"},
                    {"reference": "0090837"},
                ]
            ),
            encoding="utf-8",
        )
        unresolved_path = tmp_path / "unresolved.json"
        unresolved_path.write_text(
            json.dumps(
                {
                    "unresolved_combinations": [
                        {"reference": "0170063"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        missing_reference_path = tmp_path / "missing-reference.json"
        missing_reference_path.write_text(
            json.dumps(
                {
                    "references": [
                        {"reference": "0170063", "missing_count": 10},
                        {"reference": "0090837", "missing_count": 3},
                    ]
                }
            ),
            encoding="utf-8",
        )
        output_path = tmp_path / "shop-report.csv"

        out = StringIO()
        call_command(
            "report_problem_references_for_shop",
            "--conflicts-json",
            str(conflicts_path),
            "--unresolved-json",
            str(unresolved_path),
            "--missing-reference-json",
            str(missing_reference_path),
            "--output",
            str(output_path),
            stdout=out,
        )

        with output_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        assert rows[0]["reference"] == "0170063"
        assert rows[0]["name"] == "Tela Polycotton"
        assert rows[0]["total_problem_combinations"] == "13"
        assert rows[0]["conflict_count"] == "2"
        assert rows[0]["missing_count"] == "10"
        assert rows[0]["unresolved_count"] == "1"
        assert rows[1]["reference"] == "0090837"
        assert rows[1]["name"] == "Copic Sketch"
        assert "Wrote shop report" in out.getvalue()

    def test_requires_at_least_one_input_report(self, tmp_path: Path):
        with pytest.raises(CommandError, match="Provide at least one input report"):
            call_command(
                "report_problem_references_for_shop",
                "--output",
                str(tmp_path / "shop-report.csv"),
                stdout=StringIO(),
            )
