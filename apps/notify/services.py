"""Publiczne API powiadomień. Push jest best-effort; Notification w bazie zawsze powstaje."""
import json
import logging

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from .models import DailyAlertLog, Notification, PushSubscription

logger = logging.getLogger(__name__)


def notify(user, title, body="", url=""):
    notification = Notification.objects.create(user=user, title=title[:200], body=body, url=url)
    send_push(user, {"title": title, "body": body[:200], "url": url})
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
    """True przy pierwszym wywołaniu dla (key, dzień); kolejne zwracają False."""
    day = day or timezone.localdate()
    try:
        DailyAlertLog.objects.create(key=key[:200], day=day)
        return True
    except IntegrityError:
        return False
