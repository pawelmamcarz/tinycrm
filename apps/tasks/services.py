"""Publiczne API modułu zadań — inne moduły tworzą/zmieniają zadania WYŁĄCZNIE tędy."""
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.utils import timezone

from apps.users.models import Role, User

from .models import OPEN_STATUSES, TASK_LEGAL_TRANSITIONS, Task, TaskComment, TaskSource, TaskStatus

# Role widzące kolejkę biura i mogące zmieniać status/przypisanie dowolnego zadania organizacji.
OFFICE_ROLES = (Role.BIURO, Role.SZEF, Role.ADMIN, Role.SERWIS)


def _notify(user, title, body="", url=""):
    """Powiadomienie best-effort — moduł notify jest opcjonalny."""
    if user is None:
        return
    try:
        from apps.notify.services import notify
    except ImportError:
        return
    notify(user, title, body, url=url)


def task_url(task):
    return f"/zadania/{task.pk}/"


def create_task(organization, *, title, created_by=None, assignee=None, customer=None, description="",
                kind="INNE", due_date=None, priority=2, source=TaskSource.RECZNE, source_ref=""):
    status = TaskStatus.PRZYPISANE if assignee else TaskStatus.NOWE
    task = Task.objects.create(
        organization=organization, title=title[:255], description=description, kind=kind, status=status,
        priority=priority, customer=customer, assignee=assignee, created_by=created_by, due_date=due_date,
        source=source, source_ref=source_ref,
    )
    if assignee and assignee != created_by:
        _notify(assignee, f"Nowe zadanie: {task.title}", task.description[:200], url=task_url(task))
    return task


def change_status(task, new_status, *, user=None):
    allowed = TASK_LEGAL_TRANSITIONS.get(task.status, set())
    if new_status not in allowed:
        raise ValidationError(f"Niedozwolone przejście {task.status} → {new_status}")
    reopened = task.status not in OPEN_STATUSES and new_status in OPEN_STATUSES
    task.status = new_status
    task.done_at = timezone.now() if new_status == TaskStatus.ZROBIONE else None
    fields = ["status", "done_at", "updated_at"]
    if reopened and task.escalated_at is not None:
        # Ponownie otwarte zadanie znów podlega eskalacji zaległości (notify.overdue_escalation).
        task.escalated_at = None
        fields.append("escalated_at")
    task.save(update_fields=fields)
    # „Biuro odhacza” → zlecający (zwykle handlowiec) dowiaduje się, że sprawa załatwiona.
    if new_status == TaskStatus.ZROBIONE and task.created_by_id and task.created_by != user:
        _notify(task.created_by, f"Zrobione: {task.title}", "", url=task_url(task))
    return task


# Najkrótsza legalna ścieżka do ZROBIONE (dla zamykania zadań systemowych, np. „Umówić przegląd”).
_PATH_TO_DONE = {
    TaskStatus.NOWE: (TaskStatus.W_TOKU, TaskStatus.ZROBIONE),
    TaskStatus.ODLOZONE: (TaskStatus.W_TOKU, TaskStatus.ZROBIONE),
    TaskStatus.PRZYPISANE: (TaskStatus.ZROBIONE,),
    TaskStatus.W_TOKU: (TaskStatus.ZROBIONE,),
    TaskStatus.CZEKA: (TaskStatus.ZROBIONE,),
}


def close_system_tasks(organization, source_ref, *, user=None):
    """Odhacz otwarte zadania o danym `source_ref` (np. `inspection:<id>`), przechodząc legalną ścieżką FSM.

    Zwraca liczbę zamkniętych zadań. Zadania zrobione/odrzucone zostają bez zmian.
    """
    closed = 0
    for task in Task.objects.for_org(organization).filter(source_ref=source_ref, status__in=OPEN_STATUSES):
        for step in _PATH_TO_DONE.get(task.status, ()):
            change_status(task, step, user=user)
        if task.status == TaskStatus.ZROBIONE:
            closed += 1
    return closed


