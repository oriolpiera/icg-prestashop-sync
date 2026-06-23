import os

from config.sentry import init_sentry

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

init_sentry()

from celery import Celery  # noqa: E402

app = Celery("icg_prestashop_sync")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
