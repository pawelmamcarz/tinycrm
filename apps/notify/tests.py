"""Testy modułu notify: serwisy, widoki, web push, skrót dnia, eskalacje, izolacja organizacji."""
import datetime
import json
from unittest import mock

import pytest
from django.core import mail
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.notify import services, tasks as notify_tasks
from apps.notify.models import DailyAlertLog, Notification, PushSubscription
from apps.notify.registrations import NAV, PERIODIC
from apps.tasks.models import Task, TaskStatus
from apps.tasks.services import create_task
from apps.users.models import Role, User

TODAY = datetime.date(2026, 11, 20)
SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123", "keys": {"p256dh": "BPkluczP256", "auth": "sekretAuth"}}


def _post_json(client, url, data):
    return client.post(url, data=json.dumps(data), content_type="application/json")


def _task(org, assignee, due, title="Zadanie", status=None, **kw):
    task = create_task(org, title=title, assignee=assignee, due_date=due, **kw)
    if status:
        Task.objects.filter(pk=task.pk).update(status=status)
        task.refresh_from_db()
    return task


# --- Serwisy -----------------------------------------------------------------------------------

def test_notify_creates_notification(salesperson):
    n = services.notify(salesperson, "T" * 250, "treść", url="/zadania/1/")
    assert len(n.title) == 200 and n.url == "/zadania/1/" and n.read_at is None


def test_notify_survives_push_crash(salesperson):
    with mock.patch.object(services, "send_push", side_effect=RuntimeError("boom")):
        services.notify(salesperson, "Tytuł")
    assert Notification.objects.filter(user=salesperson).count() == 1


def test_once_per_day_is_idempotent_and_keeps_transaction(db):
    assert services.once_per_day("k", day=TODAY) is True
    assert services.once_per_day("k", day=TODAY) is False
    # transakcja nadal działa po IntegrityError (savepoint)
    assert services.once_per_day("k", day=TODAY + datetime.timedelta(days=1)) is True
    assert DailyAlertLog.objects.count() == 2


def test_mark_read_and_all_read(salesperson, office_user):
    a = services.notify(salesperson, "a")
    services.notify(salesperson, "b")
    services.notify(office_user, "c")
    assert services.unread_count(salesperson) == 2
    services.mark_read(a)
    first = a.read_at
    services.mark_read(a)
    assert a.read_at == first
    assert services.mark_all_read(salesperson) == 1
    assert services.unread_count(salesperson) == 0
    assert services.unread_count(office_user) == 1


@pytest.mark.parametrize("url,expected", [
    ("/zadania/5/", "/zadania/5/"),
    ("/klienci/?q=a", "/klienci/?q=a"),
    ("", "/fb/"),
    ("https://evil.example.com/", "/fb/"),
    ("//evil.example.com/", "/fb/"),
    ("/\\evil.example.com", "/fb/"),
    ("/\\/evil.example.com", "/fb/"),
    ("javascript:alert(1)", "/fb/"),
    ("zadania/5/", "/fb/"),
    ("/\t/evil.example.com", "/fb/"),
])
def test_safe_local_url(url, expected):
    assert services.safe_local_url(url, fallback="/fb/") == expected


def test_upsert_subscription_validation(salesperson):
    for bad in [("http://x.example.com/a", "k", "a"), ("", "k", "a"), ("https://x.example.com/a", "", "a"),
                ("https://x.example.com/a", "k", None), (123, "k", "a")]:
        with pytest.raises(services.SubscriptionError):
            services.upsert_subscription(salesperson, *bad)


def test_send_push_removes_gone_subscription(salesperson, settings):
    from pywebpush import WebPushException

    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = "pub", "priv"
    services.upsert_subscription(salesperson, "https://fcm.googleapis.com/fcm/send/gone", "k", "a")
    services.upsert_subscription(salesperson, "https://fcm.googleapis.com/fcm/send/ok", "k", "a")

    def fake(subscription_info, **kw):
        if subscription_info["endpoint"].endswith("gone"):
            raise WebPushException("gone", response=mock.Mock(status_code=410))

    with mock.patch("pywebpush.webpush", side_effect=fake):
        assert services.send_push(salesperson, {"title": "x"}) == 1
    assert list(PushSubscription.objects.values_list("endpoint", flat=True)) == ["https://fcm.googleapis.com/fcm/send/ok"]


