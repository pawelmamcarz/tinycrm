"""Testy modułu zadań: serwisy, FSM, uprawnienia ról, izolacja organizacji, widoki."""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.notify.models import Notification
from apps.tasks import services
from apps.tasks.models import TASK_LEGAL_TRANSITIONS, Task, TaskComment, TaskSource, TaskStatus
from apps.users.models import Role, User

pytestmark = pytest.mark.django_db
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def customer(org, salesperson):
    return Customer.objects.create(organization=org, name="Szpital Miejski w Radomiu", short_name="SM Radom",
                                   owner=salesperson)


@pytest.fixture
def other_customer(other_org):
    return Customer.objects.create(organization=other_org, name="Klinika Obca")


@pytest.fixture
def task(org, salesperson, office_user, customer):
    return services.create_task(org, title="Wysłać ofertę na USG", created_by=salesperson, assignee=office_user,
                                customer=customer, kind="OFERTA")


@pytest.fixture
def foreign_task(other_org, other_org_user):
    return services.create_task(other_org, title="Obce zadanie", assignee=other_org_user)


# --- serwisy ---------------------------------------------------------------

def test_create_task_status_and_notification(org, salesperson, office_user):
    t = services.create_task(org, title="x" * 300, created_by=salesperson, assignee=office_user)
    assert t.status == TaskStatus.PRZYPISANE
    assert len(t.title) == 255
    assert Notification.objects.filter(user=office_user, url=f"/zadania/{t.pk}/").exists()


def test_create_task_without_assignee_is_new(org):
    t = services.create_task(org, title="Bez wykonawcy")
    assert t.status == TaskStatus.NOWE and t.assignee is None


def test_change_status_done_sets_done_at_and_notifies_creator(task, office_user, salesperson):
    services.change_status(task, TaskStatus.ZROBIONE, user=office_user)
    task.refresh_from_db()
    assert task.status == TaskStatus.ZROBIONE and task.done_at is not None
    assert Notification.objects.filter(user=salesperson, title__startswith="Zrobione").exists()
    services.change_status(task, TaskStatus.W_TOKU, user=office_user)
    assert task.done_at is None


def test_change_status_illegal_raises(task):
    with pytest.raises(ValidationError):
        services.change_status(task, TaskStatus.NOWE)


@pytest.mark.parametrize("src", list(TaskStatus.values))
def test_legal_next_statuses_match_table(org, src):
    t = Task(organization=org, title="t", status=src)
    assert {v for v, _ in t.legal_next_statuses} == set(TASK_LEGAL_TRANSITIONS[src])


def test_pick_office_assignee_fewest_open(org, office_user):
    second = User.objects.create_user(username="ewa", password="x", organization=org, role=Role.BIURO)
    services.create_task(org, title="a", assignee=office_user)
    services.create_task(org, title="b", assignee=office_user)
    done = services.create_task(org, title="c", assignee=second)
    services.create_task(org, title="d", assignee=second)
    services.change_status(done, TaskStatus.ZROBIONE)  # zamknięte się nie liczą
    assert services.pick_office_assignee(org) == second


def test_pick_office_assignee_ignores_other_roles_and_orgs(org, salesperson, boss, other_org_user):
    assert services.pick_office_assignee(org) is None
    assert services.pick_office_assignee(None) is None


def test_pick_office_assignee_skips_inactive(org, office_user):
    office_user.is_active = False
    office_user.save()
    assert services.pick_office_assignee(org) is None


def test_assign_task_moves_new_to_assigned(org, office_user):
    t = services.create_task(org, title="x")
    services.assign_task(t, office_user)
    assert t.status == TaskStatus.PRZYPISANE and t.assignee == office_user
    assert Notification.objects.filter(user=office_user).exists()


def test_assign_task_rejects_foreign_user(org, other_org_user):
    t = services.create_task(org, title="x")
    with pytest.raises(ValidationError):
        services.assign_task(t, other_org_user)


