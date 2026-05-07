from dataclasses import dataclass
from typing import Protocol, cast

from django.conf import settings


class ICGSettings(Protocol):
    ICG_MSSQL_SERVER: str
    ICG_MSSQL_DATABASE: str
    ICG_MSSQL_USER: str
    ICG_MSSQL_PASSWORD: str
    ICG_MSSQL_DRIVER: str


@dataclass(slots=True)
class ICGConnectionSettings:
    server: str
    database: str
    user: str
    password: str
    driver: str


class ICGCatalogReader:
    def connection_settings(self) -> ICGConnectionSettings:
        typed_settings = cast(ICGSettings, settings)
        return ICGConnectionSettings(
            server=typed_settings.ICG_MSSQL_SERVER,
            database=typed_settings.ICG_MSSQL_DATABASE,
            user=typed_settings.ICG_MSSQL_USER,
            password=typed_settings.ICG_MSSQL_PASSWORD,
            driver=typed_settings.ICG_MSSQL_DRIVER,
        )

    def fetch_products(self) -> list[dict[str, object]]:
        raise NotImplementedError("ICG product import is not implemented yet.")

    def fetch_prices(self) -> list[dict[str, object]]:
        raise NotImplementedError("ICG price import is not implemented yet.")

    def fetch_stock(self) -> list[dict[str, object]]:
        raise NotImplementedError("ICG stock import is not implemented yet.")
