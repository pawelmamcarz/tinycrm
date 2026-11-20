"""Zadania Celery modułu notatek głosowych (przetwarzanie w tle, eskalacja, retencja audio)."""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import VoiceNote, VoiceStatus

logger = logging.getLogger(__name__)

ESCALATE_AFTER = timedelta(hours=2)
REQUEUE_NEW_AFTER = timedelta(minutes=5)     # NOWA dłużej → ponowne zlecenie przetwarzania
GIVE_UP_AFTER = timedelta(hours=2)           # NOWA dłużej → biuro (automat nie dał rady)
PROCESSING_LEASE = timedelta(minutes=30)     # PRZETWARZANIE bez zmian dłużej → worker padł → biuro
# Nagranie usuwamy tylko po zatwierdzeniu notatki (pozostałe statusy wymagają jeszcze odsłuchu).
PURGEABLE_STATUSES = (VoiceStatus.POTWIERDZONA,)


@shared_task
def process_voice_note_task(note_id):
    from .services import process_voice_note

    note = VoiceNote.objects.filter(pk=note_id).first()
    if note is None:
        return None
    return process_voice_note(note).status


@shared_task
def escalate_unconfirmed():
    """Notatki czekające na potwierdzenie handlowca > 2 h → do weryfikacji przez biuro.

    Status sprawdzamy ponownie na zablokowanym wierszu (`set_status`): jeśli handlowiec zdążył zatwierdzić
    notatkę po naszym SELECT-cie, eskalacja ją pomija (bez nadpisania POTWIERDZONA i bez podwójnych zadań).
    """
    from .services import StaleStatus, notify_office, set_status

    cutoff = timezone.now() - ESCALATE_AFTER
    count = 0
    stale = VoiceNote.objects.filter(status=VoiceStatus.DO_POTWIERDZENIA, updated_at__lt=cutoff)
    for note in stale.select_related("organization", "author"):
        with transaction.atomic():
            locked = (VoiceNote.objects.select_for_update().select_related("organization", "author")
                      .filter(pk=note.pk, status=VoiceStatus.DO_POTWIERDZENIA, updated_at__lt=cutoff).first())
            if locked is None:
                continue
            try:
                set_status(locked, VoiceStatus.DO_WERYFIKACJI, error="Niepotwierdzona przez handlowca w ciągu 2 h")
            except StaleStatus:
                continue
        notify_office(locked, "Niepotwierdzona notatka głosowa",
                      f"{locked.author} nie potwierdził(a) notatki — sprawdź i zatwierdź.")
        count += 1
    return count


@shared_task
def retry_stuck_notes():
    """Sprzątanie notatek, które utknęły przed potwierdzeniem.

    - NOWA starsza niż `REQUEUE_NEW_AFTER` (broker był niedostępny przy uploadzie) → ponowne zlecenie
      przetwarzania; po `GIVE_UP_AFTER` bez skutku → do weryfikacji przez biuro;
    - PRZETWARZANIE bez zmian dłużej niż `PROCESSING_LEASE` (worker padł w trakcie) → do weryfikacji przez biuro.
    Zwraca {"requeued": n, "escalated": m}.
    """
    from .services import StaleStatus, _notify, note_url, notify_office, set_status

    now = timezone.now()
    requeued = escalated = 0

    def escalate(note_id, expected, reason):
        with transaction.atomic():
            note = (VoiceNote.objects.select_for_update().select_related("organization", "author")
                    .filter(pk=note_id, status=expected).first())
            if note is None:
                return False
            try:
                set_status(note, VoiceStatus.DO_WERYFIKACJI, error=reason)
            except StaleStatus:
                return False
        notify_office(note, "Notatka głosowa do weryfikacji", f"{note.author}: {reason}")
        _notify(note.author, "Notatka przekazana do weryfikacji",
                "Przetwarzanie się nie powiodło — biuro odsłucha nagranie.", url=note_url(note))
        return True

    for note_id in VoiceNote.objects.filter(status=VoiceStatus.PRZETWARZANIE,
                                            updated_at__lt=now - PROCESSING_LEASE).values_list("pk", flat=True):
        if escalate(note_id, VoiceStatus.PRZETWARZANIE, "Przetwarzanie przerwane (przekroczony czas)"):
            escalated += 1

    for note_id in VoiceNote.objects.filter(status=VoiceStatus.NOWA,
                                            created_at__lt=now - GIVE_UP_AFTER).values_list("pk", flat=True):
        if escalate(note_id, VoiceStatus.NOWA, "Notatka nie została przetworzona automatycznie"):
            escalated += 1

    for note_id in VoiceNote.objects.filter(status=VoiceStatus.NOWA,
                                            created_at__lt=now - REQUEUE_NEW_AFTER).values_list("pk", flat=True):
        try:
            process_voice_note_task.delay(note_id)
            requeued += 1
        except Exception as exc:  # noqa: BLE001 — broker nadal niedostępny: spróbujemy w kolejnym przebiegu
            logger.warning("Nie zlecono ponownie notatki %s: %s", note_id, exc.__class__.__name__)
    return {"requeued": requeued, "escalated": escalated}


@shared_task
def purge_old_audio():
    """Usuń pliki audio starsze niż VOICE_AUDIO_RETENTION_DAYS; transkrypcja zostaje.

    Tylko notatki zatwierdzone — nagranie notatki nieprzetworzonej / do weryfikacji / z błędem jest jedynym
    źródłem treści i musi zostać, dopóki ktoś jej nie zatwierdzi.
    """
    days = getattr(settings, "VOICE_AUDIO_RETENTION_DAYS", 30)
    cutoff = timezone.now() - timedelta(days=days)
    count = 0
    qs = (VoiceNote.objects.filter(status__in=PURGEABLE_STATUSES, created_at__lt=cutoff,
                                   audio_deleted_at__isnull=True).exclude(audio=""))
    for note in qs:
        try:
            note.audio.delete(save=False)
        except Exception as exc:  # noqa: BLE001 — plik mógł już zniknąć ze storage
            logger.warning("Nie usunięto audio notatki %s: %s", note.pk, exc.__class__.__name__)
        note.audio = ""
        note.audio_deleted_at = timezone.now()
        note.save(update_fields=["audio", "audio_deleted_at", "updated_at"])
        count += 1
    return count
