"""Testy dymne integracji: każda rola × każda pozycja nawigacji, strony szczegółów z danych demo,
`seed_demo` (idempotencja, reset) i `setup_periodic`.
"""
import io

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.core.navigation import collect_nav, nav_for_user
from apps.users.models import Role, User

ROLE_FIXTURES = ["salesperson", "office_user", "service_user", "boss", "admin_user"]
PASSWORD = "Haslo-Testowe-Demo-1"


@pytest.fixture
def admin_user(org):
    return User.objects.create_user(username="admin_t", password="haslo-testowe-123", organization=org,
                                    role=Role.ADMIN, is_staff=True, is_superuser=True)


@pytest.fixture
def demo(db):
    """Pełne dane demo (organizacja „demo”) — przez komendę, tak jak w prototypie."""
    call_command("seed_demo", password=PASSWORD, stdout=io.StringIO())
    from apps.core.models import Organization

    org = Organization.objects.get(slug="demo")
    return {"org": org, "users": {u.username: u for u in User.objects.filter(organization=org)}}


def _all_nav_urls():
    out = {}
    for item in collect_nav():
        out[item["url_name"]] = reverse(item["url_name"])
    return out


def test_nav_registry_resolves():
    urls = _all_nav_urls()
    # Każdy moduł z UI zgłasza co najmniej jedną pozycję.
    for name in ("pwa:home", "tasks:mine", "tasks:office", "voice:list", "customers:list", "service:dashboard",
                 "erp_sync:status", "notify:list"):
        assert name in urls


@pytest.mark.django_db
@pytest.mark.parametrize("role_fixture", ROLE_FIXTURES)
def test_visible_nav_urls_return_200(request, client, role_fixture):
    user = request.getfixturevalue(role_fixture)
    client.force_login(user)
    items = nav_for_user(user)
    assert items, "każda rola widzi co najmniej jedną pozycję menu"
    for item in items:
        response = client.get(item["url"])
        assert response.status_code == 200, f"{user.role} → {item['url_name']}: {response.status_code}"


@pytest.mark.django_db
@pytest.mark.parametrize("role_fixture", ROLE_FIXTURES)
def test_hidden_nav_urls_are_denied_not_500(request, client, role_fixture):
    user = request.getfixturevalue(role_fixture)
    client.force_login(user)
    visible = {item["url_name"] for item in nav_for_user(user)}
    for name, url in _all_nav_urls().items():
        if name in visible:
            continue
        response = client.get(url)
        assert response.status_code in (302, 403), f"{user.role} → {name}: {response.status_code}"


@pytest.mark.django_db
def test_anonymous_nav_redirects_to_login(client):
    for name, url in _all_nav_urls().items():
        response = client.get(url)
        assert response.status_code == 302, name
        assert reverse("users:login") in response["Location"]


@pytest.mark.django_db
def test_home_redirect_for_every_role(request, client):
    for role_fixture in ROLE_FIXTURES:
        user = request.getfixturevalue(role_fixture)
        client.force_login(user)
        response = client.get(reverse("core:home"), follow=True)
        assert response.status_code == 200, role_fixture


@pytest.mark.django_db
def test_login_page_and_base_shell(client, salesperson):
    assert client.get(reverse("users:login")).status_code == 200
    client.force_login(salesperson)
    html = client.get(reverse("tasks:mine")).content.decode()
    assert reverse("notify:badge") in html  # dzwonek w navbarze
    assert reverse("pwa:service_worker") in html
    assert "htmx:responseError" in html


# --- dane demo ---------------------------------------------------------------------------------

def _seeded_detail_urls(org):
    from apps.customers.models import Customer
    from apps.service.models import Device, ServiceTicket
    from apps.tasks.models import Task
    from apps.voice.models import VoiceNote

    customer = Customer.objects.for_org(org).get(name="Szpital Specjalistyczny w Radomiu")
    return {
        "customer": reverse("customers:detail", args=[customer.pk]),
        "task": reverse("tasks:detail", args=[Task.objects.for_org(org).filter(source_ref="demo:t1").get().pk]),
        "device": reverse("service:device_detail", args=[Device.objects.for_org(org).first().pk]),
        "ticket": reverse("service:ticket_detail", args=[ServiceTicket.objects.for_org(org).first().pk]),
        "voice": reverse("voice:detail", args=[VoiceNote.objects.for_org(org).get().pk]),
    }


