from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0009_add_prestashop_specific_price_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="discount_sync_required",
            field=models.BooleanField(
                default=False,
                help_text="Whether the discount needs to be re-exported to Prestashop.",
            ),
        ),
    ]
