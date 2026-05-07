from dataclasses import dataclass
from typing import Any, Protocol, cast

from django.conf import settings


class PrestashopSettings(Protocol):
    PRESTASHOP_BASE_URL: str
    PRESTASHOP_API_KEY: str


@dataclass(slots=True)
class PrestashopCredentials:
    base_url: str
    api_key: str


class PrestashopClient:
    def credentials(self) -> PrestashopCredentials:
        typed_settings = cast(PrestashopSettings, settings)
        return PrestashopCredentials(
            base_url=typed_settings.PRESTASHOP_BASE_URL,
            api_key=typed_settings.PRESTASHOP_API_KEY,
        )

    def upsert_product(self, product: Any) -> None:
        raise NotImplementedError("Prestashop product sync is not implemented yet.")

    def upsert_price(self, price: Any) -> None:
        raise NotImplementedError("Prestashop price sync is not implemented yet.")

    def upsert_stock(self, stock: Any) -> None:
        raise NotImplementedError("Prestashop stock sync is not implemented yet.")
