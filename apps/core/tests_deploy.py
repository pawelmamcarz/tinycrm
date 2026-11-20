"""Konfiguracja wdrożeniowa: sonda zdrowia i ustawienia produkcyjne."""
import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured


@pytest.mark.django_db
def test_healthz_returns_ok_without_login(client):
    response = client.get("/healthz/")
    assert response.status_code == 200
    assert response.content == b"ok"


def _load_production(monkeypatch, **env):
    for key in ("DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "RAILWAY_PUBLIC_DOMAIN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("config.settings.production", None)
    return importlib.import_module("config.settings.production")


def test_production_requires_secret_key(monkeypatch):
    with pytest.raises(ImproperlyConfigured):
        _load_production(monkeypatch, RAILWAY_PUBLIC_DOMAIN="demo.example.com", DJANGO_SECRET_KEY="")


def test_production_uses_railway_domain(monkeypatch):
    mod = _load_production(
        monkeypatch, DJANGO_SECRET_KEY="s" * 60, RAILWAY_PUBLIC_DOMAIN="demo.example.com"
    )
    assert mod.DEBUG is False
    assert "demo.example.com" in mod.ALLOWED_HOSTS
    assert mod.CSRF_TRUSTED_ORIGINS == ["https://demo.example.com"]
    assert mod.SITE_URL == "https://demo.example.com"
    assert mod.SESSION_COOKIE_SECURE and mod.CSRF_COOKIE_SECURE
