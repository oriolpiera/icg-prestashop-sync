from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.models import SyncTrackedModel, TimeStampedModel


class CategoryType(models.TextChoices):
    DEFAULT = "default", "Default"
    HIDDEN = "hidden", "Hidden"
    NORMAL = "normal", "Normal"


class Category(SyncTrackedModel):
    prestashop_id = models.PositiveIntegerField(unique=True, null=True, blank=True)
    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    position = models.IntegerField(default=0)
    active = models.BooleanField(default=True)
    category_type = models.CharField(
        max_length=16,
        choices=CategoryType.choices,
        default=CategoryType.NORMAL,
        help_text=(
            "Determines how this category is used during product export. "
            "DEFAULT: assigned as default category for new products. "
            "HIDDEN: used for products not visible on the web. "
            "NORMAL: available for manual assignment."
        ),
    )

    class Meta:
        ordering = ["position", "name"]

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.category_type == CategoryType.DEFAULT:
            exists = (
                Category.objects.filter(category_type=CategoryType.DEFAULT)
                .exclude(pk=self.pk)
                .exists()
            )
            if exists:
                raise ValidationError("Only one category can have category_type='default'.")

    def __str__(self) -> str:
        return f"{self.name} (PS #{self.prestashop_id})"


class Manufacturer(SyncTrackedModel):
    icg_code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    prestashop_id = models.PositiveIntegerField(blank=True, null=True, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Product(SyncTrackedModel):
    icg_id = models.PositiveIntegerField(unique=True)
    reference = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    manufacturer = models.ForeignKey(
        Manufacturer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    category_default = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_products",
    )
    categories = models.ManyToManyField(
        Category,
        blank=True,
        related_name="products",
    )
    visible_web = models.BooleanField(default=True)
    discontinued = models.BooleanField(default=False)
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Product-level discount percentage (0 = no discount). "
        "Exported as a Prestashop specific_price with reduction_type='percentage'.",
    )
    prestashop_specific_price_id = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Prestashop specific_price ID for the product-level discount.",
    )
    discount_sync_required = models.BooleanField(
        default=False,
        help_text="Whether the discount needs to be re-exported to Prestashop.",
    )

    class Meta:
        ordering = ["reference"]

    def __str__(self) -> str:
        return f"{self.reference} - {self.name}"


class Combination(SyncTrackedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="combinations")
    icg_size = models.CharField(max_length=64, blank=True)
    icg_color = models.CharField(max_length=64, blank=True)
    ean13 = models.CharField(max_length=32, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["product__reference", "icg_size", "icg_color"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "icg_size", "icg_color"],
                name="catalog_unique_product_combination",
            )
        ]

    def __str__(self) -> str:
        label = " / ".join(filter(None, [self.icg_size, self.icg_color]))
        return f"{self.product.reference} - {label or 'default'}"


class Price(SyncTrackedModel):
    combination = models.OneToOneField(
        Combination,
        on_delete=models.CASCADE,
        related_name="price",
    )
    amount_ex_vat = models.DecimalField(max_digits=10, decimal_places=2)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=21)
    currency = models.CharField(max_length=3, default="EUR")

    class Meta:
        ordering = ["combination__product__reference"]

    def __str__(self) -> str:
        return f"{self.combination}: {self.amount_ex_vat} {self.currency}"


class Stock(SyncTrackedModel):
    combination = models.OneToOneField(
        Combination,
        on_delete=models.CASCADE,
        related_name="stock",
    )
    warehouse_code = models.CharField(max_length=32, blank=True)
    quantity = models.IntegerField(default=0)

    class Meta:
        ordering = ["combination__product__reference"]

    def __str__(self) -> str:
        return f"{self.combination}: {self.quantity}"


class AttributeGroup(TimeStampedModel):
    icg_type = models.CharField(max_length=32, unique=True, help_text="size or color")
    name = models.CharField(max_length=255)
    prestashop_id = models.PositiveIntegerField(unique=True)

    class Meta:
        ordering = ["icg_type"]

    def __str__(self) -> str:
        return f"{self.name} ({self.icg_type})"


class AttributeValue(TimeStampedModel):
    attribute_group = models.ForeignKey(
        AttributeGroup,
        on_delete=models.CASCADE,
        related_name="values",
    )
    icg_value = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    prestashop_id = models.PositiveIntegerField(unique=True)

    class Meta:
        ordering = ["attribute_group__icg_type", "icg_value"]
        constraints = [
            models.UniqueConstraint(
                fields=["attribute_group", "icg_value"],
                name="catalog_unique_group_value",
            )
        ]

    def __str__(self) -> str:
        return f"{self.attribute_group.icg_type}:{self.icg_value}"


class TaxRuleMapping(TimeStampedModel):
    vat_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        unique=True,
        help_text="ICG VAT rate percentage (e.g. 21 for 21%)",
    )
    prestashop_tax_rules_group_id = models.PositiveIntegerField(
        help_text="PrestaShop tax_rules_group ID that corresponds to this VAT rate",
    )
    label = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["vat_rate"]

    def __str__(self) -> str:
        return f"{self.vat_rate}% → PS group {self.prestashop_tax_rules_group_id}"


class PrestashopMapping(TimeStampedModel):
    product = models.OneToOneField(
        Product,
        on_delete=models.CASCADE,
        related_name="prestashop_mapping",
        null=True,
        blank=True,
    )
    combination = models.OneToOneField(
        Combination,
        on_delete=models.CASCADE,
        related_name="prestashop_mapping",
        null=True,
        blank=True,
    )
    prestashop_product_id = models.PositiveIntegerField(blank=True, null=True)
    prestashop_combination_id = models.PositiveIntegerField(blank=True, null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(product__isnull=False) | models.Q(combination__isnull=False)),
                name="catalog_mapping_has_target",
            )
        ]

    def __str__(self) -> str:
        if self.combination_id:
            return f"Combination mapping #{self.prestashop_combination_id or 'new'}"
        return f"Product mapping #{self.prestashop_product_id or 'new'}"
