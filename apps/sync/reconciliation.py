from collections import defaultdict
from dataclasses import dataclass

from apps.catalog.models import Product
from apps.prestashop.client import PrestashopProductSummary


@dataclass(slots=True)
class ProductMatch:
    reference: str
    prestashop_product_id: int
    django_product_ids: list[int]
    status: str


def classify_product_matches(
    prestashop_products: list[PrestashopProductSummary],
    django_products: list[Product],
) -> list[ProductMatch]:
    django_by_reference: dict[str, list[Product]] = defaultdict(list)
    for product in django_products:
        django_by_reference[product.reference].append(product)

    prestashop_by_reference: dict[str, list[PrestashopProductSummary]] = defaultdict(list)
    for product in prestashop_products:
        prestashop_by_reference[product.reference].append(product)

    matches: list[ProductMatch] = []
    for ps_product in prestashop_products:
        django_for_reference = django_by_reference.get(ps_product.reference, [])
        prestashop_for_reference = prestashop_by_reference.get(ps_product.reference, [])

        if len(prestashop_for_reference) > 1 or len(django_for_reference) > 1:
            status = "ambiguous"
        elif len(django_for_reference) == 1:
            status = "safe"
        else:
            status = "missing"

        matches.append(
            ProductMatch(
                reference=ps_product.reference,
                prestashop_product_id=ps_product.product_id,
                django_product_ids=[product.pk for product in django_for_reference],
                status=status,
            )
        )

    return matches
