from dataclasses import dataclass

from django.conf import settings


@dataclass(slots=True)
class PrestashopCredentials:
    base_url: str
    api_key: str


class PrestashopClient:
    def credentials(self) -> PrestashopCredentials:
        return PrestashopCredentials(
            base_url=settings.PRESTASHOP_BASE_URL,
            api_key=settings.PRESTASHOP_API_KEY,
        )

    def upsert_product(self, product) -> None:
        raise NotImplementedError("Prestashop product sync is not implemented yet.")

    def upsert_price(self, price) -> None:
        raise NotImplementedError("Prestashop price sync is not implemented yet.")

    def upsert_stock(self, stock) -> None:
        raise NotImplementedError("Prestashop stock sync is not implemented yet.")
