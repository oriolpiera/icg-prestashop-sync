from django.apps import AppConfig


class DbExplorerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.db_explorer"
    verbose_name = "Database Explorer"
