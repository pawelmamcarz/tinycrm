from django.contrib.auth import views as auth_views
from django.urls import path

app_name = "users"
urlpatterns = [
    path("logowanie/", auth_views.LoginView.as_view(template_name="users/login.html"), name="login"),
    path("wyloguj/", auth_views.LogoutView.as_view(), name="logout"),
]
