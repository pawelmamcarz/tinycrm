from .base import *  # noqa: F401,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
CELERY_BROKER_URL = "memory://"
ANTHROPIC_API_KEY = ""
STT_PROVIDER = "demo"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Osobna baza testowa per agent/proces: TEST_DB_SUFFIX=voice → test_tinycrm_voice
import os  # noqa: E402

_suffix = os.environ.get("TEST_DB_SUFFIX", "")
DATABASES["default"]["TEST"] = {"NAME": f"test_tinycrm_{_suffix}" if _suffix else "test_tinycrm"}  # noqa: F405

# Testy nie wymagają collectstatic — bez STATIC_ROOT WhiteNoise nie ostrzega o brakującym katalogu.
STATIC_ROOT = None
SITE_URL = ""
