"""Publiczne API modułu notatek głosowych: przetworzenie (transkrypcja + ekstrakcja) i zatwierdzenie.

Przepływ: NOWA → PRZETWARZANIE → DO_POTWIERDZENIA → (handlowiec zatwierdza) POTWIERDZONA.
Błąd STT → DO_WERYFIKACJI (biuro odsłucha nagranie); nieoczekiwany błąd ekstrakcji → BLAD.
Zadania, zgłoszenia i zamówienia części powstają WYŁĄCZNIE przez serwisy modułów tasks/service.
"""
import logging
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.users.models import Role, User

from .extraction import ASSIGNEE_ROLES, DEFAULT_ROLE_FOR_KIND, KIND_VALUES, MAX_PARTS, MAX_SERVICE, MAX_TASKS
from .extraction import extract
from .models import CONFIRMABLE_STATUSES, VOICE_LEGAL_TRANSITIONS, VoiceNote, VoiceStatus
from .transcription import TranscriptionError, transcribe_note

logger = logging.getLogger(__name__)

# Role „biurowe” notatek: widzą wszystkie notatki organizacji i weryfikują przekazane.
OFFICE_ROLES = (Role.BIURO, Role.SZEF, Role.ADMIN)


# ---------------------------------------------------------------------------------------------
# Pomocnicze
# ---------------------------------------------------------------------------------------------

def note_url(note):
    return reverse("voice:detail", args=[note.pk])


def is_office(user):
    return getattr(user, "role", None) in OFFICE_ROLES


def can_view(note, user):
    """Notatkę widzi autor albo rola biurowa tej samej organizacji."""
    if not getattr(user, "is_authenticated", False) or user.organization_id != note.organization_id:
        return False
    return note.author_id == user.pk or is_office(user)


def _notify(user, title, body="", url=""):
    if user is None:
        return
    try:
        from apps.notify.services import notify
    except ImportError:  # moduł powiadomień opcjonalny
        return
    try:
        notify(user, title, body, url=url)
    except Exception as exc:  # noqa: BLE001 — powiadomienie best-effort, nie blokuje przepływu
        logger.warning("notify failed: %s", exc.__class__.__name__)


def office_recipients(organization):
    """Odbiorcy eskalacji: aktywne biuro; gdy brak — szef/admin."""
    users = User.objects.filter(organization=organization, is_active=True)
    office = list(users.filter(role=Role.BIURO).order_by("pk"))
    return office or list(users.filter(role__in=(Role.SZEF, Role.ADMIN)).order_by("pk"))


def notify_office(note, title, body=""):
    for user in office_recipients(note.organization):
        if user.pk != note.author_id:
            _notify(user, title, body, url=note_url(note))


class StaleStatus(ValidationError):
    """Status notatki w bazie różni się od tego w pamięci — ktoś inny zmienił ją w międzyczasie."""


def set_status(note, new_status, *, error=None, extra_fields=()):
    """Zmiana statusu walidowana `VOICE_LEGAL_TRANSITIONS` względem ZABLOKOWANEGO wiersza w bazie.

    Gdy status w bazie różni się od `note.status` (np. handlowiec zatwierdził notatkę, a eskalacja trzymała
    starą kopię), rzuca `StaleStatus` i niczego nie zapisuje — nie da się nadpisać nowszego stanu.
    """
    if new_status not in VOICE_LEGAL_TRANSITIONS.get(note.status, set()):
        raise ValidationError(f"Niedozwolone przejście {note.status} → {new_status}")
    with transaction.atomic():
        current = (VoiceNote.objects.select_for_update().filter(pk=note.pk)
                   .values_list("status", flat=True).first())
        if current != note.status:
            raise StaleStatus(f"Notatka zmieniła status ({note.status} → {current}) — odśwież i spróbuj ponownie")
        note.status = new_status
        fields = ["status", "updated_at", *extra_fields]
        if error is not None:
            note.error = error[:255]
            fields.append("error")
        note.save(update_fields=fields)
    return note


