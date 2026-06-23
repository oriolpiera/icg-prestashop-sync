import os

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration


def init_sentry() -> None:
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return

    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            DjangoIntegration(),
            LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR),
            CeleryIntegration(),
        ],
        send_default_pii=False,
        traces_sample_rate=0.1,
        profiles_sample_rate=0.1,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        release=os.getenv("SENTRY_RELEASE"),
    )
