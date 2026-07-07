import csv
import io
import os
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

MARIADB_QUERY = """
SELECT
    p.id_product AS prestashop_product_id,
    p.reference,
    pl.name AS product_name,
    COUNT(DISTINCT pa.id_product_attribute) AS combination_count,
    COUNT(DISTINCT CASE
        WHEN agl.name REGEXP '(_|^)color(s|es)?$'
        THEN agl.id_attribute_group
    END) AS color_group_count,
    GROUP_CONCAT(DISTINCT CASE
        WHEN agl.name REGEXP '(_|^)color(s|es)?$'
        THEN agl.name
    END SEPARATOR '|') AS color_groups
FROM ps_product p
JOIN ps_product_lang pl ON p.id_product = pl.id_product AND pl.id_lang = %(lang_id)s
JOIN ps_product_attribute pa ON p.id_product = pa.id_product
JOIN ps_product_attribute_combination pac
    ON pa.id_product_attribute = pac.id_product_attribute
JOIN ps_attribute a ON pac.id_attribute = a.id_attribute
JOIN ps_attribute_group_lang agl
    ON a.id_attribute_group = agl.id_attribute_group AND agl.id_lang = %(lang_id)s
WHERE p.reference IS NOT NULL AND p.reference != ''
GROUP BY p.id_product, p.reference, pl.name
HAVING combination_count >= %(min_combinations)s
   AND color_group_count >= %(min_color_groups)s
ORDER BY combination_count DESC, color_group_count DESC, p.id_product
LIMIT %(limit)s;
"""


class Command(BaseCommand):
    help = (
        "Query Prestashop MariaDB directly for products with many combinations "
        "and multiple color attribute groups. Exports CSV or prints tabular output. "
        "Run from the VPS host where Docker is available."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-combinations",
            type=int,
            default=50,
            help="Minimum number of combinations required to include a product (default: 50).",
        )
        parser.add_argument(
            "--min-color-groups",
            type=int,
            default=2,
            help=(
                "Minimum number of distinct color groups required "
                "to include a product (default: 2)."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum number of rows to return (default: 100).",
        )
        parser.add_argument(
            "--output",
            type=Path,
            help="Path to write CSV output. If not provided, prints to stdout.",
        )
        parser.add_argument(
            "--container",
            default=None,
            help="MariaDB container name (default: from MARIADB_HOST setting).",
        )

    def handle(self, *args, **options):
        min_combinations = options["min_combinations"]
        min_color_groups = options["min_color_groups"]
        limit = options["limit"]
        output_path = options["output"]
        container = options["container"]

        mariadb_cfg = getattr(settings, "MARIADB", None)
        if not mariadb_cfg:
            self.stderr.write(
                self.style.ERROR(
                    "MARIADB configuration not found in Django settings. "
                    "This command requires MariaDB credentials."
                )
            )
            return

        container_name = container or mariadb_cfg.get("HOST", "prod-mariadb")
        lang_id = getattr(settings, "PRESTASHOP_DEFAULT_LANGUAGE_ID", 1)

        query = MARIADB_QUERY % {
            "min_combinations": min_combinations,
            "min_color_groups": min_color_groups,
            "limit": limit,
            "lang_id": lang_id,
        }

        env = os.environ.copy()
        env["MYSQL_PWD"] = mariadb_cfg["PASSWORD"]

        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container_name,
                "mariadb",
                "-u",
                mariadb_cfg["USER"],
                mariadb_cfg["DATABASE"],
                "-B",
                "-e",
                query,
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        if result.returncode != 0:
            self.stderr.write(self.style.ERROR(f"MariaDB query failed: {result.stderr}"))
            return

        output = result.stdout
        reader = csv.DictReader(io.StringIO(output), delimiter="\t")
        rows = list(reader)

        if not rows:
            self.stdout.write(self.style.WARNING("No matching products found."))
            return

        fieldnames = [
            "prestashop_product_id",
            "reference",
            "product_name",
            "combination_count",
            "color_group_count",
            "color_groups",
        ]

        if output_path:
            with open(output_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row.get(k, "") for k in fieldnames})
            self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows)} rows to {output_path}"))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Found {len(rows)} products "
                    f"(min_combinations>={min_combinations}, min_color_groups>={min_color_groups})"
                )
            )
            for row in rows:
                self.stdout.write(
                    f"#{row['prestashop_product_id']} {row['reference']} | "
                    f"combinations={row['combination_count']} | "
                    f"color_groups={row['color_group_count']} | "
                    f"{row['color_groups']}"
                )
