"""Publiczne API modułu zadań — inne moduły tworzą/zmieniają zadania WYŁĄCZNIE tędy."""
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import TASK_LEGAL_TRANSITIONS, Task, TaskSource, TaskStatus


def create_task(organization, *, title, created_by=None, assignee=None, customer=None, description="",
                kind="INNE", due_date=None, priority=2, source=TaskSource.RECZNE, source_ref=""):
    status = TaskStatus.PRZYPISANE if assignee else TaskStatus.NOWE
    task = Task.objects.create(
        organization=organization, title=title[:255], description=description, kind=kind, status=status,
        priority=priority, customer=customer, assignee=assignee, created_by=created_by, due_date=due_date,
        source=source, source_ref=source_ref,
    )
    try:  # powiadomienie wykonawcy — moduł notify jest opcjonalny
        from apps.notify.services import notify

        if assignee and assignee != created_by:
            notify(assignee, f"Nowe zadanie: {task.title}", task.description[:200], url=f"/zadania/{task.pk}/")
    except ImportError:
        pass
    return task


def change_status(task, new_status, *, user=None):
    allowed = TASK_LEGAL_TRANSITIONS.get(task.status, set())
    if new_status not in allowed:
        raise ValidationError(f"Niedozwolone przejście {task.status} → {new_status}")
    task.status = new_status
    task.done_at = timezone.now() if new_status == TaskStatus.ZROBIONE else None
    task.save(update_fields=["status", "done_at", "updated_at"])
    return task