def test_add_comment_notifies_other_party(task, office_user, salesperson):
    Notification.objects.all().delete()
    services.add_comment(task, author=office_user, text="Oferta w przygotowaniu")
    assert TaskComment.objects.filter(task=task, organization=task.organization).count() == 1
    assert Notification.objects.filter(user=salesperson).count() == 1
    assert not Notification.objects.filter(user=office_user).exists()
    with pytest.raises(ValidationError):
        services.add_comment(task, author=office_user, text="   ")


def test_source_url(org):
    assert Task(organization=org, title="t", source_ref="voice:12").source_url == "/notatki/12/"
    assert Task(organization=org, title="t", source_ref="service_ticket:5").source_url == "/serwis/zgloszenia/5/"
    assert Task(organization=org, title="t", source_ref="voice:١٢").source_url == ""
    assert Task(organization=org, title="t", source_ref="inspection:7").source_url == "/serwis/przeglady/7/"
    assert Task(organization=org, title="t", source_ref="demo:t1").source_url == ""
    assert Task(organization=org, title="t").source_url == ""


def test_overdue_flags(org):
    today = timezone.localdate()
    assert Task(organization=org, title="t", due_date=today - timedelta(days=1)).is_overdue
    assert not Task(organization=org, title="t", due_date=today - timedelta(days=1),
                    status=TaskStatus.ZROBIONE).is_overdue
    assert Task(organization=org, title="t", due_date=today).is_due_today


def test_timeline_registration(task, customer, org):
    from apps.tasks.registrations import NAV, TIMELINE

    entries = TIMELINE[0](customer)
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) >= {"date", "icon", "title", "text", "url"}
    assert entry["url"] == f"/zadania/{task.pk}/"
    assert {i["url_name"] for i in NAV} == {"tasks:mine", "tasks:office"}


def test_nav_visibility(salesperson, office_user):
    from apps.core.navigation import nav_for_user

    assert [i["url_name"] for i in nav_for_user(salesperson) if i["url_name"].startswith("tasks:")] == ["tasks:mine"]
    assert {i["url_name"] for i in nav_for_user(office_user)} >= {"tasks:mine", "tasks:office"}


# --- kolejka biura -----------------------------------------------------------

@pytest.mark.parametrize("fixture", ["office_user", "boss", "service_user"])
def test_office_allowed_roles(client, request, fixture):
    client.force_login(request.getfixturevalue(fixture))
    assert client.get(reverse("tasks:office")).status_code == 200


def test_office_forbidden_for_salesperson(client, salesperson):
    client.force_login(salesperson)
    assert client.get(reverse("tasks:office")).status_code == 403


def test_office_requires_login(client):
    assert client.get(reverse("tasks:office")).status_code == 302


def test_office_tabs(client, org, office_user, customer):
    today = timezone.localdate()
    unassigned = services.create_task(org, title="Nieprzypisane-zad")
    overdue = services.create_task(org, title="Spoznione-zad", assignee=office_user,
                                   due_date=today - timedelta(days=2))
    for_today = services.create_task(org, title="Dzisiejsze-zad", assignee=office_user, due_date=today)
    done = services.create_task(org, title="Zrobione-zad", assignee=office_user)
    services.change_status(done, TaskStatus.ZROBIONE)
    old_done = services.create_task(org, title="Stare-zad", assignee=office_user)
    services.change_status(old_done, TaskStatus.ZROBIONE)
    Task.objects.filter(pk=old_done.pk).update(done_at=timezone.now() - timedelta(days=10))
    client.force_login(office_user)
    url = reverse("tasks:office")

    def titles(tab):
        return {t.title for t in client.get(url, {"tab": tab}).context["tasks"]}

    assert titles("nieprzypisane") == {unassigned.title}
    assert titles("po_terminie") == {overdue.title}
    assert titles("dzis") == {for_today.title}
    assert titles("otwarte") == {unassigned.title, overdue.title, for_today.title}
    assert titles("zrobione") == {done.title}
    assert titles("bzdura") == {unassigned.title}
    response = client.get(url, {"tab": "po_terminie"})
    assert "text-error" in response.content.decode()


