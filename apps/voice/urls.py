from django.urls import path

from . import views

app_name = "voice"
urlpatterns = [
    path("", views.note_list, name="list"),
    path("wyslij/", views.upload, name="upload"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/zatwierdz/", views.confirm, name="confirm"),
    path("<int:pk>/nagranie/", views.audio, name="audio"),
]
