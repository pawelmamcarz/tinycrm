"""Ustawienia bazowe TinyCRM."""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-insecure-key")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_beat",
    "apps.core",
    "apps.users",
    "apps.customers",
    "apps.tasks",
    "apps.voice",
    "apps.service",
    "apps.notify",
    "apps.erp_sync",
    "apps.pwa",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.OrganizationMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.navigation",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": env.db("DATABASE_URL", default="postgres://tinycrm:tinycrm@localhost:5432/tinycrm")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "users.User"
AUTHENTICATION_BACKENDS = ["apps.users.throttle.ThrottledModelBackend"]
# Limity nieudanych logowań (oba ekrany: /logowanie/ i /admin/login/), okno w sekundach.
LOGIN_MAX_FAILURES_PER_USER = env.int("LOGIN_MAX_FAILURES_PER_USER", default=5)
LOGIN_MAX_FAILURES_PER_IP = env.int("LOGIN_MAX_FAILURES_PER_IP", default=20)
LOGIN_FAILURE_WINDOW_SECONDS = env.int("LOGIN_FAILURE_WINDOW_SECONDS", default=15 * 60)
LOGIN_URL = "/logowanie/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/logowanie/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pl"
TIME_ZONE = "Europe/Warsaw"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))

# Celery — bez REDIS_URL zadania wykonują się synchronicznie (prototyp/testy)
REDIS_URL = env("REDIS_URL", default="")
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_TASK_ALWAYS_EAGER = not bool(REDIS_URL)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# AI i integracje (puste = tryb demo)
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", default="claude-sonnet-5-5")
STT_PROVIDER = env("STT_PROVIDER", default="demo")
ELEVENLABS_API_KEY = env("ELEVENLABS_API_KEY", default="")
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
VAPID_SUBJECT = env("VAPID_SUBJECT", default="mailto:admin@example.com")
ENOVA_BASE_URL = env("ENOVA_BASE_URL", default="")
ENOVA_API_TOKEN = env("ENOVA_API_TOKEN", default="")
# Organizacja synchronizowana codziennie z enova (puste = jedyna organizacja w systemie)
ENOVA_ORG_SLUG = env("ENOVA_ORG_SLUG", default="")
ENOVA_KONTRAHENCI_ENDPOINT = env("ENOVA_KONTRAHENCI_ENDPOINT", default="")  # puste = /api/Kontrahenci
ENOVA_PAGE_SIZE = env.int("ENOVA_PAGE_SIZE", default=0)  # 0 = domyślnie 100
VOICE_AUDIO_RETENTION_DAYS = env.int("VOICE_AUDIO_RETENTION_DAYS", default=30)

# Adres bazowy do linków bezwzględnych w mailach (np. https://crm.example.com); puste = ścieżki względne
SITE_URL = env("SITE_URL", default="").rstrip("/")

# Uploady: nagrania do 25 MB (limit egzekwuje widok voice:upload). Pliki powyżej progu pamięci idą na dysk.
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 26 * 1024 * 1024

# WhiteNoise: w trybie deweloperskim serwuj statyki prosto z aplikacji (bez collectstatic)
WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG

EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="TinyCRM <noreply@example.com>")
