from django.urls import path

from . import views

app_name = "customers"
urlpatterns = [
    path("", views.customer_list, name="list"),
    path("szukaj/", views.customer_search, name="search"),
    path("nowy/", views.customer_create, name="create"),
    path("<int:pk>/", views.customer_detail, name="detail"),
    path("<int:pk>/edycja/", views.customer_edit, name="edit"),
    path("<int:pk>/kontakty/dodaj/", views.contact_add, name="contact_add"),
]
