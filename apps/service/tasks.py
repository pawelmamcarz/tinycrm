"""Zadania okresowe serwisu: planowanie przeglądów i alerty o kończącej się gwarancji."""
import datetime
import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.core.models import Organization
from apps.users.models import Role, User

from . import services
from .models import Inspection, InspectionStatus

logger = logging.getLogger(__name__)

REMINDER_WINDOW_DAYS = 30
REMINDER_LEAD_DAYS = 14


def _first_office_user(organization):
    return (User.objects.filter(organization=organization, role=Role.BIURO, is_active=True)
            .order_by("pk").first())


def plan_inspection_reminders(organization, today=None):
    """Zadanie „Umówić przegląd” dla otwartych przeglądów z terminem w 30 dni. Zwraca liczbę utworzonych."""
    from apps.tasks.services import create_task

    today = today or timezone.localdate()
    assignee = _first_office_user(organization)
    inspections = (Inspection.objects.for_org(organization)
                   .filter(status=InspectionStatus.ZAPLANOWANY, reminder_task_created=False,
                           due_date__lte=today + datetime.timedelta(days=REMINDER_WINDOW_DAYS))
                   .select_related("device__customer"))
    created = 0
    for inspection in inspections:
        device = inspection.device
        due = max(inspection.due_date - datetime.timedelta(days=REMINDER_LEAD_DAYS), today)
        with transaction.atomic():
            # Zablokuj wiersz, żeby równoległy przebieg nie utworzył drugiego zadania.
            locked = Inspection.objects.select_for_update().get(pk=inspection.pk)
            if locked.reminder_task_created:
                continue
            create_task(
                organization, title=f"Umówić przegląd: {device.name} – {device.customer}",
                description=(f"Termin przeglądu: {inspection.due_date:%d.%m.%Y}. "
                             f"Nr seryjny: {device.serial_number or '—'}. Lokalizacja: {device.location or '—'}."),
                assignee=assignee, customer=device.customer, kind="PRZEGLAD", source="SYSTEM",
                source_ref=f"inspection:{inspection.pk}", due_date=due,
            )
            locked.reminder_task_created = True
            locked.save(update_fields=["reminder_task_created", "updated_at"])
            created += 1
    return created


@shared_task
def daily_inspection_planning():
    summary = {"inspections": 0, "reminders": 0}
    for organization in Organization.objects.all():
        summary["inspections"] += len(services.ensure_upcoming_inspections(organization))
        summary["reminders"] += plan_inspection_reminders(organization)
    logger.info("Planowanie przeglądów: %s", summary)
    return summary


def warranty_alerts_for_org(organization, today=None):
    from apps.notify.services import once_per_day

    sent = 0
    devices = services.warranty_expiring_devices(organization, today=today).select_related("customer__owner")
    for device in devices:
        owner = device.customer.owner
        if owner is None or not owner.is_active:
            continue
        # Klucz + dzień końca gwarancji = jeden alert na urządzenie (przedłużenie gwarancji = nowy alert).
        with transaction.atomic():  # savepoint: once_per_day łapie IntegrityError
            first_time = once_per_day(f"warranty:{device.pk}", day=device.warranty_until)
        if not first_time:
            continue
        services._notify(
            owner, "Kończy się gwarancja – szansa na ofertę serwisową/wymianę",
            f"{device} u klienta {device.customer}: gwarancja do {device.warranty_until:%d.%m.%Y}.",
            url=services.device_url(device),
        )
        sent += 1
    return sent


@shared_task
def warranty_expiring_alerts():
    sent = sum(warranty_alerts_for_org(org) for org in Organization.objects.all())
    logger.info("Alerty gwarancyjne: %s", sent)
    return sent

