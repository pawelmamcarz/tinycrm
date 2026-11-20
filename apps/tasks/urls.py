from django.urls import path

from . import views

app_name = "tasks"
urlpatterns = [
    path("", views.mine, name="mine"),
    path("biuro/", views.office, name="office"),
    path("nowe/", views.create, name="create"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/status/", views.status_change, name="status"),
    path("<int:pk>/przypisz/", views.assign, name="assign"),
    path("<int:pk>/komentarz/", views.comment, name="comment"),
]
