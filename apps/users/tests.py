"""Testy logowania: limit nieudanych prób (ekran aplikacji i panel admina) i hasło kont demo."""
import io

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings
from django.urls import reverse

from apps.users.models import Role, User

pytestmark = pytest.mark.django_db
PASSWORD = "haslo-testowe-123"


def _login(client, username, password, url=None, ip="10.0.0.1"):
    return client.post(url or reverse("users:login"), {"username": username, "password": password},
                       REMOTE_ADDR=ip)


def _is_logged_in(client):
    return "_auth_user_id" in client.session


@override_settings(LOGIN_MAX_FAILURES_PER_USER=3, LOGIN_MAX_FAILURES_PER_IP=100)
def test_username_locked_after_failures_even_with_correct_password(client, salesperson):
    for _ in range(3):
        resp = _login(client, "jan", "zle-haslo")
        assert resp.status_code == 200 and "Błędny login lub hasło" in resp.content.decode()
    resp = _login(client, "jan", PASSWORD, ip="10.0.0.2")  # inne IP — limit per login nadal działa
    assert resp.status_code == 200 and "Zbyt wiele nieudanych prób" in resp.content.decode()
    assert not _is_logged_in(client)


@override_settings(LOGIN_MAX_FAILURES_PER_USER=100, LOGIN_MAX_FAILURES_PER_IP=4)
def test_ip_locked_across_usernames(client, salesperson, office_user):
    for name in ("a", "b", "c", "d"):
        _login(client, name, "zle")
    resp = _login(client, "ola", PASSWORD)
    assert "Zbyt wiele nieudanych prób" in resp.content.decode() and not _is_logged_in(client)
    resp = _login(client, "ola", PASSWORD, ip="10.0.0.9")
    assert resp.status_code == 302 and _is_logged_in(client)


@override_settings(LOGIN_MAX_FAILURES_PER_USER=3, LOGIN_MAX_FAILURES_PER_IP=100)
def test_successful_login_resets_username_counter(client, salesperson):
    _login(client, "jan", "zle")
    _login(client, "jan", "zle")
    assert _login(client, "jan", PASSWORD).status_code == 302
    client.logout()
    _login(client, "jan", "zle")
    _login(client, "jan", "zle")
    assert _login(client, "jan", PASSWORD).status_code == 302


@override_settings(LOGIN_MAX_FAILURES_PER_USER=3, LOGIN_MAX_FAILURES_PER_IP=100)
def test_admin_login_shares_the_limit(client, org):
    User.objects.create_user(username="root", password=PASSWORD, organization=org, role=Role.ADMIN,
                             is_staff=True, is_superuser=True)
    admin_login = reverse("admin:login")
    for _ in range(2):
        _login(client, "root", "zle", url=admin_login)
    _login(client, "root", "zle")  # trzecia próba na ekranie aplikacji
    resp = _login(client, "root", PASSWORD, url=admin_login)
    assert resp.status_code == 200 and "Zbyt wiele nieudanych prób" in resp.content.decode()
    assert not _is_logged_in(client)


@override_settings(DEBUG=False)
def test_seed_demo_refuses_default_password_outside_debug(db):
    with pytest.raises(CommandError, match="--password"):
        call_command("seed_demo", stdout=io.StringIO())
    assert not User.objects.exists()