def recording_day(note):
    """Dzień nagrania (lokalnie) — od niego liczymy „jutro”, „do piątku” itd., nie od dnia przetwarzania."""
    if note.recorded_at:
        return timezone.localdate(note.recorded_at)
    if note.created_at:
        return timezone.localdate(note.created_at)
    return timezone.localdate()


# ---------------------------------------------------------------------------------------------
# Przetwarzanie
# ---------------------------------------------------------------------------------------------

def process_voice_note(note, today=None):
    """Transkrypcja + ekstrakcja. Idempotentne: przetwarza tylko notatki NOWA/BLAD/PRZETWARZANIE.

    Daty względne („jutro”, „do piątku”) liczymy od dnia nagrania (`recorded_at`), nie od dnia przetwarzania —
    kolejka offline może wysłać notatkę kilka dni później.
    Nigdy nie rzuca — błąd kończy się statusem DO_WERYFIKACJI (STT) albo BLAD (reszta). Jeśli w trakcie
    przetwarzania ktoś inny zmienił status (np. sweeper przekazał notatkę biuru), wynik nie nadpisuje tego stanu.
    """
    with transaction.atomic():
        note = VoiceNote.objects.select_for_update().get(pk=note.pk)
        if note.status not in (VoiceStatus.NOWA, VoiceStatus.BLAD, VoiceStatus.PRZETWARZANIE):
            return note
        if note.status != VoiceStatus.PRZETWARZANIE:
            set_status(note, VoiceStatus.PRZETWARZANIE, error="")
    if today is None:
        today = recording_day(note)

    def finish(new_status, *, error="", fields=None):
        """Zapis wyniku tylko, gdy notatka nadal jest w PRZETWARZANIE (blokada wiersza). Zwraca True przy zapisie."""
        with transaction.atomic():
            current = VoiceNote.objects.select_for_update().filter(pk=note.pk).values_list("status", flat=True).first()
            if current != VoiceStatus.PRZETWARZANIE:
                logger.info("Notatka %s zmieniła status na %s w trakcie przetwarzania — pomijam wynik",
                            note.pk, current)
                note.status = current
                return False
            if fields:
                note.save(update_fields=[*fields, "updated_at"])
            set_status(note, new_status, error=error)
        return True

    try:
        transcript = transcribe_note(note)
    except TranscriptionError as exc:
        note.transcript = ""
        if finish(VoiceStatus.DO_WERYFIKACJI, error=str(exc), fields=["transcript"]):
            _notify(note.author, "Notatka przekazana do weryfikacji",
                    "Nie udało się rozpoznać nagrania — biuro je odsłucha.", url=note_url(note))
            notify_office(note, "Notatka głosowa do weryfikacji", f"{note.author}: {exc}")
        return note

    if not transcript.strip():
        if finish(VoiceStatus.DO_WERYFIKACJI, error="Pusta transkrypcja"):
            notify_office(note, "Notatka głosowa do weryfikacji", f"{note.author}: pusta transkrypcja")
        return note

    try:
        result = extract(note.organization, transcript, author=note.author, today=today)
    except Exception as exc:  # noqa: BLE001 — extract nie powinien rzucać, ale pipeline nigdy się nie wywraca
        logger.exception("Ekstrakcja notatki %s nieudana", note.pk)
        note.transcript = transcript
        finish(VoiceStatus.BLAD, error=f"Błąd ekstrakcji: {exc.__class__.__name__}", fields=["transcript"])
        return note

    if note.customer_id:
        # Klient wybrany przez handlowca przy nagrywaniu ma pierwszeństwo przed dopasowaniem.
        result["customer_id"] = note.customer_id
    elif result.get("customer_id"):
        note.customer = Customer.objects.for_org(note.organization).filter(pk=result["customer_id"]).first()
    note.transcript = transcript
    note.extraction = result
    if not finish(VoiceStatus.DO_POTWIERDZENIA, fields=["transcript", "extraction", "customer"]):
        return note
    count = len(result.get("tasks", [])) + len(result.get("service", []))
    _notify(note.author, "Notatka gotowa do potwierdzenia",
            f"{note.customer or 'Klient nierozpoznany'} · pozycji: {count}", url=note_url(note))
    return note


