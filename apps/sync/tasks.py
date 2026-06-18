import logging

from celery import shared_task

from apps.icg.importer import import_prices as run_import_prices
from apps.icg.importer import import_products as run_import_products
from apps.icg.importer import import_stock as run_import_stock

logger = logging.getLogger(__name__)


@shared_task
def import_products() -> dict:
    logger.info("Celery task: import_products")
    try:
        result = run_import_products()
        logger.info("import_products completed: %s", result)
        return result
    except Exception:
        logger.exception("import_products failed")
        return {"status": "error", "detail": "See worker logs for details."}


@shared_task
def import_prices() -> dict:
    logger.info("Celery task: import_prices")
    try:
        result = run_import_prices()
        logger.info("import_prices completed: %s", result)
        return result
    except Exception:
        logger.exception("import_prices failed")
        return {"status": "error", "detail": "See worker logs for details."}


@shared_task
def import_stock() -> dict:
    logger.info("Celery task: import_stock")
    try:
        result = run_import_stock()
        logger.info("import_stock completed: %s", result)
        return result
    except Exception:
        logger.exception("import_stock failed")
        return {"status": "error", "detail": "See worker logs for details."}