def test_send_push_noop_without_keys(salesperson, settings):
    settings.VAPID_PUBLIC_KEY = ""
    services.upsert_subscription(salesperson, "https://fcm.googleapis.com/fcm/send/x", "k", "a")
    with mock.patch("pywebpush.webpush") as wp:
        assert services.send_push(salesperson, {}) == 0
    wp.assert_not_called()


# --- Widoki ------------------------------------------------------------------------------------

def test_views_require_login(client, settings, db):
    login = reverse(settings.LOGIN_URL) if ":" in settings.LOGIN_URL else settings.LOGIN_URL
    for name in ["notify:list", "notify:badge", "notify:vapid_key"]:
        resp = client.get(reverse(name))
        assert resp.status_code == 302 and resp["Location"].startswith(login)
    for name in ["notify:subscribe", "notify:unsubscribe", "notify:mark_all_read"]:
        resp = _post_json(client, reverse(name), SUB)
        assert resp.status_code == 302 and resp["Location"].startswith(login)
    assert not PushSubscription.objects.exists()


def test_user_without_org_forbidden(client, db):
    u = User.objects.create_user(username="bezorg", password="haslo-testowe-123")
    client.force_login(u)
    assert client.get(reverse("notify:list")).status_code == 403


@pytest.mark.parametrize("fixture", ["salesperson", "office_user", "service_user", "boss"])
def test_list_renders_for_all_roles(client, request, fixture):
    user = request.getfixturevalue(fixture)
    services.notify(user, "Nowe zadanie: Oferta na USG", url="/zadania/1/")
    client.force_login(user)
    resp = client.get(reverse("notify:list"))
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Nowe zadanie: Oferta na USG" in html and "Włącz powiadomienia" in html
    assert "notify/push.js" in html


def test_list_shows_only_own_newest_first(client, salesperson, office_user):
    services.notify(salesperson, "Starsze")
    services.notify(salesperson, "Nowsze")
    services.notify(office_user, "Cudze powiadomienie")
    client.force_login(salesperson)
    html = client.get(reverse("notify:list")).content.decode()
    assert "Cudze powiadomienie" not in html
    assert html.index("Nowsze") < html.index("Starsze")


def test_mark_all_read_post_only(client, salesperson, office_user):
    services.notify(salesperson, "a")
    services.notify(office_user, "b")
    client.force_login(salesperson)
    assert client.get(reverse("notify:mark_all_read")).status_code == 405
    resp = client.post(reverse("notify:mark_all_read"))
    assert resp.status_code == 302
    assert services.unread_count(salesperson) == 0 and services.unread_count(office_user) == 1


def test_open_marks_read_and_redirects(client, salesperson):
    n = services.notify(salesperson, "a", url="/zadania/7/")
    client.force_login(salesperson)
    resp = client.post(reverse("notify:open", args=[n.pk]))
    assert resp.status_code == 302 and resp["Location"] == "/zadania/7/"
    n.refresh_from_db()
    assert n.read_at is not None


@pytest.mark.parametrize("evil", ["https://evil.example.com/", "//evil.example.com", "/\\evil.example.com"])
def test_open_blocks_open_redirect(client, salesperson, evil):
    n = Notification.objects.create(user=salesperson, title="x", url=evil)
    client.force_login(salesperson)
    resp = client.post(reverse("notify:open", args=[n.pk]))
    assert resp["Location"] == reverse("notify:list")


def test_open_empty_url_goes_to_list(client, salesperson):
    n = services.notify(salesperson, "x")
    client.force_login(salesperson)
    assert client.post(reverse("notify:open", args=[n.pk]))["Location"] == reverse("notify:list")


