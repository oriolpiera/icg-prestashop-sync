from django.urls import path
from django.views.generic import RedirectView

from apps.operations.sites import admin_site

urlpatterns = [
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("admin/", admin_site.urls),
]