def test_office_filters(client, org, office_user, boss, customer):
    a = services.create_task(org, title="Zad-A", assignee=office_user, customer=customer, kind="OFERTA")
    b = services.create_task(org, title="Zad-B", assignee=boss, kind="SERWIS")
    c = services.create_task(org, title="Zad-C")
    client.force_login(office_user)
    url = reverse("tasks:office")

    def titles(**params):
        return {t.title for t in client.get(url, {"tab": "otwarte", **params}).context["tasks"]}

    assert titles(assignee=office_user.pk) == {a.title}
    assert titles(assignee="brak") == {c.title}
    assert titles(kind="SERWIS") == {b.title}
    assert titles(customer=customer.pk) == {a.title}
    # Śmieci w filtrach (w tym cyfry Unicode) są ignorowane, nie dają 500.
    assert titles(assignee="١٢", customer="abc", kind="XYZ") == {a.title, b.title, c.title}


def test_office_org_isolation(client, office_user, task, foreign_task):
    client.force_login(office_user)
    response = client.get(reverse("tasks:office"), {"tab": "otwarte"})
    content = response.content.decode()
    assert task.title in content and foreign_task.title not in content
    assert "obcy" not in {u.username for u in response.context["assignable_users"]}


# --- przypisanie (HTMX) -------------------------------------------------------

def test_quick_assign_htmx_renders_row(client, org, office_user, boss):
    t = services.create_task(org, title="Do przypisania")
    client.force_login(office_user)
    response = client.post(reverse("tasks:assign", args=[t.pk]), {"assignee": boss.pk}, **HTMX)
    assert response.status_code == 200
    assert f'id="task-row-{t.pk}"' in response.content.decode()
    t.refresh_from_db()
    assert t.assignee == boss and t.status == TaskStatus.PRZYPISANE


def test_quick_unassign(client, task, office_user):
    client.force_login(office_user)
    response = client.post(reverse("tasks:assign", args=[task.pk]), {"assignee": ""}, **HTMX)
    assert response.status_code == 200
    task.refresh_from_db()
    assert task.assignee is None


def test_quick_assign_foreign_user_rejected(client, task, office_user, other_org_user):
    client.force_login(office_user)
    response = client.post(reverse("tasks:assign", args=[task.pk]), {"assignee": other_org_user.pk}, **HTMX)
    assert response.status_code == 400
    task.refresh_from_db()
    assert task.assignee == office_user


def test_quick_assign_forbidden_for_salesperson(client, task, salesperson):
    client.force_login(salesperson)
    assert client.post(reverse("tasks:assign", args=[task.pk]), {"assignee": ""}).status_code == 403


def test_assign_foreign_task_404(client, office_user, foreign_task):
    client.force_login(office_user)
    assert client.post(reverse("tasks:assign", args=[foreign_task.pk]), {"assignee": ""}).status_code == 404


def test_assign_get_not_allowed(client, task, office_user):
    client.force_login(office_user)
    assert client.get(reverse("tasks:assign", args=[task.pk])).status_code == 405


# --- zmiana statusu ---------------------------------------------------------

def test_status_change_by_assignee_redirects(client, task, office_user):
    client.force_login(office_user)
    response = client.post(reverse("tasks:status", args=[task.pk]), {"status": TaskStatus.ZROBIONE})
    assert response.status_code == 302
    task.refresh_from_db()
    assert task.status == TaskStatus.ZROBIONE


def test_status_change_htmx_row_and_card(client, task, office_user):
    client.force_login(office_user)
    url = reverse("tasks:status", args=[task.pk])
    response = client.post(url, {"status": TaskStatus.W_TOKU}, **HTMX)
    assert response.status_code == 200 and f'id="task-row-{task.pk}"' in response.content.decode()
    response = client.post(url, {"status": TaskStatus.ZROBIONE, "view": "card"}, **HTMX)
    assert response.status_code == 200 and f'id="task-card-{task.pk}"' in response.content.decode()