def test_open_foreign_notification_404(client, salesperson, office_user, other_org_user):
    n = services.notify(office_user, "x", url="/zadania/1/")
    for intruder in (salesperson, other_org_user):
        client.force_login(intruder)
        assert client.post(reverse("notify:open", args=[n.pk])).status_code == 404
    n.refresh_from_db()
    assert n.read_at is None


def test_open_requires_post(client, salesperson):
    n = services.notify(salesperson, "x")
    client.force_login(salesperson)
    assert client.get(reverse("notify:open", args=[n.pk])).status_code == 405


def test_badge_fragment(client, salesperson, office_user):
    client.force_login(salesperson)
    html = client.get(reverse("notify:badge")).content.decode()
    assert 'id="notify-badge"' in html and "hidden" in html and "hx-get" in html
    services.notify(salesperson, "a")
    services.notify(salesperson, "b")
    services.notify(office_user, "c")
    html = client.get(reverse("notify:badge")).content.decode()
    assert ">2<" in html and "badge-error" in html


def test_badge_caps_at_99(client, salesperson):
    Notification.objects.bulk_create([Notification(user=salesperson, title=str(i)) for i in range(120)])
    client.force_login(salesperson)
    assert "99+" in client.get(reverse("notify:badge")).content.decode()


def test_vapid_key(client, salesperson, settings):
    settings.VAPID_PUBLIC_KEY = "BPublicznyKlucz"
    client.force_login(salesperson)
    assert client.get(reverse("notify:vapid_key")).json() == {"publicKey": "BPublicznyKlucz"}
    settings.VAPID_PUBLIC_KEY = ""
    assert client.get(reverse("notify:vapid_key")).json() == {"publicKey": ""}


def test_subscribe_upsert(client, salesperson):
    client.force_login(salesperson)
    url = reverse("notify:subscribe")
    resp = _post_json(client, url, SUB)
    assert resp.status_code == 201 and resp.json()["created"] is True
    resp = _post_json(client, url, {**SUB, "keys": {"p256dh": "nowyKlucz", "auth": "nowyAuth"}})
    assert resp.status_code == 200 and resp.json()["created"] is False
    sub = PushSubscription.objects.get()
    assert (sub.user, sub.p256dh, sub.auth) == (salesperson, "nowyKlucz", "nowyAuth")


def test_subscribe_same_endpoint_not_moved_to_other_user(client, salesperson, office_user, other_org_user):
    client.force_login(salesperson)
    _post_json(client, reverse("notify:subscribe"), SUB)
    for intruder in (office_user, other_org_user):
        client.force_login(intruder)
        resp = _post_json(client, reverse("notify:subscribe"), SUB)
        assert resp.status_code == 409
    sub = PushSubscription.objects.get()
    assert (sub.user, sub.p256dh) == (salesperson, SUB["keys"]["p256dh"])


@pytest.mark.parametrize("endpoint", [
    "https://internal-service.local/admin/action",
    "https://127.0.0.1/x", "https://10.0.0.5/x", "https://[::1]/x",
    "https://fcm.googleapis.com.evil.example/x", "https://evilfcm.googleapis.com/x",
    "https://fcm.googleapis.com:8443/x", "https://user:pw@fcm.googleapis.com/x",
    "https://notify.windows.com/x",
])
def test_subscribe_rejects_non_push_hosts(client, salesperson, endpoint):
    client.force_login(salesperson)
    resp = _post_json(client, reverse("notify:subscribe"), {"endpoint": endpoint, "keys": SUB["keys"]})
    assert resp.status_code == 400
    assert not PushSubscription.objects.exists()


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/abc", "https://updates.push.services.mozilla.com/wpush/v2/abc",
    "https://web.push.apple.com/QGx", "https://wns2-par02p.notify.windows.com/w/?token=abc",
])
def test_upsert_accepts_known_push_hosts(salesperson, endpoint):
    sub, created = services.upsert_subscription(salesperson, endpoint, "k", "a")
    assert created and sub.endpoint == endpoint


