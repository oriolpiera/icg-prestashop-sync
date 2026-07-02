from collections import defaultdict
from dataclasses import dataclass

from apps.catalog.models import Combination, Product
from apps.catalog.variants import (
    effective_prestashop_variant_axes,
    variant_axis_candidates,
)
from apps.prestashop.client import PrestashopCombinationSummary, PrestashopProductSummary


@dataclass(slots=True)
class ProductMatch:
    reference: str
    prestashop_product_id: int
    django_product_ids: list[int]
    status: str


@dataclass(slots=True)
class ResolvedPrestashopCombination:
    prestashop_combination_id: int
    prestashop_product_id: int
    resolved_size: str
    resolved_color: str
    unresolved_value_ids: list[int]
    resolved_values: list[dict[str, str | int]]


def find_candidate_django_combinations(
    product: Product,
    *,
    resolved_size: str,
    resolved_color: str,
) -> list[Combination]:
    candidates: dict[int, Combination] = {}

    resolved_size, resolved_color = effective_prestashop_variant_axes(
        resolved_size,
        resolved_color,
    )

    lookup_pairs: list[tuple[str, str]] = []
    if resolved_size and resolved_color:
        for size_candidate in variant_axis_candidates(resolved_size):
            for color_candidate in variant_axis_candidates(resolved_color):
                lookup_pairs.append((size_candidate, color_candidate))
    elif resolved_size:
        for size_candidate in variant_axis_candidates(resolved_size):
            for blank_candidate in variant_axis_candidates(""):
                lookup_pairs.extend(
                    [
                        (size_candidate, blank_candidate),
                        (blank_candidate, size_candidate),
                    ]
                )
    elif resolved_color:
        for color_candidate in variant_axis_candidates(resolved_color):
            for blank_candidate in variant_axis_candidates(""):
                lookup_pairs.extend(
                    [
                        (blank_candidate, color_candidate),
                        (color_candidate, blank_candidate),
                    ]
                )

    for icg_size, icg_color in lookup_pairs:
        for combination in Combination.objects.filter(
            product=product,
            icg_size=icg_size,
            icg_color=icg_color,
        ):
            candidates[combination.pk] = combination

    return list(candidates.values())


def group_role(group_name: str) -> str:
    lower = group_name.strip().lower()
    if lower in {"size", "sizes", "talla", "tallas"}:
        return "size"
    if lower in {"color", "colors", "colores"}:
        return "color"

    suffix = lower.rsplit("_", 1)[-1]
    if suffix in {"size", "sizes", "talla", "tallas"}:
        return "size"
    if suffix in {"color", "colors", "colores"}:
        return "color"
    return "unknown"


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


def resolve_prestashop_combination(
    ps_combination: PrestashopCombinationSummary,
    value_index: dict[int, dict[str, str | int]],
) -> ResolvedPrestashopCombination:
    resolved_values: list[dict[str, str | int]] = []
    resolved_size = ""
    resolved_color = ""
    unresolved_value_ids: list[int] = []

    for value_id in ps_combination.attribute_value_ids:
        value_data = value_index.get(value_id)
        if value_data is None:
            unresolved_value_ids.append(value_id)
            continue

        role = group_role(str(value_data["group_name"]))
        resolved_values.append(
            {
                "prestashop_value_id": value_id,
                "name": str(value_data["name"]),
                "group_prestashop_id": int(value_data["group_prestashop_id"]),
                "group_name": str(value_data["group_name"]),
                "role": role,
            }
        )

        if role == "size" and not resolved_size:
            resolved_size = str(value_data["name"]).strip()
        elif role == "color" and not resolved_color:
            resolved_color = str(value_data["name"]).strip()

    resolved_size, resolved_color = effective_prestashop_variant_axes(
        resolved_size,
        resolved_color,
    )

    return ResolvedPrestashopCombination(
        prestashop_combination_id=ps_combination.combination_id,
        prestashop_product_id=ps_combination.product_id,
        resolved_size=resolved_size,
        resolved_color=resolved_color,
        unresolved_value_ids=unresolved_value_ids,
        resolved_values=resolved_values,
    )
