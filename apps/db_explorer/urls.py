from django.urls import path

from apps.db_explorer import views

app_name = "db_explorer"

urlpatterns = [
    path("", views.table_list, name="table_list"),
    path("table/<str:table_name>/", views.table_detail, name="table_detail"),
    path("relationships/", views.relationships, name="relationships"),
]