def test_send_push_skips_disallowed_stored_endpoint(salesperson, settings):
    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = "pub", "priv"
    PushSubscription.objects.create(user=salesperson, endpoint="https://internal.local/x", p256dh="k", auth="a")
    with mock.patch("pywebpush.webpush") as wp:
        assert services.send_push(salesperson, {"title": "x"}) == 0
    wp.assert_not_called()


@pytest.mark.parametrize("payload", [
    "nie-json", [1, 2], {"endpoint": "http://plain.example.com/x", "keys": SUB["keys"]},
    {"endpoint": SUB["endpoint"]}, {"endpoint": SUB["endpoint"], "keys": "zle"},
])
def test_subscribe_rejects_bad_payload(client, salesperson, payload):
    client.force_login(salesperson)
    body = payload if isinstance(payload, str) else json.dumps(payload)
    resp = client.post(reverse("notify:subscribe"), data=body, content_type="application/json")
    assert resp.status_code == 400
    assert not PushSubscription.objects.exists()


def test_subscribe_requires_post_and_csrf(salesperson):
    from django.test import Client

    c = Client(enforce_csrf_checks=True)
    c.force_login(salesperson)
    assert c.get(reverse("notify:subscribe")).status_code == 405
    assert _post_json(c, reverse("notify:subscribe"), SUB).status_code == 403


def test_unsubscribe_only_own(client, salesperson, office_user):
    services.upsert_subscription(salesperson, "https://fcm.googleapis.com/fcm/send/mine", "k", "a")
    services.upsert_subscription(office_user, "https://fcm.googleapis.com/fcm/send/theirs", "k", "a")
    client.force_login(salesperson)
    resp = _post_json(client, reverse("notify:unsubscribe"), {"endpoint": "https://fcm.googleapis.com/fcm/send/theirs"})
    assert resp.json()["deleted"] == 0
    resp = _post_json(client, reverse("notify:unsubscribe"), {"endpoint": "https://fcm.googleapis.com/fcm/send/mine"})
    assert resp.json()["deleted"] == 1
    assert list(PushSubscription.objects.values_list("user", flat=True)) == [office_user.pk]


def test_unsubscribe_all_without_endpoint(client, salesperson):
    services.upsert_subscription(salesperson, "https://fcm.googleapis.com/fcm/send/1", "k", "a")
    services.upsert_subscription(salesperson, "https://fcm.googleapis.com/fcm/send/2", "k", "a")
    client.force_login(salesperson)
    assert _post_json(client, reverse("notify:unsubscribe"), {}).json()["deleted"] == 2


# --- Skrót dnia --------------------------------------------------------------------------------

def _digest_notifications(user):
    return Notification.objects.filter(user=user, title__startswith="Na dziś")


def test_morning_digest_sends_once(salesperson, office_user, org):
    salesperson.email = "jan@example.com"
    salesperson.save()
    _task(org, salesperson, TODAY, title="Oferta na aparat EKG")
    _task(org, salesperson, TODAY - datetime.timedelta(days=3), title="Telefon do przychodni")
    _task(org, salesperson, TODAY + datetime.timedelta(days=2), title="Przyszłe zadanie")
    _task(org, salesperson, TODAY, title="Zrobione wcześniej", status=TaskStatus.ZROBIONE)

    assert notify_tasks.morning_digest(today=TODAY) == 1
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ["jan@example.com"]
    assert "Oferta na aparat EKG" in msg.body and "Telefon do przychodni" in msg.body
    assert "ZALEGŁE, termin 17.11.2026" in msg.body
    assert "Przyszłe zadanie" not in msg.body and "Zrobione wcześniej" not in msg.body
    n = _digest_notifications(salesperson).get()
    assert n.title == "Na dziś: 2 zadań (zaległe: 1)"
    assert not _digest_notifications(office_user).exists()

    # idempotencja: drugi przebieg tego samego dnia nic nie wysyła
    assert notify_tasks.morning_digest(today=TODAY) == 0
    assert len(mail.outbox) == 1 and _digest_notifications(salesperson).count() == 1
    # następnego dnia znów
    assert notify_tasks.morning_digest(today=TODAY + datetime.timedelta(days=1)) == 1


