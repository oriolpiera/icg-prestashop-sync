from dataclasses import dataclass

from django.conf import settings


@dataclass(slots=True)
class ICGConnectionSettings:
    server: str
    database: str
    user: str
    password: str
    driver: str


class ICGCatalogReader:
    def connection_settings(self) -> ICGConnectionSettings:
        return ICGConnectionSettings(
            server=settings.ICG_MSSQL_SERVER,
            database=settings.ICG_MSSQL_DATABASE,
            user=settings.ICG_MSSQL_USER,
            password=settings.ICG_MSSQL_PASSWORD,
            driver=settings.ICG_MSSQL_DRIVER,
        )

    def fetch_products(self):
        raise NotImplementedError("ICG product import is not implemented yet.")

    def fetch_prices(self):
        raise NotImplementedError("ICG price import is not implemented yet.")

    def fetch_stock(self):
        raise NotImplementedError("ICG stock import is not implemented yet.")
