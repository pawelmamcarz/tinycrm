"""Ograniczenie prób logowania (per IP i per login) — wspólne dla `/logowanie/` i `/admin/login/`.

Obie powierzchnie idą przez `django.contrib.auth.authenticate`, więc:
- licznik nieudanych prób podbija sygnał `user_login_failed`,
- udane logowanie (`user_logged_in`) zeruje licznik loginu,
- `ThrottledModelBackend` odmawia uwierzytelnienia, gdy limit jest przekroczony (także przy poprawnym haśle).
Liczniki są w cache Django (przy wielu procesach używaj współdzielonego cache, np. Redis).
"""
import hashlib

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.dispatch import receiver

THROTTLED_MESSAGE = "Zbyt wiele nieudanych prób logowania. Spróbuj ponownie za kilkanaście minut."


def _limits():
    return {
        "user": getattr(settings, "LOGIN_MAX_FAILURES_PER_USER", 5),
        "ip": getattr(settings, "LOGIN_MAX_FAILURES_PER_IP", 20),
        "window": getattr(settings, "LOGIN_FAILURE_WINDOW_SECONDS", 15 * 60),
    }


def _key(kind, value):
    digest = hashlib.sha256(str(value).strip().lower().encode()).hexdigest()[:32]
    return f"login-fail:{kind}:{digest}"


def client_ip(request):
    return (getattr(request, "META", {}) or {}).get("REMOTE_ADDR", "") if request is not None else ""


def _keys(request, username):
    keys = []
    if username:
        keys.append(("user", _key("user", username)))
    ip = client_ip(request)
    if ip:
        keys.append(("ip", _key("ip", ip)))
    return keys


def is_locked(request, username):
    limits = _limits()
    return any((cache.get(key) or 0) >= limits[kind] for kind, key in _keys(request, username))


def register_failure(request, username):
    window = _limits()["window"]
    for _kind, key in _keys(request, username):
        if cache.add(key, 1, timeout=window):
            continue
        try:
            cache.incr(key)
        except ValueError:  # klucz wygasł między add a incr
            cache.set(key, 1, timeout=window)


def reset_user(username):
    if username:
        cache.delete(_key("user", username))


class ThrottledModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            from django.contrib.auth import get_user_model

            username = kwargs.get(get_user_model().USERNAME_FIELD)
        if is_locked(request, username):
            if request is not None:
                request.login_throttled = True
            raise PermissionDenied(THROTTLED_MESSAGE)
        return super().authenticate(request, username=username, password=password, **kwargs)


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    register_failure(request, (credentials or {}).get("username"))


@receiver(user_logged_in)
def _on_logged_in(sender, request, user, **kwargs):
    reset_user(user.get_username())
