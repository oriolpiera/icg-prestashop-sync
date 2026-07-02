PLACEHOLDER_VARIANT_VALUES = frozenset({"***", "."})


def is_placeholder_variant_axis(value: str | None) -> bool:
    if value is None:
        return True
    return str(value).strip() in PLACEHOLDER_VARIANT_VALUES | {""}


def normalize_variant_axis(value: str | None) -> str:
    if value is None:
        return ""
    normalized = str(value).strip()
    if normalized in PLACEHOLDER_VARIANT_VALUES:
        return ""
    return normalized


def variant_axis_candidates(value: str | None) -> list[str]:
    normalized = normalize_variant_axis(value)
    candidates = [normalized]
    if normalized == "":
        candidates.extend(sorted(PLACEHOLDER_VARIANT_VALUES))
    return candidates


def effective_prestashop_variant_axes(size: str | None, color: str | None) -> tuple[str, str]:
    raw_size = "" if size is None else str(size).strip()
    raw_color = "" if color is None else str(color).strip()
    size_placeholder = is_placeholder_variant_axis(raw_size)
    color_placeholder = is_placeholder_variant_axis(raw_color)

    if size_placeholder and color_placeholder:
        return raw_size, raw_color

    return normalize_variant_axis(raw_size), normalize_variant_axis(raw_color)