def test_digest_without_email_still_notifies(salesperson, org):
    salesperson.email = ""
    salesperson.save()
    _task(org, salesperson, TODAY)
    assert notify_tasks.morning_digest(today=TODAY) == 1
    assert len(mail.outbox) == 0 and _digest_notifications(salesperson).count() == 1


def test_digest_skips_inactive_and_unassigned(salesperson, org):
    salesperson.is_active = False
    salesperson.save()
    _task(org, salesperson, TODAY)
    _task(org, None, TODAY)
    assert notify_tasks.morning_digest(today=TODAY) == 0
    assert not DailyAlertLog.objects.exists()


def test_digest_email_failure_does_not_block(salesperson, org):
    salesperson.email = "jan@example.com"
    salesperson.save()
    _task(org, salesperson, TODAY)
    with mock.patch.object(notify_tasks, "send_mail", side_effect=OSError("smtp")):
        assert notify_tasks.morning_digest(today=TODAY) == 1
    assert _digest_notifications(salesperson).count() == 1


def test_digest_per_org(salesperson, other_org_user, org, other_org):
    _task(org, salesperson, TODAY)
    _task(other_org, other_org_user, TODAY, title="Obce")
    assert notify_tasks.morning_digest(today=TODAY) == 2
    assert "Obce" in _digest_notifications(other_org_user).get().body
    assert "Obce" not in _digest_notifications(salesperson).get().body


def test_morning_digest_celery_eager(salesperson, org):
    _task(org, salesperson, timezone.localdate())
    assert notify_tasks.morning_digest.delay().get() == 1


# --- Eskalacje ---------------------------------------------------------------------------------

def _escalations(user):
    return Notification.objects.filter(user=user, title__startswith="Zaległe zadanie")


def test_overdue_escalation_once_per_task(salesperson, boss, org):
    boss2 = User.objects.create_user(username="szef2", password="x" * 12, organization=org, role=Role.SZEF)
    old = _task(org, salesperson, TODAY - datetime.timedelta(days=2), title="Stare zadanie")
    _task(org, salesperson, TODAY - datetime.timedelta(days=1), title="Wczorajsze")  # jeszcze nie
    _task(org, salesperson, TODAY - datetime.timedelta(days=5), title="Zrobione", status=TaskStatus.ZROBIONE)
    _task(org, salesperson, None, title="Bez terminu")

    assert notify_tasks.overdue_escalation(today=TODAY) == 1
    for b in (boss, boss2):
        n = _escalations(b).get()
        assert n.title == "Zaległe zadanie: Stare zadanie"
        assert n.url == reverse("tasks:detail", args=[old.pk])
        assert "opóźnienie 2 dni" in n.body
    old.refresh_from_db()
    assert old.escalated_at is not None
    assert not _escalations(salesperson).exists()

    # drugi przebieg (i kolejne dni) — bez ponownej eskalacji tego samego zadania
    assert notify_tasks.overdue_escalation(today=TODAY) == 0
    assert notify_tasks.overdue_escalation(today=TODAY + datetime.timedelta(days=1)) == 1  # „Wczorajsze”
    assert _escalations(boss).count() == 2


def test_escalation_org_isolation(salesperson, boss, org, other_org, other_org_user):
    other_boss = User.objects.create_user(username="obcyszef", password="x" * 12, organization=other_org,
                                          role=Role.SZEF)
    _task(org, salesperson, TODAY - datetime.timedelta(days=4), title="Nasze")
    assert notify_tasks.overdue_escalation(today=TODAY) == 1
    assert _escalations(boss).count() == 1
    assert not _escalations(other_boss).exists()
    assert not _escalations(other_org_user).exists()


