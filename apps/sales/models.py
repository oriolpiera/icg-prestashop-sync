from django.db import models

from apps.core.models import TimeStampedModel


class ExportStatus(models.TextChoices):
    NEVER = "never", "Never exported"
    SUCCEEDED = "succeeded", "Exported"
    FAILED = "failed", "Failed"


class PrestashopCustomer(TimeStampedModel):
    prestashop_id = models.PositiveIntegerField(unique=True)
    firstname = models.CharField(max_length=255, blank=True)
    lastname = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    date_add = models.DateTimeField()
    address1 = models.CharField(max_length=255, blank=True)
    postcode = models.CharField(max_length=32, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    phone_mobile = models.CharField(max_length=32, blank=True)
    dni = models.CharField(max_length=32, blank=True)
    vat_number = models.CharField(max_length=32, blank=True)
    last_snapshot_at = models.DateTimeField()
    export_status = models.CharField(
        max_length=16,
        choices=ExportStatus.choices,
        default=ExportStatus.NEVER,
    )
    exported_to_icg_at = models.DateTimeField(blank=True, null=True)
    last_export_error = models.TextField(blank=True)
    last_export_inserted = models.BooleanField(blank=True, null=True)

    class Meta:
        ordering = ["-date_add", "-prestashop_id"]

    def __str__(self) -> str:
        full_name = " ".join(part for part in [self.firstname, self.lastname] if part).strip()
        return full_name or f"Customer #{self.prestashop_id}"


class PrestashopOrder(TimeStampedModel):
    prestashop_id = models.PositiveIntegerField(unique=True)
    customer = models.ForeignKey(
        PrestashopCustomer,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    payment = models.CharField(max_length=255, blank=True)
    date_add = models.DateTimeField()
    total_paid_tax_incl = models.DecimalField(max_digits=12, decimal_places=2)
    total_shipping_tax_incl = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_shipping_tax_excl = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    last_snapshot_at = models.DateTimeField()
    export_status = models.CharField(
        max_length=16,
        choices=ExportStatus.choices,
        default=ExportStatus.NEVER,
    )
    exported_to_icg_at = models.DateTimeField(blank=True, null=True)
    last_export_error = models.TextField(blank=True)
    inserted_rows = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-date_add", "-prestashop_id"]

    def __str__(self) -> str:
        return f"Order #{self.prestashop_id}"


class PrestashopOrderLine(models.Model):
    order = models.ForeignKey(
        PrestashopOrder,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    position = models.PositiveIntegerField()
    prestashop_product_id = models.PositiveIntegerField()
    prestashop_combination_id = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=255, blank=True)
    quantity = models.PositiveIntegerField(default=0)
    unit_price_tax_incl = models.DecimalField(max_digits=12, decimal_places=2)
    total_price_tax_incl = models.DecimalField(max_digits=12, decimal_places=2)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        ordering = ["order", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "position"],
                name="sales_unique_order_line_position",
            )
        ]

    def __str__(self) -> str:
        return f"{self.order} line {self.position}"


class PrestashopOrderDiscountLine(models.Model):
    order = models.ForeignKey(
        PrestashopOrder,
        on_delete=models.CASCADE,
        related_name="discounts",
    )
    position = models.PositiveIntegerField()
    description = models.CharField(max_length=255, blank=True)
    amount_tax_incl = models.DecimalField(max_digits=12, decimal_places=2)
    amount_tax_excl = models.DecimalField(max_digits=12, decimal_places=2)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        ordering = ["order", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "position"],
                name="sales_unique_order_discount_position",
            )
        ]

    def __str__(self) -> str:
        return f"{self.order} discount {self.position}"