def test_status_change_illegal_htmx_400(client, task, office_user):
    client.force_login(office_user)
    response = client.post(reverse("tasks:status", args=[task.pk]), {"status": TaskStatus.NOWE}, **HTMX)
    assert response.status_code == 400
    task.refresh_from_db()
    assert task.status == TaskStatus.PRZYPISANE


def test_status_change_illegal_plain_message(client, task, office_user):
    client.force_login(office_user)
    response = client.post(reverse("tasks:status", args=[task.pk]), {"status": "BZDURA"}, follow=True)
    assert "Niedozwolona zmiana statusu" in response.content.decode()
    task.refresh_from_db()
    assert task.status == TaskStatus.PRZYPISANE


def test_status_change_by_creator(client, task, salesperson):
    client.force_login(salesperson)
    response = client.post(reverse("tasks:status", args=[task.pk]), {"status": TaskStatus.ODRZUCONE})
    assert response.status_code == 302
    task.refresh_from_db()
    assert task.status == TaskStatus.ODRZUCONE


def test_status_change_forbidden_for_unrelated_salesperson(client, org, task):
    other_rep = User.objects.create_user(username="piotr", password="x", organization=org, role=Role.HANDLOWIEC)
    # Opiekun klienta może zobaczyć zadanie, ale nie jest wykonawcą ani zlecającym.
    task.customer.owner = other_rep
    task.customer.save()
    client.force_login(other_rep)
    assert client.get(reverse("tasks:detail", args=[task.pk])).status_code == 200
    response = client.post(reverse("tasks:status", args=[task.pk]), {"status": TaskStatus.ZROBIONE})
    assert response.status_code == 403


def test_status_change_by_office_role_not_assignee(client, task, boss):
    client.force_login(boss)
    response = client.post(reverse("tasks:status", args=[task.pk]), {"status": TaskStatus.W_TOKU})
    assert response.status_code == 302


def test_status_change_foreign_task_404(client, office_user, foreign_task):
    client.force_login(office_user)
    response = client.post(reverse("tasks:status", args=[foreign_task.pk]), {"status": TaskStatus.W_TOKU})
    assert response.status_code == 404
    foreign_task.refresh_from_db()
    assert foreign_task.status == TaskStatus.PRZYPISANE


def test_status_change_get_not_allowed(client, task, office_user):
    client.force_login(office_user)
    assert client.get(reverse("tasks:status", args=[task.pk])).status_code == 405


def test_status_change_safe_next(client, task, office_user):
    client.force_login(office_user)
    url = reverse("tasks:status", args=[task.pk])
    response = client.post(url, {"status": TaskStatus.W_TOKU, "next": "https://zly.example.com/"})
    assert response.url == reverse("tasks:detail", args=[task.pk])
    response = client.post(url, {"status": TaskStatus.CZEKA, "next": "/zadania/biuro/"})
    assert response.url == "/zadania/biuro/"


# --- moje zadania -------------------------------------------------------------

def test_mine_groups(client, org, salesperson, office_user):
    today = timezone.localdate()
    services.create_task(org, title="Spoznione", assignee=salesperson, due_date=today - timedelta(days=1))
    services.create_task(org, title="Dzis", assignee=salesperson, due_date=today)
    services.create_task(org, title="Jutro", assignee=salesperson, due_date=today + timedelta(days=1))
    services.create_task(org, title="Kiedys", assignee=salesperson)
    services.create_task(org, title="Cudze", assignee=office_user)
    done = services.create_task(org, title="Zamkniete", assignee=salesperson)
    services.change_status(done, TaskStatus.ZROBIONE)
    client.force_login(salesperson)
    response = client.get(reverse("tasks:mine"))
    assert response.status_code == 200
    groups = {g["label"]: [t.title for t in g["tasks"]] for g in response.context["groups"]}
    assert groups == {"Po terminie": ["Spoznione"], "Dziś": ["Dzis"], "Najbliższe": ["Jutro"],
                      "Bez terminu": ["Kiedys"]}
    assert response.context["total"] == 4


