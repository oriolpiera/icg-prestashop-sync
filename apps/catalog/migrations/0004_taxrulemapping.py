from decimal import Decimal

from django.db import migrations, models


def seed_tax_rule_mappings(apps, schema_editor):
    TaxRuleMapping = apps.get_model("catalog", "TaxRuleMapping")
    TaxRuleMapping.objects.bulk_create(
        [
            TaxRuleMapping(
                vat_rate=Decimal("4.00"),
                prestashop_tax_rules_group_id=3,
                label="IVA 4%",
            ),
            TaxRuleMapping(
                vat_rate=Decimal("10.00"),
                prestashop_tax_rules_group_id=2,
                label="IVA 10%",
            ),
            TaxRuleMapping(
                vat_rate=Decimal("21.00"),
                prestashop_tax_rules_group_id=1,
                label="IVA 21%",
            ),
        ],
        ignore_conflicts=True,
    )


def remove_tax_rule_mappings(apps, schema_editor):
    TaxRuleMapping = apps.get_model("catalog", "TaxRuleMapping")
    TaxRuleMapping.objects.filter(
        vat_rate__in=[Decimal("4.00"), Decimal("10.00"), Decimal("21.00")]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_attributegroup_attributevalue"),
    ]

    operations = [
        migrations.CreateModel(
            name="TaxRuleMapping",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "vat_rate",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="ICG VAT rate percentage (e.g. 21 for 21%)",
                        max_digits=5,
                        unique=True,
                    ),
                ),
                (
                    "prestashop_tax_rules_group_id",
                    models.PositiveIntegerField(
                        help_text="PrestaShop tax_rules_group ID that corresponds to this VAT rate",
                    ),
                ),
                ("label", models.CharField(blank=True, max_length=128)),
            ],
            options={
                "ordering": ["vat_rate"],
            },
        ),
        migrations.RunPython(seed_tax_rule_mappings, remove_tax_rule_mappings),
    ]
