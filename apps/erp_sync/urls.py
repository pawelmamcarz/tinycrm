from django.urls import path

from . import views

app_name = "erp_sync"
urlpatterns = [
    path("", views.status, name="status"),
]