# ---------------------------------------------------------------------------------------------
# Zatwierdzenie
# ---------------------------------------------------------------------------------------------

def _as_date(value):
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _as_int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_items(items):
    """Ujednolica pozycje z ekranu potwierdzenia (lub z `extraction`): typy, role, daty, limity."""
    items = items or {}
    tasks = []
    for task in (items.get("tasks") or [])[:MAX_TASKS]:
        title = str(task.get("title") or "").strip()[:255]
        if not title:
            continue
        kind = str(task.get("kind") or "").upper()
        kind = kind if kind in KIND_VALUES else "INNE"
        role = str(task.get("assignee_role") or "").upper()
        role = role if role in ASSIGNEE_ROLES else DEFAULT_ROLE_FOR_KIND.get(kind, "BIURO")
        tasks.append({"title": title, "kind": kind, "due_date": _as_date(task.get("due_date")),
                      "assignee_role": role, "priority": _as_int(task.get("priority"), 2, 1, 3),
                      "description": str(task.get("description") or "").strip()[:2000]})
    service = []
    for item in (items.get("service") or [])[:MAX_SERVICE]:
        title = str(item.get("title") or "").strip()[:255]
        description = str(item.get("description") or "").strip()[:2000]
        if not title and not description:
            continue
        parts = []
        for part in (item.get("parts") or [])[:MAX_PARTS]:
            name = str(part.get("name") or "").strip()[:200]
            if name:
                parts.append({"name": name, "quantity": _as_int(part.get("quantity"), 1, 1, 999)})
        service.append({"title": title or description[:120], "description": description,
                        "device_hint": str(item.get("device_hint") or "").strip()[:200], "parts": parts})
    return {"customer": items.get("customer"), "tasks": tasks, "service": service}


def serialize_items(items):
    """Pozycje do zapisu w `extraction` (daty jako ISO)."""
    return {
        "tasks": [{**t, "due_date": t["due_date"].isoformat() if t.get("due_date") else None}
                  for t in items["tasks"]],
        "service": items["service"],
    }


def resolve_assignee(organization, role, author):
    if role == "HANDLOWIEC":
        return author
    if role == "SERWIS":
        return (User.objects.filter(organization=organization, role=Role.SERWIS, is_active=True)
                .order_by("pk").first())
    try:
        from apps.tasks.services import pick_office_assignee
    except ImportError:
        pick_office_assignee = None
    if pick_office_assignee is not None:
        assignee = pick_office_assignee(organization)
        if assignee is not None:
            return assignee
    return User.objects.filter(organization=organization, role=Role.BIURO, is_active=True).order_by("pk").first()


def _task_description(note, base=""):
    lines = [base] if base else []
    extraction = note.extraction or {}
    if extraction.get("contact_person"):
        lines.append(f"Osoba kontaktowa: {extraction['contact_person']}")
    if extraction.get("summary"):
        lines.append(f"Z notatki głosowej: {extraction['summary']}")
    return "\n".join(lines)


def items_from_extraction(note):
    extraction = note.extraction or {}
    return {"customer": note.customer, "tasks": extraction.get("tasks", []),
            "service": extraction.get("service", [])}


