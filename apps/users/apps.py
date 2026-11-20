from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"

    def ready(self):
        from django.contrib import admin

        from . import throttle  # noqa: F401 — rejestracja sygnałów licznika prób logowania
        from .forms import ThrottledAdminAuthenticationForm

        admin.site.login_form = ThrottledAdminAuthenticationForm
