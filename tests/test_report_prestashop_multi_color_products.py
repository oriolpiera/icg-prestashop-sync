import csv
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command


@pytest.mark.django_db
class TestReportPrestashopMultiColorProducts:
    def test_reports_products_with_min_combinations_and_min_color_groups(self, tmp_path: Path):
        output_path = tmp_path / "multi-color-products.csv"

        mock_mariadb_output = (
            "prestashop_product_id\treference\tproduct_name\t"
            "combination_count\tcolor_group_count\tcolor_groups\n"
            "22\tREF001\tProduct REF001\t52\t2\tREF001_color|legacy_color\n"
            "23\tREF002\tProduct REF002\t55\t3\tref2_color|legacy_color|another_color\n"
        )

        with patch(
            "apps.sync.management.commands.report_prestashop_multi_color_products.subprocess.run"
        ) as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = mock_mariadb_output
            mock_run.return_value.stderr = ""

            out = StringIO()
            call_command(
                "report_prestashop_multi_color_products",
                "--min-combinations=50",
                "--min-color-groups=2",
                f"--output={output_path}",
                stdout=out,
            )

        assert output_path.exists()
        content = output_path.read_text()
        assert "prestashop_product_id" in content
        assert "REF001" in content
        assert "REF002" in content
        assert "test_reports_products_with_min_combinations_and_min_color_groups" not in content

    def test_writes_csv_with_correct_headers(self, tmp_path: Path):
        output_path = tmp_path / "output.csv"

        mock_mariadb_output = (
            "prestashop_product_id\treference\tproduct_name\t"
            "combination_count\tcolor_group_count\tcolor_groups\n"
            "1\tref1\tName1\t100\t2\tgrp1|grp2\n"
        )

        with patch(
            "apps.sync.management.commands.report_prestashop_multi_color_products.subprocess.run"
        ) as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = mock_mariadb_output
            mock_run.return_value.stderr = ""

            call_command(
                "report_prestashop_multi_color_products",
                f"--output={output_path}",
            )

        with open(output_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 1
        assert rows[0]["prestashop_product_id"] == "1"
        assert rows[0]["reference"] == "ref1"
        assert rows[0]["combination_count"] == "100"
        assert rows[0]["color_groups"] == "grp1|grp2"

    def test_prints_to_stdout_when_no_output_path(self):
        mock_mariadb_output = (
            "prestashop_product_id\treference\tproduct_name\t"
            "combination_count\tcolor_group_count\tcolor_groups\n"
            "1\tref1\tName1\t100\t2\tgrp1|grp2\n"
        )

        with patch(
            "apps.sync.management.commands.report_prestashop_multi_color_products.subprocess.run"
        ) as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = mock_mariadb_output
            mock_run.return_value.stderr = ""

            out = StringIO()
            call_command(
                "report_prestashop_multi_color_products",
                stdout=out,
            )

        output = out.getvalue()
        assert "Found 1 products" in output
        assert "REF001" not in output
        assert "ref1" in output

    def test_handles_empty_result(self):
        mock_mariadb_output = (
            "prestashop_product_id\treference\tproduct_name\t"
            "combination_count\tcolor_group_count\tcolor_groups\n"
        )

        with patch(
            "apps.sync.management.commands.report_prestashop_multi_color_products.subprocess.run"
        ) as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = mock_mariadb_output
            mock_run.return_value.stderr = ""

            out = StringIO()
            call_command(
                "report_prestashop_multi_color_products",
                stdout=out,
            )

        assert "No matching products found" in out.getvalue()

    def test_error_case_does_not_crash(self):
        with patch(
            "apps.sync.management.commands.report_prestashop_multi_color_products.subprocess.run"
        ) as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "Connection refused"

            out = StringIO()
            call_command(
                "report_prestashop_multi_color_products",
                stdout=out,
            )

        assert "ref1" not in out.getvalue()