def assignable_users(organization):
    """Aktywni użytkownicy organizacji, którym można przypisać zadanie."""
    if organization is None:
        return User.objects.none()
    return User.objects.filter(organization=organization, is_active=True).order_by("first_name", "username")


def assign_task(task, assignee, *, user=None):
    """Przypisz (albo odepnij przy `assignee=None`) wykonawcę. Wykonawca musi być z tej samej organizacji.

    Nowe zadanie po przypisaniu przechodzi do PRZYPISANE (legalne przejście z tabeli FSM).
    """
    if assignee is not None and (assignee.organization_id != task.organization_id or not assignee.is_active):
        raise ValidationError("Wykonawca spoza organizacji albo nieaktywny.")
    previous = task.assignee_id
    task.assignee = assignee
    fields = ["assignee", "updated_at"]
    if assignee is not None and task.status == TaskStatus.NOWE:
        task.status = TaskStatus.PRZYPISANE
        fields.append("status")
    task.save(update_fields=fields)
    if assignee is not None and assignee.pk != previous and assignee != user:
        _notify(assignee, f"Przypisano Ci zadanie: {task.title}", task.description[:200], url=task_url(task))
    return task


def pick_office_assignee(organization):
    """Użytkownik biura (rola BIURO) z najmniejszą liczbą otwartych zadań; None, gdy brak biura."""
    if organization is None:
        return None
    return (
        User.objects.filter(organization=organization, role=Role.BIURO, is_active=True)
        .annotate(open_count=Count("assigned_tasks", filter=Q(assigned_tasks__status__in=OPEN_STATUSES)))
        .order_by("open_count", "pk")
        .first()
    )


def is_office(user):
    return getattr(user, "role", None) in OFFICE_ROLES


def can_view(task, user):
    if not user.is_authenticated or user.organization_id != task.organization_id:
        return False
    if is_office(user):
        return True
    owner_id = task.customer.owner_id if task.customer_id else None
    return user.pk in (task.assignee_id, task.created_by_id, owner_id)


def can_change_status(task, user):
    """Status zmienia wykonawca, zlecający albo rola biurowa (tej samej organizacji)."""
    if not user.is_authenticated or user.organization_id != task.organization_id:
        return False
    return is_office(user) or user.pk in (task.assignee_id, task.created_by_id)


def add_comment(task, *, author, text):
    text = (text or "").strip()
    if not text:
        raise ValidationError("Komentarz nie może być pusty.")
    comment = TaskComment.objects.create(organization=task.organization, task=task, author=author, text=text)
    # Powiadom drugą stronę (wykonawcę / zlecającego), ale nie autora.
    for recipient in {task.assignee, task.created_by} - {None, author}:
        _notify(recipient, f"Komentarz do zadania: {task.title}", text[:200], url=task_url(task))
    return comment


def customer_timeline(customer, user=None):
    """Wpisy osi czasu klienta (kontrakt TIMELINE z AGENTS.md); przy `user` tylko zadania zgodne z `can_view`."""
    tasks = Task.objects.for_org(customer.organization).filter(customer=customer)
    if user is not None and not is_office(user):
        uid = getattr(user, "pk", None)
        if customer.owner_id is None or customer.owner_id != uid:
            tasks = tasks.filter(Q(assignee_id=uid) | Q(created_by_id=uid))
    tasks = tasks.select_related("assignee").order_by("-created_at")[:50]
    entries = []
    for task in tasks:
        who = f" · {task.assignee}" if task.assignee_id else ""
        entries.append({
            "date": task.done_at or task.created_at,
            "icon": "✅" if task.status == TaskStatus.ZROBIONE else task.source_icon,
            "title": f"Zadanie: {task.title}",
            "text": f"{task.get_status_display()}{who}",
            "url": task_url(task),
        })
    return entries
