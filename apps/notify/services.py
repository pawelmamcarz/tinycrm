"""Publiczne API powiadomień. Push jest best-effort; Notification w bazie zawsze powstaje."""
import json
import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .models import DailyAlertLog, Notification, PushSubscription

logger = logging.getLogger(__name__)


def notify(user, title, body="", url=""):
    notification = Notification.objects.create(user=user, title=title[:200], body=body, url=url[:300])
    try:
        send_push(user, {"title": title, "body": body[:200], "url": url})
    except Exception:  # noqa: BLE001 — push nigdy nie może zablokować powiadomienia w bazie
        logger.exception("push: nieoczekiwany błąd")
    return notification


def send_push(user, payload):
    if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY):
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return 0
    sent = 0
    for sub in PushSubscription.objects.filter(user=user):
        if not is_allowed_push_endpoint(sub.endpoint):
            logger.warning("push: pomijam subskrypcję %s z niedozwolonym hostem", sub.pk)
            continue
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=json.dumps(payload), vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
            )
            sent += 1
        except WebPushException as exc:
            if getattr(exc, "response", None) is not None and exc.response.status_code in (404, 410):
                sub.delete()
            logger.warning("push failed: %s", exc)
    return sent


def once_per_day(key, day=None):
    """True przy pierwszym wywołaniu dla (key, dzień); kolejne zwracają False.

    Własny savepoint: IntegrityError nie psuje transakcji wywołującego.
    """
    day = day or timezone.localdate()
    try:
        with transaction.atomic():
            DailyAlertLog.objects.create(key=key[:200], day=day)
        return True
    except IntegrityError:
        return False


# --- Odczyt / oznaczanie ------------------------------------------------------------------------

def unread_count(user):
    if not getattr(user, "is_authenticated", False):
        return 0
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


def mark_read(notification):
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
    return notification


def mark_all_read(user):
    return Notification.objects.filter(user=user, read_at__isnull=True).update(read_at=timezone.now())


def safe_local_url(url, fallback="/"):
    """Zwraca `url` tylko, gdy to ścieżka względna w obrębie aplikacji (bez open redirect)."""
    url = (url or "").strip()
    if (
        not url.startswith("/")
        or url.startswith("//")
        or url.startswith("/\\")
        or any(ord(ch) < 32 or ch == "\\" for ch in url)
        or not url_has_allowed_host_and_scheme(url, allowed_hosts=set(), require_https=False)
    ):
        return fallback
    return url


# --- Web push: subskrypcje ------------------------------------------------------------------------

class SubscriptionError(ValueError):
    pass


class SubscriptionConflict(SubscriptionError):
    """Endpoint jest już przypisany do innego konta — nie przenosimy go."""


# Hosty usług push przeglądarek. Serwer wysyła POST-y wyłącznie tam (ochrona przed SSRF).
# Wpis zaczynający się od „.” dopuszcza subdomeny (np. `wns2-par02p.notify.windows.com`).
DEFAULT_PUSH_HOSTS = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
    ".notify.windows.com",
)


def _push_hosts():
    return tuple(getattr(settings, "WEBPUSH_ALLOWED_HOSTS", None) or DEFAULT_PUSH_HOSTS)


def is_allowed_push_endpoint(endpoint):
    """True tylko dla `https://<znany host usługi push>/...` bez portu, danych logowania i adresów IP."""
    if not isinstance(endpoint, str) or len(endpoint) > 500 or not endpoint.startswith("https://"):
        return False
    if any(ord(ch) < 33 or ch == "\\" for ch in endpoint):
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password or port not in (None, 443):
        return False
    for allowed in _push_hosts():
        allowed = allowed.lower()
        if allowed.startswith("."):
            if host.endswith(allowed) and len(host) > len(allowed):
                return True
        elif host == allowed:
            return True
    return False


def upsert_subscription(user, endpoint, p256dh, auth):
    """Zapisuje subskrypcję push użytkownika. Ten sam endpoint tego samego użytkownika = aktualizacja kluczy.

    Endpoint innego użytkownika → `SubscriptionConflict` (nie przejmujemy cudzego kanału push).
    """
    endpoint = (endpoint or "").strip() if isinstance(endpoint, str) else ""
    p256dh = p256dh.strip() if isinstance(p256dh, str) else ""
    auth = auth.strip() if isinstance(auth, str) else ""
    if not is_allowed_push_endpoint(endpoint):
        raise SubscriptionError("Nieprawidłowy endpoint subskrypcji.")
    if not p256dh or len(p256dh) > 200 or not auth or len(auth) > 100:
        raise SubscriptionError("Brak lub nieprawidłowe klucze subskrypcji.")
    with transaction.atomic():
        existing = PushSubscription.objects.select_for_update().filter(endpoint=endpoint).first()
        if existing is not None and existing.user_id != user.pk:
            raise SubscriptionConflict(
                "To urządzenie ma już subskrypcję innego konta. Wyłącz powiadomienia i włącz je ponownie.")
        sub, created = PushSubscription.objects.update_or_create(
            endpoint=endpoint, defaults={"user": user, "p256dh": p256dh, "auth": auth},
        )
    return sub, created


def remove_subscription(user, endpoint=None):
    qs = PushSubscription.objects.filter(user=user)
    if endpoint:
        qs = qs.filter(endpoint=endpoint)
    deleted, _ = qs.delete()
    return deleted


# --- Pomocnicze ---------------------------------------------------------------------------------

def reverse_or(name, args=None, fallback="/"):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return fallback


def mark_tasks_escalated(task_queryset, when=None):
    """Jedyny zapis do modelu zadań z modułu notify: znacznik eskalacji (warunkowo, bez nadpisywania).

    Zwraca liczbę faktycznie oznaczonych wierszy — 0 znaczy, że ktoś już eskalował.
    """
    return task_queryset.filter(escalated_at__isnull=True).update(escalated_at=when or timezone.now())
