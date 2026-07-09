from dataclasses import dataclass


@dataclass(slots=True)
class RemoteAttributeGroupMatch:
    prestashop_id: int
    name: str
    product_specific: bool


def attribute_group_role(group_name: str) -> str:
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


def preferred_color_group_names(product) -> list[str]:
    names: list[str] = []
    if getattr(product, "prestashop_id", None):
        names.append(f"{product.prestashop_id}_color")
    names.append(f"{product.reference}_color")
    return names


def expected_local_attribute_group_name(icg_type: str, product=None) -> str:
    if icg_type == "color":
        if product is None:
            raise ValueError("Color attribute groups require a product.")
        if getattr(product, "prestashop_id", None):
            return f"{product.prestashop_id}_color"
        return f"{product.reference}_color"
    return "Size"


def resolve_remote_attribute_group_match(
    remote_groups: list[dict[str, str | int]],
    *,
    icg_type: str,
    product=None,
) -> RemoteAttributeGroupMatch | None:
    if not isinstance(remote_groups, list):
        remote_groups = []

    if icg_type == "color":
        if product is None:
            return None
        for candidate_name in preferred_color_group_names(product):
            for remote_group in remote_groups:
                if remote_group.get("name") != candidate_name:
                    continue
                ps_id = remote_group.get("ps_id")
                if isinstance(ps_id, int):
                    return RemoteAttributeGroupMatch(ps_id, candidate_name, True)
        return None

    if icg_type != "size":
        return None

    if product is not None and getattr(product, "prestashop_id", None):
        preferred_names = (
            f"{product.prestashop_id}_talla",
            f"{product.prestashop_id}_size",
            f"{product.prestashop_id}_tallas",
            f"{product.prestashop_id}_sizes",
        )
        remote_by_name = {
            str(group.get("name") or ""): group.get("ps_id") for group in remote_groups
        }
        for candidate_name in preferred_names:
            ps_id = remote_by_name.get(candidate_name)
            if isinstance(ps_id, int):
                return RemoteAttributeGroupMatch(ps_id, candidate_name, True)

    for remote_group in remote_groups:
        group_name = str(remote_group.get("name") or "")
        ps_id = remote_group.get("ps_id")
        if group_name == "Size" and isinstance(ps_id, int):
            return RemoteAttributeGroupMatch(ps_id, group_name, False)

    return None