@pytest.mark.parametrize("fixture", ["salesperson", "office_user", "service_user", "boss"])
def test_mine_all_roles(client, request, fixture):
    client.force_login(request.getfixturevalue(fixture))
    assert client.get(reverse("tasks:mine")).status_code == 200


def test_mine_user_without_org_forbidden(client, db):
    user = User.objects.create_user(username="bezorg", password="x")
    client.force_login(user)
    assert client.get(reverse("tasks:mine")).status_code == 403


# --- szczegóły i komentarze ---------------------------------------------------

def test_detail_renders_legal_buttons_and_source(client, org, office_user):
    t = services.create_task(org, title="Z notatki", assignee=office_user, source=TaskSource.GLOS,
                             source_ref="voice:42")
    client.force_login(office_user)
    content = client.get(reverse("tasks:detail", args=[t.pk])).content.decode()
    assert "/notatki/42/" in content and "🎙️" in content
    for value in TASK_LEGAL_TRANSITIONS[TaskStatus.PRZYPISANE]:
        assert f'value="{value}"' in content
    assert 'name="status" value="NOWE"' not in content


def test_detail_service_ticket_link(client, org, service_user):
    t = services.create_task(org, title="Z serwisu", source=TaskSource.SYSTEM, source_ref="service_ticket:7")
    client.force_login(service_user)
    assert "/serwis/zgloszenia/7/" in client.get(reverse("tasks:detail", args=[t.pk])).content.decode()


def test_detail_hides_status_buttons_without_permission(client, org, task):
    other_rep = User.objects.create_user(username="piotr", password="x", organization=org, role=Role.HANDLOWIEC)
    task.customer.owner = other_rep
    task.customer.save()
    client.force_login(other_rep)
    content = client.get(reverse("tasks:detail", args=[task.pk])).content.decode()
    assert 'name="status"' not in content


def test_detail_foreign_org_404(client, office_user, foreign_task):
    client.force_login(office_user)
    assert client.get(reverse("tasks:detail", args=[foreign_task.pk])).status_code == 404


def test_detail_unrelated_salesperson_404(client, org, office_user):
    t = services.create_task(org, title="Wewnętrzne biura", assignee=office_user)
    rep = User.objects.create_user(username="piotr", password="x", organization=org, role=Role.HANDLOWIEC)
    client.force_login(rep)
    assert client.get(reverse("tasks:detail", args=[t.pk])).status_code == 404


def test_comment_htmx(client, task, salesperson):
    client.force_login(salesperson)
    response = client.post(reverse("tasks:comment", args=[task.pk]), {"text": "Klient prosi o rabat"}, **HTMX)
    assert response.status_code == 200
    assert "Klient prosi o rabat" in response.content.decode()
    comment = TaskComment.objects.get(task=task)
    assert comment.author == salesperson and comment.organization == task.organization


def test_comment_empty_htmx_400(client, task, salesperson):
    client.force_login(salesperson)
    assert client.post(reverse("tasks:comment", args=[task.pk]), {"text": ""}, **HTMX).status_code == 400


def test_comment_foreign_task_404(client, office_user, foreign_task):
    client.force_login(office_user)
    assert client.post(reverse("tasks:comment", args=[foreign_task.pk]), {"text": "x"}).status_code == 404
    assert not TaskComment.objects.exists()


# --- tworzenie ----------------------------------------------------------------

def test_create_form_scoped_querysets(client, office_user, customer, other_customer, other_org_user):
    client.force_login(office_user)
    response = client.get(reverse("tasks:create"), {"customer": customer.pk})
    assert response.status_code == 200
    form = response.context["form"]
    assert list(form.fields["customer"].queryset) == [customer]
    assert other_org_user not in form.fields["assignee"].queryset
    assert form.initial["customer"] == customer
    assert 'type="date"' in response.content.decode()


