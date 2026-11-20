"""Zadania okresowe powiadomień: poranny skrót dnia i eskalacja zaległych zadań do szefa."""
import datetime
import logging
from collections import defaultdict

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.users.models import Role, User

from .services import mark_tasks_escalated, notify, once_per_day, reverse_or

logger = logging.getLogger(__name__)

ESCALATION_GRACE_DAYS = 1  # eskalujemy zadania z terminem < dziś - 1 dzień


def _absolute(path):
    return f"{getattr(settings, 'SITE_URL', '').rstrip('/')}{path}"


def _task_line(task, today):
    if task.due_date < today:
        when = f"ZALEGŁE, termin {task.due_date:%d.%m.%Y}"
    else:
        when = "na dziś"
    customer = f" – {task.customer}" if task.customer_id else ""
    return f"- [{when}] {task.title}{customer}"


def digest_text(user, tasks, today):
    overdue = sum(1 for t in tasks if t.due_date < today)
    lines = [
        f"Dzień dobry {user.first_name or user.username},",
        "",
        f"Twoje otwarte zadania na {today:%d.%m.%Y}: {len(tasks)} (w tym zaległe: {overdue}).",
        "",
        *[_task_line(t, today) for t in tasks],
        "",
        f"Lista zadań: {_absolute(reverse_or('tasks:mine'))}",
        "",
        "— TinyCRM (wiadomość automatyczna)",
    ]
    return "\n".join(lines)


def _digest_candidates(today):
    """Otwarte zadania na dziś/zaległe, pogrupowane po aktywnym wykonawcy z tej samej organizacji."""
    from apps.tasks.models import OPEN_STATUSES, Task

    tasks = (Task.objects.filter(status__in=OPEN_STATUSES, due_date__lte=today, assignee__is_active=True,
                                 assignee__organization=F("organization"))
             .select_related("assignee", "customer")
             .order_by("assignee_id", "due_date", "-priority", "pk"))
    grouped = defaultdict(list)
    for task in tasks:
        grouped[task.assignee].append(task)
    return grouped


def send_digest_for_user(user, tasks, today):
    """Jeden e-mail + jedno powiadomienie na użytkownika dziennie. Zwraca True, gdy wysłano."""
    if not tasks or not once_per_day(f"digest:{user.pk}", day=today):
        return False
    overdue = sum(1 for t in tasks if t.due_date < today)
    title = f"Na dziś: {len(tasks)} zadań" + (f" (zaległe: {overdue})" if overdue else "")
    body = digest_text(user, tasks, today)
    if user.email:
        try:
            send_mail(f"TinyCRM – {title}", body, settings.DEFAULT_FROM_EMAIL, [user.email])
        except Exception:  # noqa: BLE001 — błąd SMTP nie blokuje powiadomienia w aplikacji
            logger.exception("Skrót dnia: nie wysłano e-maila do użytkownika %s", user.pk)
    notify(user, title, "\n".join(_task_line(t, today) for t in tasks[:10]), url=reverse_or("tasks:mine"))
    return True


@shared_task
def morning_digest(today=None):
    today = today or timezone.localdate()
    sent = 0
    for user, tasks in _digest_candidates(today).items():
        if send_digest_for_user(user, tasks, today):
            sent += 1
    logger.info("Skrót dnia: wysłano %s", sent)
    return sent


def escalation_candidates(today):
    from apps.tasks.models import OPEN_STATUSES, Task

    cutoff = today - datetime.timedelta(days=ESCALATION_GRACE_DAYS)
    return (Task.objects.filter(status__in=OPEN_STATUSES, due_date__lt=cutoff, escalated_at__isnull=True)
            .select_related("assignee", "customer", "organization").order_by("organization_id", "due_date", "pk"))


def _escalation_body(task, today):
    days = (today - task.due_date).days
    who = str(task.assignee) if task.assignee_id else "nieprzypisane"
    customer = f" Klient: {task.customer}." if task.customer_id else ""
    return f"Termin {task.due_date:%d.%m.%Y} (opóźnienie {days} dni). Wykonawca: {who}.{customer}"


@shared_task
def overdue_escalation(today=None):
    """Każde zadanie zaległe > 1 dzień eskalujemy RAZ: powiadomienie do każdego SZEF-a tej organizacji."""
    from apps.tasks.models import Task

    today = today or timezone.localdate()
    bosses_by_org = {}
    escalated = 0
    for task in escalation_candidates(today):
        if task.organization_id not in bosses_by_org:
            bosses_by_org[task.organization_id] = list(
                User.objects.filter(organization_id=task.organization_id, role=Role.SZEF, is_active=True))
        bosses = bosses_by_org[task.organization_id]
        if not bosses:
            continue  # brak szefa w organizacji — spróbujemy jutro
        with transaction.atomic():
            if not mark_tasks_escalated(Task.objects.filter(pk=task.pk)):
                continue  # równoległy przebieg już eskalował
            for boss in bosses:
                notify(boss, f"Zaległe zadanie: {task.title}"[:200], _escalation_body(task, today),
                       url=reverse_or("tasks:detail", args=[task.pk]))
        escalated += 1
    logger.info("Eskalacje zaległych zadań: %s", escalated)
    return escalated