def test_escalation_waits_for_boss(salesperson, org):
    task = _task(org, salesperson, TODAY - datetime.timedelta(days=4))
    assert notify_tasks.overdue_escalation(today=TODAY) == 0
    task.refresh_from_db()
    assert task.escalated_at is None


def test_escalation_skips_inactive_boss(salesperson, boss, org):
    boss.is_active = False
    boss.save()
    _task(org, salesperson, TODAY - datetime.timedelta(days=4))
    assert notify_tasks.overdue_escalation(today=TODAY) == 0
    assert not _escalations(boss).exists()


def test_escalation_race_claimed_elsewhere(salesperson, boss, org):
    task = _task(org, salesperson, TODAY - datetime.timedelta(days=4))
    candidates = list(notify_tasks.escalation_candidates(TODAY))
    services.mark_tasks_escalated(Task.objects.filter(pk=task.pk))  # równoległy przebieg
    with mock.patch.object(notify_tasks, "escalation_candidates", return_value=candidates):
        assert notify_tasks.overdue_escalation(today=TODAY) == 0
    assert not _escalations(boss).exists()


def test_mark_tasks_escalated_does_not_overwrite(salesperson, org):
    task = _task(org, salesperson, TODAY)
    first = timezone.now() - datetime.timedelta(days=3)
    assert services.mark_tasks_escalated(Task.objects.filter(pk=task.pk), when=first) == 1
    assert services.mark_tasks_escalated(Task.objects.filter(pk=task.pk)) == 0
    task.refresh_from_db()
    assert task.escalated_at == first


# --- Rejestracje i komendy ---------------------------------------------------------------------

def test_registrations():
    assert NAV == [{"label": "Powiadomienia", "url_name": "notify:list", "order": 90, "icon": "🔔", "roles": []}]
    assert {p["name"]: (p["task"], p["crontab"]) for p in PERIODIC} == {
        "notify-morning-digest": ("apps.notify.tasks.morning_digest", {"hour": 7, "minute": 0}),
        "notify-overdue-escalation": ("apps.notify.tasks.overdue_escalation", {"hour": 9, "minute": 0}),
    }
    from importlib import import_module

    for p in PERIODIC:
        module, _, name = p["task"].rpartition(".")
        assert callable(getattr(import_module(module), name))


def test_nav_visible_for_every_role(salesperson, office_user, service_user, boss):
    from apps.core.navigation import nav_for_user

    for u in (salesperson, office_user, service_user, boss):
        assert any(i["url_name"] == "notify:list" for i in nav_for_user(u))


def test_generate_vapid_keys_command():
    from io import StringIO

    from py_vapid import Vapid
    from py_vapid.utils import b64urldecode

    out = StringIO()
    call_command("generate_vapid_keys", stdout=out)
    lines = dict(line.split("=", 1) for line in out.getvalue().splitlines() if line.startswith("VAPID_"))
    assert len(b64urldecode(lines["VAPID_PUBLIC_KEY"].encode())) == 65
    vapid = Vapid.from_string(lines["VAPID_PRIVATE_KEY"])  # format akceptowany przez pywebpush
    from cryptography.hazmat.primitives import serialization

    derived = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    assert derived == b64urldecode(lines["VAPID_PUBLIC_KEY"].encode())


def test_reopened_task_is_escalated_again(salesperson, boss, org):
    """Regresja: zadanie eskalowane → zrobione → ponownie otwarte znów podlega eskalacji."""
    from apps.tasks import services as task_services

    task = create_task(org, title="Reklamacja", assignee=salesperson, due_date=TODAY - datetime.timedelta(days=3))
    assert notify_tasks.overdue_escalation(today=TODAY) == 1
    task.refresh_from_db()
    task_services.change_status(task, TaskStatus.ZROBIONE, user=salesperson)
    task_services.change_status(task, TaskStatus.W_TOKU, user=salesperson)
    task.refresh_from_db()
    assert task.escalated_at is None
    assert notify_tasks.overdue_escalation(today=TODAY) == 1
    assert _escalations(boss).count() == 2