def confirm_voice_note(note, user, cleaned_items=None):
    """Zatwierdź notatkę: utwórz zadania, zgłoszenia serwisowe i zamówienia części — dokładnie raz.

    `cleaned_items` = {"customer": Customer|None, "tasks": [...], "service": [...]} z ekranu potwierdzenia;
    `None` = użyj wyniku ekstrakcji bez zmian. Zwraca dict z utworzonymi obiektami
    (pusty, gdy notatka była już zatwierdzona — idempotencja).
    Rzuca ValidationError, gdy są sprawy serwisowe bez klienta albo klient jest z innej organizacji.
    """
    from apps.service.services import create_part_order, create_ticket
    from apps.tasks.services import create_task

    created = {"tasks": [], "tickets": [], "part_orders": []}
    with transaction.atomic():
        note = VoiceNote.objects.select_for_update().select_related("organization", "author").get(pk=note.pk)
        if note.status not in CONFIRMABLE_STATUSES:
            return created
        items = normalize_items(cleaned_items if cleaned_items is not None else items_from_extraction(note))
        customer = items["customer"]
        if customer is not None and customer.organization_id != note.organization_id:
            raise ValidationError("Klient spoza organizacji.")
        if items["service"] and customer is None:
            raise ValidationError("Wybierz klienta — sprawy serwisowe wymagają klienta.")
        org = note.organization
        source_ref = f"voice:{note.pk}"

        for task in items["tasks"]:
            created["tasks"].append(create_task(
                org, title=task["title"], created_by=note.author,
                assignee=resolve_assignee(org, task["assignee_role"], note.author), customer=customer,
                description=_task_description(note, task.get("description", "")), kind=task["kind"],
                due_date=task["due_date"], priority=task["priority"], source="GLOS", source_ref=source_ref,
            ))
        for item in items["service"]:
            description = item["description"]
            if item["device_hint"]:
                description = f"{description}\nUrządzenie: {item['device_hint']}".strip()
            ticket = create_ticket(org, customer=customer, title=item["title"], description=description,
                                   reported_by=note.author, source_ref=source_ref)
            created["tickets"].append(ticket)
            for part in item["parts"]:
                created["part_orders"].append(create_part_order(
                    org, customer=customer, part_name=part["name"], quantity=part["quantity"], ticket=ticket,
                ))

        extraction = dict(note.extraction or {})
        extraction.update(serialize_items(items))
        extraction["customer_id"] = customer.pk if customer else None
        extraction["result"] = {
            "task_ids": [t.pk for t in created["tasks"]],
            "ticket_ids": [t.pk for t in created["tickets"]],
            "part_order_ids": [p.pk for p in created["part_orders"]],
            "confirmed_by": user.pk if user else None,
        }
        note.extraction = extraction
        note.customer = customer
        note.confirmed_at = timezone.now()
        note.save(update_fields=["extraction", "customer", "confirmed_at", "updated_at"])
        set_status(note, VoiceStatus.POTWIERDZONA, error="")
    return created


def send_to_verification(note, user, cleaned_items=None):
    """Handlowiec przekazuje notatkę biuru („📨 Przekaż biuru”). Zapisuje jego poprawki pozycji."""
    with transaction.atomic():
        note = VoiceNote.objects.select_for_update().get(pk=note.pk)
        if note.status == VoiceStatus.DO_WERYFIKACJI:
            return note
        if cleaned_items is not None:
            items = normalize_items(cleaned_items)
            customer = items["customer"]
            if customer is not None and customer.organization_id != note.organization_id:
                raise ValidationError("Klient spoza organizacji.")
            extraction = dict(note.extraction or {})
            extraction.update(serialize_items(items))
            extraction["customer_id"] = customer.pk if customer else None
            note.extraction = extraction
            note.customer = customer
            note.save(update_fields=["extraction", "customer", "updated_at"])
        set_status(note, VoiceStatus.DO_WERYFIKACJI)
    notify_office(note, "Notatka głosowa do weryfikacji", f"Przekazał(a): {user}")
    return note


# ---------------------------------------------------------------------------------------------
# Oś czasu klienta
# ---------------------------------------------------------------------------------------------

def customer_timeline(customer, user=None):
    """Wpisy osi czasu klienta; przy `user` tylko notatki, które ten użytkownik może zobaczyć (`can_view`)."""
    notes = VoiceNote.objects.for_org(customer.organization).filter(customer=customer)
    if user is not None and not is_office(user):
        notes = notes.filter(author_id=getattr(user, "pk", None))
    notes = notes.select_related("author").order_by("-created_at")[:50]
    entries = []
    for note in notes:
        summary = (note.extraction or {}).get("summary") or note.transcript or note.typed_text
        entries.append({
            "date": note.recorded_at or note.created_at,
            "icon": "🎙️",
            "title": f"Notatka głosowa · {note.author}",
            "text": (summary or note.get_status_display())[:300],
            "url": note_url(note),
        })
    return entries
