from django.urls import path

from . import views

app_name = "pwa"
urlpatterns = [
    path("", views.home, name="home"),
    path("nagraj/", views.record, name="record"),
    path("offline/", views.offline, name="offline"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="service_worker"),
]
