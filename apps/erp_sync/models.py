from django.db import models

from apps.core.models import OrgScopedModel


class SyncStatus(models.TextChoices):
    W_TOKU = "W_TOKU", "W toku"
    OK = "OK", "Zakończona"
    BLAD = "BLAD", "Błąd"


class SyncState(OrgScopedModel):
    """Kursor synchronizacji per (organizacja, źródło). Przesuwany TYLKO po udanej partii."""

    source = models.CharField(max_length=50)  # np. "enova:kontrahenci"
    cursor = models.CharField(max_length=100, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "source"], name="uniq_sync_state")]


class SyncRun(OrgScopedModel):
    source = models.CharField(max_length=50)
    status = models.CharField(max_length=10, choices=SyncStatus.choices, default=SyncStatus.W_TOKU)
    finished_at = models.DateTimeField(null=True, blank=True)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