@pytest.mark.django_db
def test_seed_demo_contents(demo):
    from apps.customers.models import Customer
    from apps.service.models import Device, PartOrder, ServiceTicket
    from apps.tasks.models import Task
    from apps.voice.models import VoiceNote, VoiceStatus

    org, users = demo["org"], demo["users"]
    assert org.name == "Demo Med"
    assert {u: users[u].role for u in users} == {
        "jan": Role.HANDLOWIEC, "ewa": Role.HANDLOWIEC, "ola": Role.BIURO, "marek": Role.SERWIS,
        "szef": Role.SZEF, "admin": Role.ADMIN,
    }
    assert users["admin"].is_staff and users["admin"].is_superuser
    assert users["jan"].check_password(PASSWORD)
    assert Customer.objects.for_org(org).count() >= 25
    assert Device.objects.for_org(org).count() >= 30
    assert any(d.is_inspection_overdue for d in Device.objects.for_org(org))
    assert ServiceTicket.objects.for_org(org).count() == 2
    assert PartOrder.objects.for_org(org).count() >= 2
    assert any(t.is_overdue for t in Task.objects.for_org(org))
    assert Task.objects.for_org(org).values("status").distinct().count() >= 4
    note = VoiceNote.objects.for_org(org).get()
    assert note.status == VoiceStatus.DO_POTWIERDZENIA
    assert note.author == users["jan"]
    assert note.customer and "Radom" in note.customer.name
    assert note.extraction.get("tasks")


@pytest.mark.django_db
def test_seed_demo_is_idempotent_and_resettable(demo):
    from apps.customers.models import Customer
    from apps.service.models import Device, Inspection, ServiceTicket
    from apps.tasks.models import Task
    from apps.voice.models import VoiceNote

    org = demo["org"]

    def counts():
        return [M.objects.for_org(org).count() for M in (Customer, Device, Inspection, ServiceTicket, Task, VoiceNote)]

    before = counts()
    call_command("seed_demo", password=PASSWORD, stdout=io.StringIO())
    assert counts() == before
    call_command("seed_demo", "--reset", password=PASSWORD, stdout=io.StringIO())
    from apps.core.models import Organization

    org = Organization.objects.get(slug="demo")
    assert [M.objects.for_org(org).count() for M in (Customer, Device, Inspection, ServiceTicket, Task, VoiceNote)] \
        == before


@pytest.mark.django_db
@pytest.mark.parametrize("username", ["ola", "szef", "admin"])
def test_seeded_detail_pages_for_office(client, demo, username):
    client.force_login(demo["users"][username])
    for label, url in _seeded_detail_urls(demo["org"]).items():
        assert client.get(url).status_code == 200, f"{username} → {label}"


@pytest.mark.django_db
def test_seeded_detail_pages_for_salesperson_author(client, demo):
    client.force_login(demo["users"]["jan"])
    urls = _seeded_detail_urls(demo["org"])
    for label in ("customer", "task", "voice", "device", "ticket"):
        assert client.get(urls[label]).status_code == 200, label


@pytest.mark.django_db
def test_seeded_nav_for_demo_users(client, demo):
    for user in demo["users"].values():
        client.force_login(user)
        for item in nav_for_user(user):
            assert client.get(item["url"]).status_code == 200, f"{user.username} → {item['url_name']}"


@pytest.mark.django_db
def test_seeded_detail_pages_isolated_from_other_org(client, demo, other_org_user):
    client.force_login(other_org_user)
    for label, url in _seeded_detail_urls(demo["org"]).items():
        assert client.get(url).status_code == 404, label


@pytest.mark.django_db
def test_seed_demo_nips_are_fictional_and_valid(demo):
    from apps.customers.models import Customer

    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    for customer in Customer.objects.for_org(demo["org"]):
        nip = customer.nip
        assert nip.startswith("000") and len(nip) == 10
        assert sum(w * int(d) for w, d in zip(weights, nip)) % 11 == int(nip[9])


# --- setup_periodic ----------------------------------------------------------------------------

@pytest.mark.django_db
def test_setup_periodic_dry_run_writes_nothing():
    from django_celery_beat.models import PeriodicTask

    out = io.StringIO()
    call_command("setup_periodic", "--dry-run", stdout=out)
    assert "notify-morning-digest" in out.getvalue()
    assert not PeriodicTask.objects.filter(name="notify-morning-digest").exists()


@pytest.mark.django_db
def test_setup_periodic_upserts_with_warsaw_timezone():
    from django_celery_beat.models import PeriodicTask

    from apps.core.management.commands.setup_periodic import collect_periodic

    call_command("setup_periodic", stdout=io.StringIO())
    call_command("setup_periodic", stdout=io.StringIO())  # idempotentnie
    entries = collect_periodic()
    assert PeriodicTask.objects.filter(name__in=[e["name"] for e in entries]).count() == len(entries)
    digest = PeriodicTask.objects.get(name="notify-morning-digest")
    assert digest.task == "apps.notify.tasks.morning_digest"
    assert (digest.crontab.hour, digest.crontab.minute) == ("7", "0")
    assert str(digest.crontab.timezone) == "Europe/Warsaw"
    escalate = PeriodicTask.objects.get(name="voice-escalate-unconfirmed")
    assert (escalate.crontab.minute, escalate.crontab.hour) == ("*/15", "*")
