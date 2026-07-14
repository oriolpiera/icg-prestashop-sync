import os

from .base import *  # noqa: F403

if os.getenv("DATABASE_HOST") == "postgres":
    raise RuntimeError(
        "config.settings.test refuses to start: DATABASE_HOST=postgres detected. "
        "This looks like the production VPS environment. "
        "Use config.settings.production for production or unset DATABASE_HOST for local testing."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

MARIADB = {
    "CONTAINER": "test-mariadb",
    "HOST": "localhost",
    "PORT": 3306,
    "USER": "test_user",
    "PASSWORD": "test_password",
    "DATABASE": "test_prestashop",
}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

CELERY_TASK_ALWAYS_EAGER = True
