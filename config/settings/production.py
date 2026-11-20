"""Ustawienia produkcyjne/demo (Railway). Konfiguracja wyłącznie ze zmiennych środowiskowych."""
import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

SECRET_KEY = env("DJANGO_SECRET_KEY", default="")
if not SECRET_KEY or SECRET_KEY == "dev-only-insecure-key":
    raise ImproperlyConfigured("Ustaw DJANGO_SECRET_KEY w zmiennych środowiska.")

# Railway podaje publiczną domenę w RAILWAY_PUBLIC_DOMAIN
_railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[]) + ([_railway_domain] if _railway_domain else [])
if _railway_domain:
    ALLOWED_HOSTS.append("healthcheck.railway.app")  # sonda zdrowia Railway
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("Ustaw DJANGO_ALLOWED_HOSTS albo RAILWAY_PUBLIC_DOMAIN.")
CSRF_TRUSTED_ORIGINS = [
    f"https://{h}" for h in ALLOWED_HOSTS if h and not h.startswith(".") and h != "healthcheck.railway.app"
]
if not SITE_URL and _railway_domain:  # noqa: F405
    SITE_URL = f"https://{_railway_domain}"

# TLS kończy się na proxy Railway
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_REDIRECT_EXEMPT = [r"^healthz/?$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=3600)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_USE_FINDERS = False
WHITENOISE_AUTOREFRESH = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}
