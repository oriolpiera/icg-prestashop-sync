import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return default or []
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core",
    "apps.icg",
    "apps.catalog",
    "apps.sync",
    "apps.prestashop",
    "apps.operations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DATABASE_NAME", "icg_prestashop_sync"),
        "USER": os.getenv("DATABASE_USER", "postgres"),
        "PASSWORD": os.getenv("DATABASE_PASSWORD", "postgres"),
        "HOST": os.getenv("DATABASE_HOST", "localhost"),
        "PORT": os.getenv("DATABASE_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "ca"
TIME_ZONE = os.getenv("TIME_ZONE", "Europe/Madrid")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 240

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

PRESTASHOP_BASE_URL = os.getenv("PRESTASHOP_BASE_URL", "")
PRESTASHOP_API_KEY = os.getenv("PRESTASHOP_API_KEY", "")
PRESTASHOP_DEFAULT_LANGUAGE_ID = int(os.getenv("PRESTASHOP_DEFAULT_LANGUAGE_ID", "1"))
PRESTASHOP_DEFAULT_CATEGORY_ID = int(os.getenv("PRESTASHOP_DEFAULT_CATEGORY_ID", "2"))
PRESTASHOP_ROOT_CATEGORY_ID = int(os.getenv("PRESTASHOP_ROOT_CATEGORY_ID", "2"))
_tax_raw = int(os.getenv("PRESTASHOP_DEFAULT_TAX_RULES_GROUP_ID", "0"))
PRESTASHOP_DEFAULT_TAX_RULES_GROUP_ID = _tax_raw or None
PRESTASHOP_SYNC_TEXTURE_IMAGES = os.getenv("PRESTASHOP_SYNC_TEXTURE_IMAGES", "false").lower() in ("true", "1", "yes")  # noqa:E501

ICG_ODBC_CONNECTION_STRING = os.getenv("ICG_ODBC_CONNECTION_STRING", "")
ICG_MSSQL_SERVER = os.getenv("ICG_MSSQL_SERVER", "")
ICG_MSSQL_SERVERNAME = os.getenv("ICG_MSSQL_SERVERNAME", "")
ICG_MSSQL_DATABASE = os.getenv("ICG_MSSQL_DATABASE", "")
ICG_MSSQL_USER = os.getenv("ICG_MSSQL_USER", "")
ICG_MSSQL_PASSWORD = os.getenv("ICG_MSSQL_PASSWORD", "")
ICG_MSSQL_DRIVER = os.getenv("ICG_MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
ICG_MSSQL_LOGIN_TIMEOUT = int(os.getenv("ICG_MSSQL_LOGIN_TIMEOUT", "10"))
ICG_MSSQL_QUERY_TIMEOUT = int(os.getenv("ICG_MSSQL_QUERY_TIMEOUT", "30"))
ICG_MSSQL_TRUST_SERVER_CERTIFICATE = env_bool("ICG_MSSQL_TRUST_SERVER_CERTIFICATE", False)
