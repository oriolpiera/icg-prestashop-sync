import os

from config.sentry import init_sentry

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

init_sentry()

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()
