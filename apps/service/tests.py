"""Testy modułu serwisu: serwisy, FSM, przeglądy, zadania okresowe, uprawnienia ról, izolacja organizacji."""
import datetime
from unittest import mock

from dateutil.relativedelta import relativedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.notify.models import DailyAlertLog, Notification
from apps.service import services
from apps.service import tasks as service_tasks
from apps.service.models import (
    INSPECTION_LEGAL_TRANSITIONS,
    PART_ORDER_LEGAL_TRANSITIONS,
    TICKET_LEGAL_TRANSITIONS,
    Device,
    Inspection,
    InspectionStatus,
    PartOrder,
    PartOrderStatus,
    ServiceTicket,
    TicketStatus,
)
from apps.tasks.models import Task, TaskStatus
from apps.users.models import Role, User

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()


def days(n):
    return TODAY + datetime.timedelta(days=n)


@pytest.fixture
def customer(org, salesperson):
    return Customer.objects.create(organization=org, name="Szpital Powiatowy w Zielonej Dolinie",
                                   short_name="SP Zielona Dolina", owner=salesperson)


@pytest.fixture
def other_sales(org):
    return User.objects.create_user(username="piotr", password="haslo-testowe-123", organization=org,
                                    role=Role.HANDLOWIEC)


@pytest.fixture
def foreign_customer(org, other_sales):
    return Customer.objects.create(organization=org, name="Klinika Pod Lipami", owner=other_sales)


@pytest.fixture
def device(org, customer):
    return Device.objects.create(organization=org, customer=customer, name="Aparat USG Vista 3", manufacturer="MedTec",
                                 serial_number="SN-001", inspection_interval_months=12,
                                 next_inspection_due=days(20), warranty_until=days(45))


@pytest.fixture
def other_org_customer(other_org):
    return Customer.objects.create(organization=other_org, name="Obca Przychodnia")


@pytest.fixture
def other_org_device(other_org, other_org_customer):
    return Device.objects.create(organization=other_org, customer=other_org_customer, name="Obcy defibrylator",
                                 next_inspection_due=days(5))


@pytest.fixture
def ticket(org, customer, device, salesperson):
    return services.create_ticket(org, customer=customer, device=device, title="Nie działa głowica",
                                  reported_by=salesperson)


@pytest.fixture
def other_org_ticket(other_org, other_org_customer):
    return services.create_ticket(other_org, customer=other_org_customer, title="Obce zgłoszenie")


def login(client, user):
    client.force_login(user)
    return client


# ---------------------------------------------------------------- numeracja

class TestTicketNumbering:
    def test_first_number(self, ticket):
        assert ticket.number == f"SRV-{TODAY.year}-0001"

    def test_sequential(self, org, customer, ticket):
        second = services.create_ticket(org, customer=customer, title="Drugie")
        assert second.number == f"SRV-{TODAY.year}-0002"

    def test_after_9999_comes_10000_then_10001(self, org, customer):
        ServiceTicket.objects.create(organization=org, customer=customer, title="stare",
                                     number=f"SRV-{TODAY.year}-9999")
        t1 = services.create_ticket(org, customer=customer, title="a")
        assert t1.number == f"SRV-{TODAY.year}-10000"
        t2 = services.create_ticket(org, customer=customer, title="b")
        assert t2.number == f"SRV-{TODAY.year}-10001"

    def test_numbering_per_org_and_ignores_garbage(self, org, customer, other_org, other_org_customer):
        ServiceTicket.objects.create(organization=other_org, customer=other_org_customer, title="x",
                                     number=f"SRV-{TODAY.year}-0500")
        ServiceTicket.objects.create(organization=org, customer=customer, title="x", number=f"SRV-{TODAY.year}-١٢")
        assert services.create_ticket(org, customer=customer, title="a").number == f"SRV-{TODAY.year}-0001"

    def test_previous_year_does_not_continue(self, org, customer):
        ServiceTicket.objects.create(organization=org, customer=customer, title="x",
                                     number=f"SRV-{TODAY.year - 1}-0042")
        assert services.create_ticket(org, customer=customer, title="a").number == f"SRV-{TODAY.year}-0001"

    def test_create_ticket_rejects_foreign_customer(self, org, other_org_customer):
        with pytest.raises(ValidationError):
            services.create_ticket(org, customer=other_org_customer, title="x")

    def test_create_ticket_rejects_device_of_other_customer(self, org, foreign_customer, device):
        with pytest.raises(ValidationError):
            services.create_ticket(org, customer=foreign_customer, device=device, title="x")

    def test_assignee_gets_notification(self, org, customer, service_user, salesperson):
        services.create_ticket(org, customer=customer, title="x", reported_by=salesperson, assignee=service_user)
        assert Notification.objects.filter(user=service_user).exists()


# ---------------------------------------------------------------- FSM zgłoszeń

