"""Publiczne API modułu serwisu. Inne moduły (np. voice) tworzą zgłoszenia i zamówienia części tędy.

Statusy zmieniamy wyłącznie funkcjami `change_*_status` walidowanymi tabelami `*_LEGAL_TRANSITIONS`.
"""
import datetime
import logging

from dateutil.relativedelta import relativedelta
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.users.models import Role

from .models import (
    INSPECTION_LEGAL_TRANSITIONS,
    OPEN_INSPECTION_STATUSES,
    OPEN_TICKET_STATUSES,
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

logger = logging.getLogger(__name__)

TICKET_PREFIX = "SRV"
INSPECTION_PLANNING_HORIZON_DAYS = 60
WARRANTY_ALERT_DAYS = 60


# --- adresy (zgodne z kontraktem URL; bez reverse, żeby serwisy działały też poza requestem) ---

def ticket_url(ticket):
    return f"/serwis/zgloszenia/{ticket.pk}/"


def device_url(device):
    return f"/serwis/urzadzenia/{device.pk}/"


def _notify(user, title, body="", url=""):
    """Powiadomienie best-effort — moduł notify jest opcjonalny, błąd pushu nie psuje operacji."""
    if user is None:
        return None
    try:
        from apps.notify.services import notify
    except ImportError:  # pragma: no cover
        return None
    return notify(user, title, body, url=url)


# --- numeracja zgłoszeń ---

def _number_suffix(number):
    """Int z końcówki `SRV-YYYY-NNNN`; None, gdy końcówka nie jest liczbą ASCII."""
    suffix = (number or "").rsplit("-", 1)[-1]
    if suffix.isascii() and suffix.isdigit():
        return int(suffix)
    return None


def _next_ticket_number(organization):
    year = timezone.localdate().year
    prefix = f"{TICKET_PREFIX}-{year}-"
    numbers = ServiceTicket.objects.for_org(organization).filter(number__startswith=prefix).values_list("number",
                                                                                                         flat=True)
    # Sortuj po int suffix, nie po stringu (9999 < 10000).
    last = max((n for n in (_number_suffix(x) for x in numbers) if n is not None), default=0)
    return f"{prefix}{last + 1:04d}"


def assign_ticket_number(ticket):
    """Nadaj numer `SRV-YYYY-NNNN` zgłoszeniu bez numeru (np. dodanemu w panelu admina) pod blokadą organizacji.

    Wywołujący zapisuje zgłoszenie w TEJ SAMEJ zewnętrznej transakcji (blokada trwa do jej końca).
    """
    if ticket.number:
        return ticket
    with transaction.atomic():
        organization = type(ticket.organization).objects.select_for_update().get(pk=ticket.organization_id)
        ticket.number = _next_ticket_number(organization)
    return ticket


# --- zgłoszenia ---

@transaction.atomic
def create_ticket(organization, *, customer, title, description="", device=None, reported_by=None,
                  is_warranty=False, source_ref="", assignee=None):
    if customer.organization_id != organization.pk:
        raise ValidationError("Klient spoza organizacji.")
    if device is not None and (device.organization_id != organization.pk or device.customer_id != customer.pk):
        raise ValidationError("Urządzenie nie należy do tego klienta.")
    # Blokada organizacji serializuje numerację równoległych zgłoszeń.
    type(organization).objects.select_for_update().filter(pk=organization.pk).first()
    ticket = ServiceTicket.objects.create(
        organization=organization, number=_next_ticket_number(organization), customer=customer, device=device,
        title=title[:255], description=description, reported_by=reported_by, is_warranty=is_warranty,
        source_ref=source_ref, assignee=assignee,
    )
    if assignee is not None and assignee != reported_by:
        _notify(assignee, f"Nowe zgłoszenie serwisowe {ticket.number}", ticket.title, url=ticket_url(ticket))
    return ticket


def change_ticket_status(ticket, new_status, *, user=None):
    allowed = TICKET_LEGAL_TRANSITIONS.get(ticket.status, set())
    if new_status not in allowed:
        raise ValidationError(f"Niedozwolone przejście {ticket.status} → {new_status}")
    ticket.status = new_status
    ticket.save(update_fields=["status", "updated_at"])
    if new_status == TicketStatus.ZAKONCZONE and ticket.reported_by_id and ticket.reported_by != user:
        _notify(ticket.reported_by, f"Zakończono zgłoszenie {ticket.number}", ticket.title, url=ticket_url(ticket))
    return ticket


def assign_ticket(ticket, assignee, *, user=None):
    if assignee is not None and (assignee.organization_id != ticket.organization_id or not assignee.is_active):
        raise ValidationError("Serwisant spoza organizacji albo nieaktywny.")
    previous = ticket.assignee_id
    ticket.assignee = assignee
    ticket.save(update_fields=["assignee", "updated_at"])
    if assignee is not None and assignee.pk != previous and assignee != user:
        _notify(assignee, f"Przypisano Ci zgłoszenie {ticket.number}", ticket.title, url=ticket_url(ticket))
    return ticket


# --- zamówienia części ---

@transaction.atomic
def create_part_order(organization, *, customer, part_name, quantity=1, ticket=None, part_number="", supplier="",
                      expected_at=None):
    if ticket is not None and ticket.organization_id != organization.pk:
        raise ValidationError("Zgłoszenie spoza organizacji.")
    order = PartOrder.objects.create(
        organization=organization, customer=customer, ticket=ticket, part_name=part_name[:200],
        quantity=quantity, part_number=part_number, supplier=supplier, expected_at=expected_at,
    )
    # Zgłoszenie w realizacji czeka teraz na część.
    if ticket is not None and ticket.status in (TicketStatus.NOWE, TicketStatus.W_TOKU):
        change_ticket_status(ticket, TicketStatus.CZEKA_NA_CZESC)
    return order


_PART_ORDER_TIMESTAMPS = {
    PartOrderStatus.ZAMOWIONE: "ordered_at",
    PartOrderStatus.DOSTARCZONE: "delivered_at",
    PartOrderStatus.ZAMONTOWANE: "installed_at",
}


@transaction.atomic
def change_part_order_status(order, new_status, *, user=None, expected_at=None):
    allowed = PART_ORDER_LEGAL_TRANSITIONS.get(order.status, set())
    if new_status not in allowed:
        raise ValidationError(f"Niedozwolone przejście {order.status} → {new_status}")
    order.status = new_status
    fields = ["status", "updated_at"]
    stamp = _PART_ORDER_TIMESTAMPS.get(new_status)
    if stamp:
        setattr(order, stamp, timezone.now())
        fields.append(stamp)
    if expected_at is not None:
        order.expected_at = expected_at
        fields.append("expected_at")
    order.save(update_fields=fields)
    if new_status == PartOrderStatus.DOSTARCZONE and order.ticket_id:
        ticket = order.ticket
        recipient = ticket.assignee or ticket.reported_by
        if recipient is not None and recipient != user:
            _notify(recipient, f"Dotarła część: {order.part_name}",
                    f"{ticket.number} · {ticket.customer} — można umawiać naprawę.", url=ticket_url(ticket))
    return order


# --- urządzenia i przeglądy ---

def compute_next_due(from_date, interval_months):
    if not from_date:
        return None
    return from_date + relativedelta(months=interval_months or 12)


def initial_next_due(device):
    """Pierwszy termin przeglądu dla nowego urządzenia: od ostatniego przeglądu, a gdy brak — od instalacji."""
    base = device.last_inspection_at or device.installed_at
    return compute_next_due(base, device.inspection_interval_months)


def open_inspection(device):
    return Inspection.objects.filter(device=device, organization_id=device.organization_id,
                                     status__in=OPEN_INSPECTION_STATUSES).first()


@transaction.atomic
def change_inspection_status(inspection, new_status, *, user=None, scheduled_for=None):
    """Umówienie / cofnięcie do planu / anulowanie. Wykonanie przeglądu idzie przez `complete_inspection`.

    - UMOWIONY: przegląd umówiony → zadanie „Umówić przegląd” jest załatwione (odhaczamy je);
    - ANULOWANY: odhaczamy przypomnienie; termin urządzenia zostaje, ale planowanie nie odtworzy przeglądu
      na ten sam termin (patrz `ensure_upcoming_inspections`) — nowy powstanie po zmianie terminu urządzenia;
    - UMOWIONY → ZAPLANOWANY (wizyta odwołana): przypomnienie zostanie utworzone ponownie.
    """
    allowed = INSPECTION_LEGAL_TRANSITIONS.get(inspection.status, set())
    if new_status not in allowed:
        raise ValidationError(f"Niedozwolone przejście {inspection.status} → {new_status}")
    if new_status == InspectionStatus.WYKONANY:
        return complete_inspection(inspection, timezone.localdate(), user)
    inspection.status = new_status
    fields = ["status", "updated_at"]
    if scheduled_for is not None or new_status == InspectionStatus.UMOWIONY:
        inspection.scheduled_for = scheduled_for
        fields.append("scheduled_for")
    if new_status == InspectionStatus.ZAPLANOWANY and inspection.reminder_task_created:
        inspection.reminder_task_created = False
        fields.append("reminder_task_created")
    inspection.save(update_fields=fields)
    if new_status in (InspectionStatus.UMOWIONY, InspectionStatus.ANULOWANY):
        _close_reminder_task(inspection, user)
    return inspection


def _close_reminder_task(inspection, user):
    """Odhacz automatyczne zadanie „Umówić przegląd” (legalną ścieżką FSM zadań, przez serwis modułu tasks)."""
    try:
        from apps.tasks.services import close_system_tasks
    except ImportError:  # pragma: no cover
        return 0
    try:
        return close_system_tasks(inspection.organization, f"inspection:{inspection.pk}", user=user)
    except ValidationError:
        logger.warning("Nie udało się zamknąć przypomnienia przeglądu %s", inspection.pk)
        return 0


@transaction.atomic
def complete_inspection(inspection, performed_at, user, protocol=None, notes=""):
    """Przegląd wykonany: status WYKONANY + aktualizacja terminów urządzenia (ostatni / następny)."""
    if InspectionStatus.WYKONANY not in INSPECTION_LEGAL_TRANSITIONS.get(inspection.status, set()):
        raise ValidationError(f"Niedozwolone przejście {inspection.status} → {InspectionStatus.WYKONANY}")
    if isinstance(performed_at, datetime.datetime):
        performed_at = timezone.localdate(performed_at) if timezone.is_aware(performed_at) else performed_at.date()
    if performed_at > timezone.localdate():
        raise ValidationError("Data wykonania przeglądu nie może być z przyszłości.")
    inspection.status = InspectionStatus.WYKONANY
    inspection.performed_at = performed_at
    inspection.performed_by = user
    if protocol:
        inspection.protocol = protocol
    if notes:
        inspection.notes = notes
    inspection.save()

    device = Device.objects.select_for_update().get(pk=inspection.device_id)
    if device.last_inspection_at is None or performed_at >= device.last_inspection_at:
        device.last_inspection_at = performed_at
        device.next_inspection_due = compute_next_due(performed_at, device.inspection_interval_months)
        device.save(update_fields=["last_inspection_at", "next_inspection_due", "updated_at"])
    inspection.device = device
    _close_reminder_task(inspection, user)
    return inspection


def complete_device_inspection(device, performed_at, user, protocol=None, notes=""):
    """Wykonanie przeglądu z karty urządzenia: zamyka otwarty przegląd albo tworzy doraźny."""
    with transaction.atomic():
        inspection = open_inspection(device)
        if inspection is None:
            inspection = Inspection.objects.create(
                organization_id=device.organization_id, device=device,
                due_date=device.next_inspection_due or performed_at,
            )
        return complete_inspection(inspection, performed_at, user, protocol=protocol, notes=notes)


def ensure_upcoming_inspections(organization, horizon_days=INSPECTION_PLANNING_HORIZON_DAYS, today=None):
    """Jeden otwarty przegląd na urządzenie z terminem w horyzoncie. Idempotentne. Zwraca listę utworzonych."""
    today = today or timezone.localdate()
    limit = today + datetime.timedelta(days=horizon_days)
    cancelled_same_due = Inspection.objects.filter(
        device=OuterRef("pk"), status=InspectionStatus.ANULOWANY, due_date=OuterRef("next_inspection_due"))
    devices = (Device.objects.for_org(organization)
               .filter(next_inspection_due__isnull=False, next_inspection_due__lte=limit)
               .exclude(inspections__status__in=OPEN_INSPECTION_STATUSES)
               # Anulowany przegląd na ten sam termin = świadoma decyzja; nie odtwarzamy go co rano.
               .exclude(Exists(cancelled_same_due)))
    created = []
    for device in devices:
        try:
            with transaction.atomic():
                created.append(Inspection.objects.create(organization=organization, device=device,
                                                         due_date=device.next_inspection_due))
        except IntegrityError:
            # Równoległe planowanie utworzyło już otwarty przegląd — ograniczenie w bazie pilnuje duplikatów.
            continue
    return created


# --- zapytania dla pulpitu i zadań okresowych ---

def dashboard_stats(organization, today=None):
    today = today or timezone.localdate()
    devices = Device.objects.for_org(organization)
    orders = PartOrder.objects.for_org(organization)
    return {
        "overdue": devices.filter(next_inspection_due__lt=today).count(),
        "due_30": devices.filter(next_inspection_due__gte=today,
                                 next_inspection_due__lte=today + datetime.timedelta(days=30)).count(),
        "open_tickets": ServiceTicket.objects.for_org(organization).filter(status__in=OPEN_TICKET_STATUSES).count(),
        "parts_to_order": orders.filter(status=PartOrderStatus.DO_ZAMOWIENIA).count(),
        "parts_in_transit": orders.filter(status=PartOrderStatus.ZAMOWIONE).count(),
        "warranty_ending": warranty_expiring_devices(organization, today=today).count(),
    }


def warranty_expiring_devices(organization, days=WARRANTY_ALERT_DAYS, today=None):
    today = today or timezone.localdate()
    return Device.objects.for_org(organization).filter(
        warranty_until__gte=today, warranty_until__lte=today + datetime.timedelta(days=days),
    )


# --- oś czasu klienta (kontrakt TIMELINE) ---

STAFF_ROLES = (Role.SERWIS, Role.BIURO, Role.SZEF, Role.ADMIN)


def is_staff(user):
    """Rola serwisu/biura: pełny wgląd w urządzenia i zgłoszenia organizacji."""
    return getattr(user, "role", None) in STAFF_ROLES


def customer_timeline(customer, user=None):
    """Wpisy osi czasu klienta. Przy `user` spoza serwisu/biura: tylko to, co widzi w module serwisu
    (zgłoszenia i urządzenia własnych klientów albo zgłoszenia przez niego zgłoszone)."""
    entries = []
    restricted = user is not None and not is_staff(user)
    uid = getattr(user, "pk", None)
    owns = customer.owner_id is not None and customer.owner_id == uid
    tickets = ServiceTicket.objects.for_org(customer.organization).filter(customer=customer)
    if restricted and not owns:
        tickets = tickets.filter(reported_by_id=uid)
    tickets = tickets.order_by("-created_at")[:50]
    for ticket in tickets:
        entries.append({
            "date": ticket.created_at,
            "icon": "🔧",
            "title": f"Zgłoszenie serwisowe {ticket.number}: {ticket.title}",
            "text": ticket.get_status_display(),
            "url": ticket_url(ticket),
        })
    inspections = (Inspection.objects.for_org(customer.organization)
                   .filter(device__customer=customer, status=InspectionStatus.WYKONANY, performed_at__isnull=False)
                   .select_related("device", "performed_by").order_by("-performed_at")[:50])
    if restricted and not owns:
        inspections = []
    for inspection in inspections:
        who = f" · {inspection.performed_by}" if inspection.performed_by_id else ""
        entries.append({
            "date": inspection.performed_at,
            "icon": "🩺",
            "title": f"Przegląd wykonany: {inspection.device}",
            "text": f"Następny do {inspection.device.next_inspection_due:%d.%m.%Y}{who}"
            if inspection.device.next_inspection_due else who.strip(" ·"),
            "url": device_url(inspection.device),
        })
    return entries
