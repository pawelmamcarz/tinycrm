from django.urls import path

from . import views

app_name = "notify"
urlpatterns = [
    path("", views.notification_list, name="list"),
    path("odczytaj-wszystkie/", views.mark_all, name="mark_all_read"),
    path("<int:pk>/otworz/", views.open_notification, name="open"),
    path("licznik/", views.badge, name="badge"),
    path("push/klucz/", views.vapid_key, name="vapid_key"),
    path("push/subskrybuj/", views.subscribe, name="subscribe"),
    path("push/wypisz/", views.unsubscribe, name="unsubscribe"),
]