class TestTicketFSM:
    def test_legal_transition(self, ticket, service_user):
        services.change_ticket_status(ticket, TicketStatus.W_TOKU, user=service_user)
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.W_TOKU

    def test_illegal_transition(self, ticket):
        with pytest.raises(ValidationError):
            services.change_ticket_status(ticket, TicketStatus.ZAKONCZONE)  # NOWE → ZAKONCZONE niedozwolone

    def test_garbage_status(self, ticket):
        with pytest.raises(ValidationError):
            services.change_ticket_status(ticket, "BZDURA")

    def test_cancelled_is_terminal(self, ticket):
        services.change_ticket_status(ticket, TicketStatus.ANULOWANE)
        assert TICKET_LEGAL_TRANSITIONS[TicketStatus.ANULOWANE] == set()
        with pytest.raises(ValidationError):
            services.change_ticket_status(ticket, TicketStatus.W_TOKU)

    def test_done_notifies_reporter(self, ticket, salesperson, service_user):
        services.change_ticket_status(ticket, TicketStatus.W_TOKU, user=service_user)
        services.change_ticket_status(ticket, TicketStatus.ZAKONCZONE, user=service_user)
        assert Notification.objects.filter(user=salesperson, title__contains="Zakończono").exists()

    def test_assign_rejects_other_org(self, ticket, other_org_user):
        with pytest.raises(ValidationError):
            services.assign_ticket(ticket, other_org_user)


# ---------------------------------------------------------------- części

class TestPartOrders:
    def test_create_part_order_moves_new_ticket_to_waiting(self, org, ticket):
        services.create_part_order(org, customer=ticket.customer, ticket=ticket, part_name="Głowica liniowa")
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.CZEKA_NA_CZESC

    def test_create_part_order_moves_in_progress_ticket_to_waiting(self, org, ticket):
        services.change_ticket_status(ticket, TicketStatus.W_TOKU)
        services.create_part_order(org, customer=ticket.customer, ticket=ticket, part_name="Kabel")
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.CZEKA_NA_CZESC

    def test_create_part_order_does_not_touch_closed_ticket(self, org, ticket):
        services.change_ticket_status(ticket, TicketStatus.W_TOKU)
        services.change_ticket_status(ticket, TicketStatus.ZAKONCZONE)
        services.create_part_order(org, customer=ticket.customer, ticket=ticket, part_name="Kabel")
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.ZAKONCZONE

    def test_part_order_without_ticket(self, org, customer):
        order = services.create_part_order(org, customer=customer, part_name="Bateria", quantity=2)
        assert order.status == PartOrderStatus.DO_ZAMOWIENIA and order.quantity == 2

    def test_timestamps_and_delivery_notification(self, org, ticket, service_user, office_user):
        services.assign_ticket(ticket, service_user)
        order = services.create_part_order(org, customer=ticket.customer, ticket=ticket, part_name="Głowica")
        services.change_part_order_status(order, PartOrderStatus.ZAMOWIONE, user=office_user,
                                          expected_at=days(7))
        assert order.ordered_at is not None and order.expected_at == days(7)
        before = Notification.objects.filter(user=service_user, title__contains="Dotarła część").count()
        services.change_part_order_status(order, PartOrderStatus.DOSTARCZONE, user=office_user)
        assert order.delivered_at is not None
        assert Notification.objects.filter(user=service_user, title__contains="Dotarła część").count() == before + 1
        services.change_part_order_status(order, PartOrderStatus.ZAMONTOWANE, user=service_user)
        order.refresh_from_db()
        assert order.installed_at is not None and order.status == PartOrderStatus.ZAMONTOWANE

    def test_delivery_notifies_reporter_when_no_assignee(self, org, ticket, salesperson, office_user):
        order = services.create_part_order(org, customer=ticket.customer, ticket=ticket, part_name="Głowica")
        services.change_part_order_status(order, PartOrderStatus.ZAMOWIONE)
        services.change_part_order_status(order, PartOrderStatus.DOSTARCZONE, user=office_user)
        assert Notification.objects.filter(user=salesperson, title__contains="Dotarła część").exists()

    def test_illegal_part_transition(self, org, customer):
        order = services.create_part_order(org, customer=customer, part_name="X")
        with pytest.raises(ValidationError):
            services.change_part_order_status(order, PartOrderStatus.ZAMONTOWANE)
        assert PART_ORDER_LEGAL_TRANSITIONS[PartOrderStatus.ZAMONTOWANE] == set()


# ---------------------------------------------------------------- przeglądy

