from django.urls import path

from . import views

app_name = "service"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("urzadzenia/", views.device_list, name="devices"),
    path("urzadzenia/nowe/", views.device_create, name="device_create"),
    path("urzadzenia/<int:pk>/", views.device_detail, name="device_detail"),
    path("urzadzenia/<int:pk>/edycja/", views.device_edit, name="device_edit"),
    path("urzadzenia/<int:pk>/przeglad/", views.inspection_complete, name="inspection_complete"),
    path("przeglady/<int:pk>/", views.inspection_redirect, name="inspection_detail"),
    path("przeglady/<int:pk>/status/", views.inspection_status, name="inspection_status"),
    path("przeglady/<int:pk>/protokol/", views.inspection_protocol, name="inspection_protocol"),
    path("zgloszenia/", views.ticket_list, name="tickets"),
    path("zgloszenia/nowe/", views.ticket_create, name="ticket_create"),
    path("zgloszenia/<int:pk>/", views.ticket_detail, name="ticket_detail"),
    path("zgloszenia/<int:pk>/status/", views.ticket_status, name="ticket_status"),
    path("zgloszenia/<int:pk>/przypisz/", views.ticket_assign, name="ticket_assign"),
    path("zgloszenia/<int:pk>/czesci/", views.part_order_add, name="part_order_add"),
    path("czesci/", views.parts, name="parts"),
    path("czesci/<int:pk>/status/", views.part_order_status, name="part_order_status"),
]
