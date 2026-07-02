from apps.catalog.variants import (
    effective_prestashop_variant_axes,
    normalize_variant_axis,
    variant_axis_candidates,
)


def test_normalize_variant_axis_treats_asterisk_placeholder_as_empty():
    assert normalize_variant_axis("***") == ""
    assert normalize_variant_axis(" *** ") == ""
    assert normalize_variant_axis(".") == ""
    assert normalize_variant_axis("B00") == "B00"


def test_variant_axis_candidates_include_placeholder_for_blank_axis():
    assert variant_axis_candidates("") == ["", "***", "."]
    assert variant_axis_candidates("***") == ["", "***", "."]
    assert variant_axis_candidates("B00") == ["B00"]


def test_effective_prestashop_variant_axes_preserves_double_placeholder_only():
    assert effective_prestashop_variant_axes("***", "***") == ("***", "***")
    assert effective_prestashop_variant_axes(".", ".") == (".", ".")
    assert effective_prestashop_variant_axes("***", "B00") == ("", "B00")
    assert effective_prestashop_variant_axes(".", "B00") == ("", "B00")
