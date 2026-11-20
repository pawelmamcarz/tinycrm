from django.conf import settings
from django.db import models

from apps.core.models import OrgScopedModel


class VoiceStatus(models.TextChoices):
    NOWA = "NOWA", "Nowa (w kolejce)"
    PRZETWARZANIE = "PRZETWARZANIE", "Przetwarzanie"
    DO_POTWIERDZENIA = "DO_POTWIERDZENIA", "Do potwierdzenia przez handlowca"
    POTWIERDZONA = "POTWIERDZONA", "Potwierdzona"
    DO_WERYFIKACJI = "DO_WERYFIKACJI", "Do weryfikacji przez biuro"
    BLAD = "BLAD", "Błąd przetwarzania"


class VoiceNote(OrgScopedModel):
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="voice_notes",
                               verbose_name="autor")
    customer = models.ForeignKey("customers.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="voice_notes", verbose_name="klient")
    audio = models.FileField("nagranie", upload_to="voice/%Y/%m/", blank=True)
    typed_text = models.TextField("tekst (dyktowanie przeglądarki / wpisany)", blank=True)
    transcript = models.TextField("transkrypcja", blank=True)
    extraction = models.JSONField("wynik ekstrakcji AI", default=dict, blank=True)
    status = models.CharField("status", max_length=20, choices=VoiceStatus.choices, default=VoiceStatus.NOWA)
    error = models.CharField("błąd", max_length=255, blank=True)
    client_uuid = models.CharField("UUID z urządzenia (idempotencja kolejki offline)", max_length=64, blank=True)
    recorded_at = models.DateTimeField("nagrano", null=True, blank=True)
    confirmed_at = models.DateTimeField("potwierdzono", null=True, blank=True)
    audio_deleted_at = models.DateTimeField("usunięto audio (retencja)", null=True, blank=True)

    class Meta:
        verbose_name = "notatka głosowa"
        verbose_name_plural = "notatki głosowe"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "client_uuid"], condition=~models.Q(client_uuid=""),
                                    name="uniq_voice_client_uuid"),
        ]

    def __str__(self):
        return f"Notatka {self.pk} · {self.author}"