class TestInspections:
    def test_complete_inspection_updates_device(self, org, device, service_user):
        inspection = Inspection.objects.create(organization=org, device=device, due_date=device.next_inspection_due)
        performed = days(-1)
        services.complete_inspection(inspection, performed, service_user, notes="OK")
        inspection.refresh_from_db()
        device.refresh_from_db()
        assert inspection.status == InspectionStatus.WYKONANY
        assert inspection.performed_by == service_user and inspection.notes == "OK"
        assert device.last_inspection_at == performed
        assert device.next_inspection_due == performed + relativedelta(months=12)

    def test_complete_inspection_month_end(self, org, customer, service_user):
        dev = Device.objects.create(organization=org, customer=customer, name="X", inspection_interval_months=6)
        inspection = Inspection.objects.create(organization=org, device=dev, due_date=datetime.date(2026, 8, 31))
        services.complete_inspection(inspection, datetime.date(2026, 8, 31), service_user)
        dev.refresh_from_db()
        assert dev.next_inspection_due == datetime.date(2027, 2, 28)

    def test_complete_with_protocol(self, org, device, service_user):
        inspection = Inspection.objects.create(organization=org, device=device, due_date=TODAY)
        protocol = SimpleUploadedFile("protokol.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        services.complete_inspection(inspection, TODAY, service_user, protocol=protocol)
        inspection.refresh_from_db()
        assert inspection.protocol.name.endswith(".pdf")

    def test_cannot_complete_twice(self, org, device, service_user):
        inspection = Inspection.objects.create(organization=org, device=device, due_date=TODAY)
        services.complete_inspection(inspection, TODAY, service_user)
        with pytest.raises(ValidationError):
            services.complete_inspection(inspection, TODAY, service_user)

    def test_cannot_complete_in_future(self, org, device, service_user):
        inspection = Inspection.objects.create(organization=org, device=device, due_date=TODAY)
        with pytest.raises(ValidationError):
            services.complete_inspection(inspection, days(3), service_user)

    def test_older_inspection_does_not_move_dates_back(self, org, device, service_user):
        device.last_inspection_at = days(-10)
        device.next_inspection_due = days(355)
        device.save()
        inspection = Inspection.objects.create(organization=org, device=device, due_date=days(-400))
        services.complete_inspection(inspection, days(-400), service_user)
        device.refresh_from_db()
        assert device.last_inspection_at == days(-10)

    def test_complete_closes_reminder_task(self, org, device, office_user, service_user):
        inspection = Inspection.objects.create(organization=org, device=device, due_date=days(10))
        service_tasks.plan_inspection_reminders(org)
        task = Task.objects.get(source_ref=f"inspection:{inspection.pk}")
        assert task.status == TaskStatus.PRZYPISANE
        services.complete_inspection(inspection, TODAY, service_user)
        task.refresh_from_db()
        assert task.status == TaskStatus.ZROBIONE

    def test_inspection_fsm(self, org, device):
        inspection = Inspection.objects.create(organization=org, device=device, due_date=TODAY)
        services.change_inspection_status(inspection, InspectionStatus.UMOWIONY, scheduled_for=days(3))
        assert inspection.scheduled_for == days(3)
        services.change_inspection_status(inspection, InspectionStatus.ANULOWANY)
        assert INSPECTION_LEGAL_TRANSITIONS[InspectionStatus.ANULOWANY] == set()
        with pytest.raises(ValidationError):
            services.change_inspection_status(inspection, InspectionStatus.ZAPLANOWANY)

    def test_ensure_upcoming_inspections_idempotent(self, org, customer, device):
        far = Device.objects.create(organization=org, customer=customer, name="Daleki", next_inspection_due=days(200))
        overdue = Device.objects.create(organization=org, customer=customer, name="Zaległy",
                                        next_inspection_due=days(-5))
        no_due = Device.objects.create(organization=org, customer=customer, name="Bez terminu")
        created = services.ensure_upcoming_inspections(org)
        assert {i.device_id for i in created} == {device.pk, overdue.pk}
        assert services.ensure_upcoming_inspections(org) == []
        assert not Inspection.objects.filter(device__in=[far, no_due]).exists()
        assert Inspection.objects.get(device=device).due_date == device.next_inspection_due

    def test_ensure_creates_new_after_completion(self, org, device, service_user):
        (inspection,) = services.ensure_upcoming_inspections(org)
        services.complete_inspection(inspection, TODAY, service_user)
        # następny termin za 12 miesięcy — poza horyzontem 60 dni
        assert services.ensure_upcoming_inspections(org) == []
        assert services.ensure_upcoming_inspections(org, horizon_days=400)

    def test_ensure_scoped_to_org(self, org, other_org_device):
        assert services.ensure_upcoming_inspections(org) == []

    def test_db_constraint_one_open_inspection(self, org, device):
        Inspection.objects.create(organization=org, device=device, due_date=TODAY)
        with pytest.raises(IntegrityError), transaction.atomic():
            Inspection.objects.create(organization=org, device=device, due_date=TODAY,
                                      status=InspectionStatus.UMOWIONY)

    def test_complete_device_inspection_adhoc(self, org, device, service_user):
        services.complete_device_inspection(device, TODAY, service_user)
        assert Inspection.objects.get(device=device).status == InspectionStatus.WYKONANY

    def test_initial_next_due(self, org, customer):
        dev = Device(organization=org, customer=customer, name="X", installed_at=datetime.date(2026, 1, 15),
                     inspection_interval_months=6)
        assert services.initial_next_due(dev) == datetime.date(2026, 7, 15)
        assert services.initial_next_due(Device(name="Y")) is None


# ---------------------------------------------------------------- zadania okresowe

class TestPeriodicTasks:
    def test_daily_planning_creates_reminder_tasks(self, org, device, customer, office_user, salesperson):
        later = Device.objects.create(organization=org, customer=customer, name="Za 50 dni",
                                      next_inspection_due=days(50))
        result = service_tasks.daily_inspection_planning()
        assert result["inspections"] >= 2 and result["reminders"] >= 1
        inspection = Inspection.objects.get(device=device)
        assert inspection.reminder_task_created
        task = Task.objects.get(source_ref=f"inspection:{inspection.pk}")
        assert task.kind == "PRZEGLAD" and task.source == "SYSTEM"
        assert task.assignee == office_user and task.customer == customer
        assert task.title == f"Umówić przegląd: {device.name} – {customer}"
        assert task.due_date == days(20 - 14)
        # przegląd za 50 dni — zaplanowany, ale bez przypomnienia (poza oknem 30 dni)
        later_inspection = Inspection.objects.get(device=later)
        assert not later_inspection.reminder_task_created
        # idempotencja
        service_tasks.daily_inspection_planning()
        assert Task.objects.filter(source_ref=f"inspection:{inspection.pk}").count() == 1

    def test_reminder_due_is_today_when_close(self, org, customer, office_user):
        Device.objects.create(organization=org, customer=customer, name="Pilny", next_inspection_due=days(3))
        service_tasks.daily_inspection_planning()
        assert Task.objects.get(kind="PRZEGLAD").due_date == TODAY

    def test_overdue_inspection_gets_reminder_today(self, org, customer, office_user):
        Device.objects.create(organization=org, customer=customer, name="Zaległy", next_inspection_due=days(-10))
        service_tasks.daily_inspection_planning()
        assert Task.objects.get(kind="PRZEGLAD").due_date == TODAY

    def test_reminder_without_office_user_is_unassigned(self, org, device):
        service_tasks.daily_inspection_planning()
        task = Task.objects.get(kind="PRZEGLAD")
        assert task.assignee is None and task.status == TaskStatus.NOWE

    def test_reminder_task_in_right_org(self, org, other_org, device, other_org_device, office_user):
        service_tasks.daily_inspection_planning()
        assert Task.objects.filter(organization=other_org, kind="PRZEGLAD").count() == 1
        assert Task.objects.filter(organization=org, kind="PRZEGLAD").count() == 1

    def test_warranty_alerts_once_per_device(self, org, device, salesperson):
        Device.objects.create(organization=org, customer=device.customer, name="Gwarancja dawno",
                              warranty_until=days(-5))
        Device.objects.create(organization=org, customer=device.customer, name="Gwarancja daleko",
                              warranty_until=days(200))
        assert service_tasks.warranty_expiring_alerts() == 1
        notes = Notification.objects.filter(user=salesperson)
        assert notes.count() == 1
        assert notes.get().title == "Kończy się gwarancja – szansa na ofertę serwisową/wymianę"
        assert service_tasks.warranty_expiring_alerts() == 0  # tego samego dnia
        with mock.patch("django.utils.timezone.localdate", return_value=days(1)):
            assert service_tasks.warranty_expiring_alerts() == 0  # i następnego też nie
        assert DailyAlertLog.objects.filter(key=f"warranty:{device.pk}").count() == 1

    def test_warranty_alert_skips_customer_without_owner(self, org):
        orphan = Customer.objects.create(organization=org, name="Bez opiekuna")
        Device.objects.create(organization=org, customer=orphan, name="X", warranty_until=days(10))
        assert service_tasks.warranty_expiring_alerts() == 0


# ---------------------------------------------------------------- oś czasu i rejestracje

class TestRegistrations:
    def test_timeline(self, org, ticket, device, service_user):
        services.complete_device_inspection(device, days(-2), service_user)
        entries = services.customer_timeline(ticket.customer)
        titles = [e["title"] for e in entries]
        assert any(ticket.number in t for t in titles)
        assert any(t.startswith("Przegląd wykonany") for t in titles)
        assert all(e["url"].startswith("/serwis/") for e in entries)

    def test_timeline_isolated(self, org, ticket, other_org_ticket):
        entries = services.customer_timeline(ticket.customer)
        assert not any("Obce" in e["title"] for e in entries)
        assert len(entries) == 1

    def test_registrations(self):
        from apps.service import registrations
        assert registrations.NAV[0]["url_name"] == "service:dashboard"
        assert registrations.NAV[0]["order"] == 40
        assert set(registrations.NAV[0]["roles"]) == {"SERWIS", "BIURO", "SZEF", "ADMIN"}
        tasks = {p["task"]: p["crontab"] for p in registrations.PERIODIC}
        assert tasks["apps.service.tasks.daily_inspection_planning"] == {"hour": 6, "minute": 15}
        assert tasks["apps.service.tasks.warranty_expiring_alerts"] == {"hour": 6, "minute": 30}
        assert registrations.TIMELINE == [services.customer_timeline]


# ---------------------------------------------------------------- widoki: uprawnienia i render

STAFF_PAGES = ["service:dashboard", "service:tickets", "service:parts", "service:device_create"]


class TestViewsAccess:
    @pytest.mark.parametrize("name", STAFF_PAGES + ["service:devices", "service:ticket_create"])
    def test_staff_pages_render(self, client, service_user, device, ticket, name):
        login(client, service_user)
        assert client.get(reverse(name)).status_code == 200

    @pytest.mark.parametrize("fixture", ["office_user", "boss"])
    def test_office_and_boss_see_dashboard(self, client, request, fixture, device, ticket):
        login(client, request.getfixturevalue(fixture))
        response = client.get(reverse("service:dashboard"))
        assert response.status_code == 200
        assert "Przeglądy po terminie" in response.content.decode()

    @pytest.mark.parametrize("name", STAFF_PAGES)
    def test_salesperson_blocked_from_staff_pages(self, client, salesperson, name):
        login(client, salesperson)
        assert client.get(reverse(name)).status_code == 403

    def test_anonymous_redirected(self, client):
        response = client.get(reverse("service:dashboard"))
        assert response.status_code == 302

    def test_dashboard_counts(self, client, service_user, org, customer, device, ticket):
        Device.objects.create(organization=org, customer=customer, name="Zaległy", next_inspection_due=days(-1))
        services.create_part_order(org, customer=customer, part_name="X")
        stats = services.dashboard_stats(org)
        assert stats == {"overdue": 1, "due_30": 1, "open_tickets": 1, "parts_to_order": 1,
                         "parts_in_transit": 0, "warranty_ending": 1}

    def test_salesperson_sees_only_own_devices(self, client, salesperson, org, device, foreign_customer):
        foreign = Device.objects.create(organization=org, customer=foreign_customer, name="Cudzy aparat")
        login(client, salesperson)
        body = client.get(reverse("service:devices")).content.decode()
        assert device.name in body and foreign.name not in body
        assert client.get(reverse("service:device_detail", args=[device.pk])).status_code == 200
        assert client.get(reverse("service:device_detail", args=[foreign.pk])).status_code == 404

    def test_salesperson_device_detail_has_no_complete_form(self, client, salesperson, device):
        login(client, salesperson)
        body = client.get(reverse("service:device_detail", args=[device.pk])).content.decode()
        assert "Przegląd wykonany" not in body and "Zgłoś usterkę" in body

    def test_salesperson_cannot_complete_or_change_status(self, client, salesperson, org, device, ticket):
        login(client, salesperson)
        assert client.post(reverse("service:inspection_complete", args=[device.pk]),
                           {"performed_at": TODAY.isoformat()}).status_code == 403
        assert client.post(reverse("service:ticket_status", args=[ticket.pk]),
                           {"status": "W_TOKU"}).status_code == 403

    def test_salesperson_creates_ticket_for_own_customer(self, client, salesperson, customer, device):
        login(client, salesperson)
        assert client.get(reverse("service:ticket_create") + f"?device={device.pk}").status_code == 200
        response = client.post(reverse("service:ticket_create"),
                               {"customer": customer.pk, "device": device.pk, "title": "Piszczy przy starcie"})
        ticket = ServiceTicket.objects.get(title="Piszczy przy starcie")
        assert response.status_code == 302 and response.url == reverse("service:ticket_detail", args=[ticket.pk])
        assert ticket.reported_by == salesperson and ticket.number.startswith("SRV-")
        assert client.get(response.url).status_code == 200

    def test_salesperson_cannot_create_ticket_for_foreign_customer(self, client, salesperson, foreign_customer):
        login(client, salesperson)
        response = client.post(reverse("service:ticket_create"), {"customer": foreign_customer.pk, "title": "x"})
        assert response.status_code == 200
        assert not ServiceTicket.objects.exists()

    def test_salesperson_cannot_see_foreign_ticket(self, client, salesperson, org, foreign_customer):
        foreign = services.create_ticket(org, customer=foreign_customer, title="cudze")
        login(client, salesperson)
        assert client.get(reverse("service:ticket_detail", args=[foreign.pk])).status_code == 404

    def test_ticket_create_device_customer_mismatch(self, client, service_user, foreign_customer, device):
        login(client, service_user)
        response = client.post(reverse("service:ticket_create"),
                               {"customer": foreign_customer.pk, "device": device.pk, "title": "x"})
        assert response.status_code == 200 and not ServiceTicket.objects.exists()


class TestViewsActions:
    def test_device_create_computes_next_due(self, client, office_user, customer):
        login(client, office_user)
        response = client.post(reverse("service:device_create"), {
            "customer": customer.pk, "name": "Kardiomonitor K5", "inspection_interval_months": 12,
            "installed_at": "2026-03-10",
        })
        device = Device.objects.get(name="Kardiomonitor K5")
        assert response.status_code == 302
        assert device.organization_id == office_user.organization_id
        assert device.next_inspection_due == datetime.date(2027, 3, 10)

    def test_device_create_rejects_foreign_org_customer(self, client, office_user, other_org_customer):
        login(client, office_user)
        response = client.post(reverse("service:device_create"), {
            "customer": other_org_customer.pk, "name": "X", "inspection_interval_months": 12})
        assert response.status_code == 200 and not Device.objects.filter(name="X").exists()

    def test_device_edit(self, client, service_user, device):
        login(client, service_user)
        url = reverse("service:device_edit", args=[device.pk])
        assert client.get(url).status_code == 200
        response = client.post(url, {"customer": device.customer_id, "name": "Nowa nazwa",
                                     "inspection_interval_months": 6,
                                     "next_inspection_due": device.next_inspection_due.isoformat()})
        assert response.status_code == 302
        device.refresh_from_db()
        assert device.name == "Nowa nazwa" and device.inspection_interval_months == 6

    def test_complete_inspection_view_with_protocol_and_download(self, client, service_user, salesperson, device):
        login(client, service_user)
        protocol = SimpleUploadedFile("protokol.pdf", b"%PDF-1.4 protokol", content_type="application/pdf")
        response = client.post(reverse("service:inspection_complete", args=[device.pk]),
                               {"performed_at": TODAY.isoformat(), "protocol": protocol, "notes": "Bez uwag"})
        assert response.status_code == 302
        inspection = Inspection.objects.get(device=device)
        assert inspection.status == InspectionStatus.WYKONANY
        device.refresh_from_db()
        assert device.last_inspection_at == TODAY
        url = reverse("service:inspection_protocol", args=[inspection.pk])
        download = client.get(url)
        assert download.status_code == 200
        assert download["X-Content-Type-Options"] == "nosniff"
        assert b"".join(download.streaming_content).startswith(b"%PDF")
        # właściciel klienta (handlowiec) też może pobrać, obcy handlowiec — nie
        login(client, salesperson)
        assert client.get(url).status_code == 200

    def test_protocol_forbidden_extension(self, client, service_user, device):
        login(client, service_user)
        bad = SimpleUploadedFile("evil.html", b"<script>", content_type="text/html")
        response = client.post(reverse("service:inspection_complete", args=[device.pk]),
                               {"performed_at": TODAY.isoformat(), "protocol": bad})
        assert response.status_code == 400
        assert not Inspection.objects.filter(device=device).exists()

    def test_complete_inspection_future_date_rejected(self, client, service_user, device):
        login(client, service_user)
        response = client.post(reverse("service:inspection_complete", args=[device.pk]),
                               {"performed_at": days(5).isoformat()})
        assert response.status_code == 400

    def test_protocol_404_without_file_and_for_foreign_salesperson(self, client, other_sales, org, device,
                                                                   service_user):
        inspection = Inspection.objects.create(organization=org, device=device, due_date=TODAY)
        login(client, service_user)
        assert client.get(reverse("service:inspection_protocol", args=[inspection.pk])).status_code == 404
        services.complete_inspection(inspection, TODAY, service_user,
                                     protocol=SimpleUploadedFile("p.pdf", b"%PDF", content_type="application/pdf"))
        login(client, other_sales)
        assert client.get(reverse("service:inspection_protocol", args=[inspection.pk])).status_code == 404

    def test_inspection_status_view(self, client, office_user, org, device):
        inspection = Inspection.objects.create(organization=org, device=device, due_date=days(10))
        login(client, office_user)
        response = client.post(reverse("service:inspection_status", args=[inspection.pk]),
                               {"status": "UMOWIONY", "scheduled_for": days(8).isoformat()})
        assert response.status_code == 302
        inspection.refresh_from_db()
        assert inspection.status == InspectionStatus.UMOWIONY and inspection.scheduled_for == days(8)
        # WYKONANY tylko przez formularz wykonania
        client.post(reverse("service:inspection_status", args=[inspection.pk]), {"status": "WYKONANY"})
        inspection.refresh_from_db()
        assert inspection.status == InspectionStatus.UMOWIONY

    def test_inspection_redirect(self, client, office_user, org, device):
        inspection = Inspection.objects.create(organization=org, device=device, due_date=days(10))
        login(client, office_user)
        response = client.get(reverse("service:inspection_detail", args=[inspection.pk]))
        assert response.url == reverse("service:device_detail", args=[device.pk])

    def test_ticket_status_buttons(self, client, service_user, ticket):
        login(client, service_user)
        body = client.get(reverse("service:ticket_detail", args=[ticket.pk])).content.decode()
        assert 'value="W_TOKU"' in body
        response = client.post(reverse("service:ticket_status", args=[ticket.pk]), {"status": "W_TOKU"})
        assert response.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.W_TOKU
        client.post(reverse("service:ticket_status", args=[ticket.pk]), {"status": "NOWE"})  # niedozwolone
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.W_TOKU

    def test_ticket_status_next_must_be_safe(self, client, service_user, ticket):
        login(client, service_user)
        response = client.post(reverse("service:ticket_status", args=[ticket.pk]),
                               {"status": "W_TOKU", "next": "https://zlosliwa.example/"})
        assert response.url == reverse("service:ticket_detail", args=[ticket.pk])

    def test_ticket_assign(self, client, office_user, service_user, salesperson, ticket):
        login(client, office_user)
        client.post(reverse("service:ticket_assign", args=[ticket.pk]), {"assignee": service_user.pk})
        ticket.refresh_from_db()
        assert ticket.assignee == service_user
        # handlowiec nie jest serwisantem
        assert client.post(reverse("service:ticket_assign", args=[ticket.pk]),
                           {"assignee": salesperson.pk}).status_code == 404

    def test_part_order_add_and_status_from_parts_board(self, client, service_user, ticket):
        login(client, service_user)
        response = client.post(reverse("service:part_order_add", args=[ticket.pk]),
                               {"part_name": "Głowica convex", "quantity": 1})
        assert response.status_code == 302
        order = PartOrder.objects.get(ticket=ticket)
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.CZEKA_NA_CZESC
        board = client.get(reverse("service:parts")).content.decode()
        assert "Głowica convex" in board
        response = client.post(reverse("service:part_order_status", args=[order.pk]),
                               {"status": "ZAMOWIONE", "expected_at": days(5).isoformat(), "next": "/serwis/czesci/"})
        assert response.url == "/serwis/czesci/"
        order.refresh_from_db()
        assert order.status == PartOrderStatus.ZAMOWIONE and order.expected_at == days(5)

    def test_part_order_add_invalid(self, client, service_user, ticket):
        login(client, service_user)
        response = client.post(reverse("service:part_order_add", args=[ticket.pk]), {"part_name": "", "quantity": 0})
        assert response.status_code == 400 and not PartOrder.objects.exists()

    def test_ticket_list_status_filter(self, client, service_user, org, customer, ticket):
        closed = services.create_ticket(org, customer=customer, title="Anulowane zgłoszenie")
        services.change_ticket_status(closed, TicketStatus.ANULOWANE)
        login(client, service_user)
        body = client.get(reverse("service:tickets")).content.decode()
        assert ticket.title in body and closed.title not in body
        body = client.get(reverse("service:tickets") + "?status=ANULOWANE").content.decode()
        assert closed.title in body and ticket.title not in body
        assert client.get(reverse("service:tickets") + "?status=<x>").status_code == 200

    def test_device_list_filters(self, client, service_user, org, customer, foreign_customer, device):
        overdue = Device.objects.create(organization=org, customer=foreign_customer, name="Zaległy aparat",
                                        next_inspection_due=days(-3))
        login(client, service_user)
        body = client.get(reverse("service:devices") + "?overdue=1").content.decode()
        assert overdue.name in body and device.name not in body
        body = client.get(reverse("service:devices") + f"?customer={customer.pk}").content.decode()
        assert device.name in body and overdue.name not in body
        assert client.get(reverse("service:devices") + "?customer=١٢").status_code == 200


class TestOrgIsolation:
    def test_foreign_objects_404(self, client, service_user, other_org_device, other_org_ticket, other_org):
        inspection = Inspection.objects.create(organization=other_org, device=other_org_device, due_date=TODAY)
        order = services.create_part_order(other_org, customer=other_org_ticket.customer, ticket=other_org_ticket,
                                           part_name="Obca część")
        login(client, service_user)
        assert client.get(reverse("service:device_detail", args=[other_org_device.pk])).status_code == 404
        assert client.get(reverse("service:device_edit", args=[other_org_device.pk])).status_code == 404
        assert client.get(reverse("service:ticket_detail", args=[other_org_ticket.pk])).status_code == 404
        assert client.post(reverse("service:ticket_status", args=[other_org_ticket.pk]),
                           {"status": "W_TOKU"}).status_code == 404
        assert client.post(reverse("service:ticket_assign", args=[other_org_ticket.pk]), {}).status_code == 404
        assert client.post(reverse("service:part_order_add", args=[other_org_ticket.pk]),
                           {"part_name": "x", "quantity": 1}).status_code == 404
        assert client.post(reverse("service:part_order_status", args=[order.pk]),
                           {"status": "ZAMOWIONE"}).status_code == 404
        assert client.post(reverse("service:inspection_complete", args=[other_org_device.pk]),
                           {"performed_at": TODAY.isoformat()}).status_code == 404
        assert client.post(reverse("service:inspection_status", args=[inspection.pk]),
                           {"status": "UMOWIONY"}).status_code == 404
        assert client.get(reverse("service:inspection_protocol", args=[inspection.pk])).status_code == 404
        other_org_ticket.refresh_from_db()
        assert other_org_ticket.status == TicketStatus.CZEKA_NA_CZESC

    def test_lists_do_not_leak(self, client, service_user, device, ticket, other_org_device, other_org_ticket):
        login(client, service_user)
        assert other_org_device.name not in client.get(reverse("service:devices")).content.decode()
        assert other_org_ticket.title not in client.get(reverse("service:tickets")).content.decode()
        dashboard = client.get(reverse("service:dashboard"))
        assert dashboard.context["stats"]["open_tickets"] == 1
        assert other_org_device.name not in dashboard.content.decode()

    def test_user_without_org_gets_403(self, client, db):
        orphan = User.objects.create_user(username="sierota", password="haslo-testowe-123", role=Role.SERWIS)
        login(client, orphan)
        assert client.get(reverse("service:dashboard")).status_code == 403


def test_staff_device_detail_with_open_inspection_renders(client, service_user, org, device, ticket):
    Inspection.objects.create(organization=org, device=device, due_date=device.next_inspection_due)
    login(client, service_user)
    body = client.get(reverse("service:device_detail", args=[device.pk])).content.decode()
    assert "Przegląd wykonany" in body and 'value="UMOWIONY"' in body and ticket.number in body


# ---------------------------------------------------------------- regresje: anulowanie i przypomnienia przeglądów

def _reminder(inspection):
    return Task.objects.get(source_ref=f"inspection:{inspection.pk}")


class TestInspectionReminderLifecycle:
    def test_cancel_is_not_recreated_and_closes_reminder(self, org, device, office_user, service_user):
        service_tasks.daily_inspection_planning()
        inspection = Inspection.objects.get(device=device)
        services.change_inspection_status(inspection, InspectionStatus.ANULOWANY, user=service_user)
        assert _reminder(inspection).status == TaskStatus.ZROBIONE
        service_tasks.daily_inspection_planning()
        assert list(Inspection.objects.filter(device=device).values_list("status", flat=True)) == [
            InspectionStatus.ANULOWANY]
        assert Task.objects.filter(kind="PRZEGLAD", status__in=("NOWE", "PRZYPISANE", "W_TOKU")).count() == 0

    def test_new_due_date_after_cancel_plans_again(self, org, device, office_user, service_user):
        service_tasks.daily_inspection_planning()
        inspection = Inspection.objects.get(device=device)
        services.change_inspection_status(inspection, InspectionStatus.ANULOWANY, user=service_user)
        Device.objects.filter(pk=device.pk).update(next_inspection_due=days(25))
        service_tasks.daily_inspection_planning()
        assert Inspection.objects.filter(device=device, status=InspectionStatus.ZAPLANOWANY,
                                         due_date=days(25)).count() == 1

    @pytest.mark.parametrize("initial", [TaskStatus.NOWE, TaskStatus.ODLOZONE, TaskStatus.PRZYPISANE])
    def test_reminder_closed_on_complete_from_any_open_status(self, org, device, service_user, initial):
        service_tasks.daily_inspection_planning()  # brak biura → zadanie NOWE (bez wykonawcy)
        inspection = Inspection.objects.get(device=device)
        Task.objects.filter(source_ref=f"inspection:{inspection.pk}").update(status=initial)
        services.complete_device_inspection(device, days(-1), service_user)
        assert _reminder(inspection).status == TaskStatus.ZROBIONE

    def test_booking_closes_reminder_and_unbooking_recreates(self, org, device, office_user, service_user):
        service_tasks.daily_inspection_planning()
        inspection = Inspection.objects.get(device=device)
        services.change_inspection_status(inspection, InspectionStatus.UMOWIONY, user=office_user,
                                          scheduled_for=days(10))
        assert _reminder(inspection).status == TaskStatus.ZROBIONE
        services.change_inspection_status(inspection, InspectionStatus.ZAPLANOWANY, user=office_user)
        inspection.refresh_from_db()
        assert not inspection.reminder_task_created
        service_tasks.daily_inspection_planning()
        assert Task.objects.filter(source_ref=f"inspection:{inspection.pk}", status=TaskStatus.PRZYPISANE).count() == 1


class TestAdminTicketNumber:
    def test_admin_add_assigns_number(self, client, org, customer):
        admin = User.objects.create_user(username="root_t", password="haslo-testowe-123", organization=org,
                                         role=Role.ADMIN, is_staff=True, is_superuser=True)
        existing = services.create_ticket(org, customer=customer, title="Pierwsze")
        client.force_login(admin)
        resp = client.post(reverse("admin:service_serviceticket_add"), {
            "organization": org.pk, "customer": customer.pk, "title": "Z panelu admina", "description": "",
            "partorder_set-TOTAL_FORMS": "0", "partorder_set-INITIAL_FORMS": "0",
            "parts-TOTAL_FORMS": "0", "parts-INITIAL_FORMS": "0",
            "part_orders-TOTAL_FORMS": "0", "part_orders-INITIAL_FORMS": "0",
        })
        assert resp.status_code == 302, resp.content.decode()[:2000]
        ticket = ServiceTicket.objects.get(title="Z panelu admina")
        year = timezone.localdate().year
        assert existing.number == f"SRV-{year}-0001"
        assert ticket.number == f"SRV-{year}-0002"

    def test_assign_ticket_number_keeps_existing(self, org, ticket):
        number = ticket.number
        assert services.assign_ticket_number(ticket).number == number
