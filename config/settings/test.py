from .base import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

MARIADB = {
    "HOST": "test-mariadb",
    "PORT": 3306,
    "USER": "test_user",
    "PASSWORD": "test_password",
    "DATABASE": "test_prestashop",
}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

CELERY_TASK_ALWAYS_EAGER = True
