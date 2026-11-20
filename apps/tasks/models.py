from django.conf import settings
from django.db import models

from apps.core.models import OrgScopedModel


class TaskStatus(models.TextChoices):
    NOWE = "NOWE", "Nowe"
    PRZYPISANE = "PRZYPISANE", "Przypisane"
    W_TOKU = "W_TOKU", "W toku"
    CZEKA = "CZEKA", "Czeka (na klienta/dostawcę)"
    ZROBIONE = "ZROBIONE", "Zrobione"
    ODRZUCONE = "ODRZUCONE", "Odrzucone"
    ODLOZONE = "ODLOZONE", "Odłożone"


OPEN_STATUSES = (TaskStatus.NOWE, TaskStatus.PRZYPISANE, TaskStatus.W_TOKU, TaskStatus.CZEKA, TaskStatus.ODLOZONE)

# Przejścia statusów — tylko przyciskami walidowanymi tą tabelą (konwencja z Pyrlandii).
TASK_LEGAL_TRANSITIONS = {
    TaskStatus.NOWE: {TaskStatus.PRZYPISANE, TaskStatus.W_TOKU, TaskStatus.ODRZUCONE, TaskStatus.ODLOZONE},
    TaskStatus.PRZYPISANE: {TaskStatus.W_TOKU, TaskStatus.ZROBIONE, TaskStatus.ODRZUCONE, TaskStatus.ODLOZONE},
    TaskStatus.W_TOKU: {TaskStatus.CZEKA, TaskStatus.ZROBIONE, TaskStatus.ODLOZONE},
    TaskStatus.CZEKA: {TaskStatus.W_TOKU, TaskStatus.ZROBIONE},
    TaskStatus.ODLOZONE: {TaskStatus.PRZYPISANE, TaskStatus.W_TOKU},
    TaskStatus.ZROBIONE: {TaskStatus.W_TOKU},
    TaskStatus.ODRZUCONE: {TaskStatus.NOWE},
}


class TaskKind(models.TextChoices):
    OFERTA = "OFERTA", "Oferta"
    ZAMOWIENIE = "ZAMOWIENIE", "Zamówienie"
    KONTAKT = "KONTAKT", "Kontakt z klientem"
    SERWIS = "SERWIS", "Serwis / część"
    PRZEGLAD = "PRZEGLAD", "Przegląd"
    PRZETARG = "PRZETARG", "Przetarg"
    INNE = "INNE", "Inne"


class TaskSource(models.TextChoices):
    GLOS = "GLOS", "Notatka głosowa"
    MAIL = "MAIL", "E-mail"
    RECZNE = "RECZNE", "Ręcznie"
    SYSTEM = "SYSTEM", "System (przypomnienie)"


class Priority(models.IntegerChoices):
    NISKI = 1, "Niski"
    NORMALNY = 2, "Normalny"
    PILNY = 3, "Pilny"


class Task(OrgScopedModel):
    title = models.CharField("zadanie", max_length=255)
    description = models.TextField("opis", blank=True)
    kind = models.CharField("typ", max_length=20, choices=TaskKind.choices, default=TaskKind.INNE)
    status = models.CharField("status", max_length=20, choices=TaskStatus.choices, default=TaskStatus.NOWE)
    priority = models.IntegerField("priorytet", choices=Priority.choices, default=Priority.NORMALNY)
    customer = models.ForeignKey("customers.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="tasks", verbose_name="klient")
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="assigned_tasks", verbose_name="wykonawca")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="created_tasks", verbose_name="zlecający")
    due_date = models.DateField("termin", null=True, blank=True)
    source = models.CharField("źródło", max_length=10, choices=TaskSource.choices, default=TaskSource.RECZNE)
    source_ref = models.CharField("odniesienie do źródła", max_length=100, blank=True,
                                  help_text="np. voice:12, service_ticket:5, inspection:7")
    done_at = models.DateTimeField("zakończono", null=True, blank=True)
    last_reminded_at = models.DateTimeField("ostatnie przypomnienie", null=True, blank=True)
    escalated_at = models.DateTimeField("eskalowano", null=True, blank=True)

    class Meta:
        verbose_name = "zadanie"
        verbose_name_plural = "zadania"
        ordering = ["due_date", "-priority", "created_at"]
        indexes = [models.Index(fields=["organization", "status", "due_date"])]

    def __str__(self):
        return self.title

    @property
    def is_open(self):
        return self.status in OPEN_STATUSES


class TaskComment(OrgScopedModel):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    text = models.TextField("komentarz")

    class Meta:
        ordering = ["created_at"]
