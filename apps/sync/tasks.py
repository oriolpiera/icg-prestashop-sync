from celery import shared_task


@shared_task
def import_products() -> dict[str, str]:
    return {"status": "pending", "detail": "ICG product import not implemented yet."}


@shared_task
def import_prices() -> dict[str, str]:
    return {"status": "pending", "detail": "ICG price import not implemented yet."}


@shared_task
def import_stock() -> dict[str, str]:
    return {"status": "pending", "detail": "ICG stock import not implemented yet."}
