from django.db import migrations, models


def migrate_mapping_data(apps, schema_editor):
    try:
        PrestashopMapping = apps.get_model("catalog", "PrestashopMapping")
    except LookupError:
        return

    Product = apps.get_model("catalog", "Product")
    Combination = apps.get_model("catalog", "Combination")

    for mapping in PrestashopMapping.objects.select_related("product", "combination").all():
        if mapping.product_id and mapping.prestashop_product_id is not None:
            Product.objects.filter(pk=mapping.product_id).update(
                prestashop_id=mapping.prestashop_product_id
            )
        if mapping.combination_id and mapping.prestashop_combination_id is not None:
            Combination.objects.filter(pk=mapping.combination_id).update(
                prestashop_id=mapping.prestashop_combination_id
            )


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0012_alter_product_reference"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="prestashop_id",
            field=models.PositiveIntegerField(blank=True, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="combination",
            name="prestashop_id",
            field=models.PositiveIntegerField(blank=True, null=True, unique=True),
        ),
        migrations.RunPython(migrate_mapping_data, migrations.RunPython.noop),
        migrations.DeleteModel(name="PrestashopMapping"),
    ]