def test_create_by_salesperson_defaults_to_office(client, org, salesperson, office_user, customer):
    client.force_login(salesperson)
    response = client.post(reverse("tasks:create"), {
        "title": "Zadzwonić do oddziału", "description": "", "kind": "KONTAKT", "priority": 2,
        "customer": customer.pk, "assignee": "", "due_date": "2026-12-01",
    })
    assert response.status_code == 302
    t = Task.objects.get(title="Zadzwonić do oddziału")
    assert t.assignee == office_user and t.status == TaskStatus.PRZYPISANE
    assert t.created_by == salesperson and t.organization == org and t.source == TaskSource.RECZNE
    assert t.due_date.isoformat() == "2026-12-01"


def test_create_by_office_keeps_unassigned(client, office_user):
    client.force_login(office_user)
    client.post(reverse("tasks:create"), {"title": "Ogólne", "kind": "INNE", "priority": 2})
    t = Task.objects.get(title="Ogólne")
    assert t.assignee is None and t.status == TaskStatus.NOWE


def test_create_rejects_foreign_customer_and_assignee(client, office_user, other_customer, other_org_user):
    client.force_login(office_user)
    response = client.post(reverse("tasks:create"), {
        "title": "Atak", "kind": "INNE", "priority": 2, "customer": other_customer.pk,
        "assignee": other_org_user.pk,
    })
    assert response.status_code == 200
    assert {"customer", "assignee"} <= set(response.context["form"].errors)
    assert not Task.objects.filter(title="Atak").exists()


def test_create_has_no_status_field(client, office_user):
    client.force_login(office_user)
    client.post(reverse("tasks:create"), {"title": "Status", "kind": "INNE", "priority": 2, "status": "ZROBIONE"})
    assert Task.objects.get(title="Status").status == TaskStatus.NOWE


def test_create_requires_login(client):
    assert client.get(reverse("tasks:create")).status_code == 302


def test_admin_registered():
    from django.contrib import admin

    assert Task in admin.site._registry


# ---------------------------------------------------------------- zadania systemowe i eskalacja

@pytest.mark.parametrize("initial", [TaskStatus.NOWE, TaskStatus.PRZYPISANE, TaskStatus.W_TOKU, TaskStatus.CZEKA,
                                     TaskStatus.ODLOZONE])
def test_close_system_tasks_from_any_open_status(org, other_org, initial):
    task = services.create_task(org, title="Umówić przegląd", source=TaskSource.SYSTEM, source_ref="inspection:7")
    Task.objects.filter(pk=task.pk).update(status=initial)
    foreign = services.create_task(other_org, title="Obce", source=TaskSource.SYSTEM, source_ref="inspection:7")
    assert services.close_system_tasks(org, "inspection:7") == 1
    task.refresh_from_db()
    foreign.refresh_from_db()
    assert task.status == TaskStatus.ZROBIONE and task.done_at is not None
    assert foreign.status == TaskStatus.NOWE


def test_close_system_tasks_leaves_closed(org):
    task = services.create_task(org, title="X", source_ref="inspection:8")
    Task.objects.filter(pk=task.pk).update(status=TaskStatus.ODRZUCONE)
    assert services.close_system_tasks(org, "inspection:8") == 0
    task.refresh_from_db()
    assert task.status == TaskStatus.ODRZUCONE


def test_reopen_clears_escalation(org, salesperson):
    task = services.create_task(org, title="X", assignee=salesperson)
    services.change_status(task, TaskStatus.ZROBIONE)
    Task.objects.filter(pk=task.pk).update(escalated_at=timezone.now())
    task.refresh_from_db()
    services.change_status(task, TaskStatus.W_TOKU)
    task.refresh_from_db()
    assert task.escalated_at is None
    # zmiana między stanami otwartymi nie kasuje znacznika
    Task.objects.filter(pk=task.pk).update(escalated_at=timezone.now())
    task.refresh_from_db()
    services.change_status(task, TaskStatus.CZEKA)
    task.refresh_from_db()
    assert task.escalated_at is not None
