from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0011_alter_product_discount_percent"),
    ]

    operations = [
        migrations.AlterField(
            model_name="product",
            name="reference",
            field=models.CharField(max_length=64),
        ),
    ]
