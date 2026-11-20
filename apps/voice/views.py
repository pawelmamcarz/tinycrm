"""Widoki notatek głosowych: upload (PWA/JSON/formularz), ekran potwierdzenia, zatwierdzenie, lista, audio."""
import json
import logging
import uuid

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from apps.core.permissions import role_required
from apps.customers.models import Customer
from apps.tasks.models import Priority, TaskKind

from . import services
from .extraction import MAX_PARTS, MAX_SERVICE, MAX_TASKS
from .models import PROCESSING_STATUSES, TextSource, VoiceNote, VoiceStatus
from .tasks import process_voice_note_task
from .transcription import AUDIO_EXT_MIME, audio_mime_for_name

logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 25 * 1024 * 1024
ASSIGNEE_ROLE_CHOICES = [("BIURO", "Biuro"), ("HANDLOWIEC", "Ja (handlowiec)"), ("SERWIS", "Serwis")]
EXTRA_TASK_ROWS = 3
EXTRA_SERVICE_ROWS = 1
EXTRA_PART_ROWS = 2
_MIME_TO_EXT = {mime: ext for ext, mime in reversed(list(AUDIO_EXT_MIME.items()))}


# ---------------------------------------------------------------------------------------------
# Pomocnicze
# ---------------------------------------------------------------------------------------------

def _parse_pk(value):
    value = str(value or "").strip()
    if value.isascii() and value.isdigit() and len(value) < 18:
        return int(value)
    return None


def _parse_count(value, maximum):
    number = _parse_pk(value)
    return min(number, maximum) if number is not None else 0


def _wants_json(request):
    accept = request.headers.get("Accept", "")
    return ("application/json" in accept or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.content_type == "application/json" or request.GET.get("format") == "json")


def _get_note(request, pk):
    note = get_object_or_404(VoiceNote.objects.for_org(request.organization).select_related("author", "customer"),
                             pk=pk)
    if not services.can_view(note, request.user):
        raise Http404
    return note


def _upload_error(request, message, status=400):
    if _wants_json(request):
        return JsonResponse({"error": message}, status=status)
    messages.error(request, message)
    return redirect("voice:list")


def _upload_payload(request):
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return request.POST


def _note_response(request, note, created, warning=""):
    if _wants_json(request):
        payload = {"id": note.pk, "status": note.status, "url": services.note_url(note)}
        if warning:
            payload["warning"] = warning
        return JsonResponse(payload, status=201 if created else 200)
    if warning:
        messages.warning(request, warning)
    if created:
        messages.success(request, "Notatka zapisana — sprawdź i zatwierdź.")
    return redirect("voice:detail", pk=note.pk)


# ---------------------------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------------------------

@require_POST
@role_required()
def upload(request):
    org = request.organization
    data = _upload_payload(request)
    if data is None:
        return _upload_error(request, "Nieprawidłowe dane (JSON).")
    client_uuid = str(data.get("client_uuid") or "").strip()[:64]
    if client_uuid:
        existing = VoiceNote.objects.for_org(org).filter(client_uuid=client_uuid).first()
        if existing is not None:
            if existing.author_id != request.user.pk and not services.is_office(request.user):
                return _upload_error(request, "Konflikt identyfikatora notatki.", status=409)
            return _note_response(request, existing, created=False)

    audio = request.FILES.get("audio")
    typed_text = str(data.get("typed_text") or "").strip()[:20000]
    if audio is None and not typed_text:
        return _upload_error(request, "Nagraj notatkę albo wpisz tekst.")
    if audio is not None:
        content_type = (getattr(audio, "content_type", "") or "").split(";")[0].strip().lower()
        if not content_type.startswith("audio/"):
            return _upload_error(request, "Nieobsługiwany format pliku — wymagane nagranie audio.")
        if audio.size > MAX_AUDIO_BYTES:
            return _upload_error(request, "Nagranie jest za duże (maks. 25 MB).", status=413)
        if not audio.size:
            return _upload_error(request, "Puste nagranie.")
        ext = _MIME_TO_EXT.get(content_type, "webm")
        audio.name = f"{uuid.uuid4().hex}.{ext}"  # nazwa z serwera, nie od klienta

    customer = None
    warning = ""
    customer_raw = data.get("customer_id")
    if customer_raw not in (None, ""):
        pk = _parse_pk(customer_raw)
        customer = Customer.objects.for_org(org).filter(pk=pk).first() if pk else None
        if customer is None:
            # Klient mógł zostać usunięty/scalony, zanim kolejka offline wysłała notatkę. Nie odrzucamy
            # nagrania (handlowiec nie mógłby go już wysłać) — AI dopasuje klienta z treści.
            warning = "Wybrany klient nie istnieje — klienta rozpozna AI z treści notatki."
            logger.info("Upload notatki: nieznany customer_id — dopasowanie przez AI")

    text_source = str(data.get("text_source") or "").strip().upper()
    if text_source not in TextSource.values:
        text_source = TextSource.NIEZNANE

    recorded_at = None
    recorded_raw = str(data.get("recorded_at") or "").strip()
    if recorded_raw:
        try:
            recorded_at = parse_datetime(recorded_raw)
        except ValueError:
            recorded_at = None
        if recorded_at is not None and timezone.is_naive(recorded_at):
            recorded_at = timezone.make_aware(recorded_at)

    try:
        with transaction.atomic():
            note = VoiceNote.objects.create(
                organization=org, author=request.user, customer=customer, typed_text=typed_text,
                text_source=text_source, client_uuid=client_uuid, recorded_at=recorded_at or timezone.now(),
                **({"audio": audio} if isinstance(audio, UploadedFile) else {}),
            )
    except IntegrityError:
        existing = VoiceNote.objects.for_org(org).filter(client_uuid=client_uuid).first()
        if client_uuid and existing is not None:
            return _note_response(request, existing, created=False)
        raise

    try:
        process_voice_note_task.delay(note.pk)
    except Exception as exc:  # noqa: BLE001 — broker niedostępny: notatka zostaje w kolejce (NOWA)
        logger.warning("Nie zlecono przetwarzania notatki %s: %s", note.pk, exc.__class__.__name__)
    note.refresh_from_db()
    return _note_response(request, note, created=True, warning=warning)


# ---------------------------------------------------------------------------------------------
# Ekran potwierdzenia
# ---------------------------------------------------------------------------------------------

def _task_rows(note):
    tasks = list((note.extraction or {}).get("tasks") or [])
    rows = [{**t, "extra": False} for t in tasks]
    rows += [{"title": "", "kind": "INNE", "due_date": None, "assignee_role": "BIURO", "priority": 2, "extra": True,
              "extra_index": i} for i in range(EXTRA_TASK_ROWS)]
    return rows


def _service_rows(note):
    items = list((note.extraction or {}).get("service") or [])
    rows = [{**s, "extra": False} for s in items]
    rows += [{"title": "", "description": "", "device_hint": "", "parts": [], "extra": True, "extra_index": i}
             for i in range(EXTRA_SERVICE_ROWS)]
    for row in rows:
        row["part_rows"] = list(row.get("parts") or []) + [{"name": "", "quantity": 1}] * EXTRA_PART_ROWS
    return rows


def _processing_stale(note):
    """Przetwarzanie trwa podejrzanie długo — pokaż komunikat zamiast wiecznego spinnera."""
    from datetime import timedelta

    started = note.updated_at or note.created_at
    return bool(started) and timezone.now() - started > timedelta(minutes=3)


def _detail_context(request, note):
    result = (note.extraction or {}).get("result") or {}
    extraction = note.extraction or {}
    customer = note.customer if note.customer_id else None
    customer_initial = {"id": customer.pk, "label": f"{customer.name} ({customer.city})" if customer.city
                        else customer.name} if customer else {"id": "", "label": ""}
    return {
        "note": note,
        "extraction": note.extraction or {},
        "task_rows": _task_rows(note),
        "service_rows": _service_rows(note),
        "extra_task_rows": EXTRA_TASK_ROWS,
        "kinds": TaskKind.choices,
        "roles": ASSIGNEE_ROLE_CHOICES,
        "priorities": Priority.choices,
        "customer_initial": customer_initial,
        "has_tasks": bool(extraction.get("tasks")),
        "has_service": bool(extraction.get("service")),
        "processing_stale": note.status in PROCESSING_STATUSES and _processing_stale(note),
        "is_processing": note.status in PROCESSING_STATUSES,
        "can_confirm": note.status in (VoiceStatus.DO_POTWIERDZENIA, VoiceStatus.DO_WERYFIKACJI),
        "can_send_to_office": note.status in (VoiceStatus.DO_POTWIERDZENIA, VoiceStatus.BLAD),
        "is_office": services.is_office(request.user),
        "result_counts": {"tasks": len(result.get("task_ids", [])), "tickets": len(result.get("ticket_ids", [])),
                          "parts": len(result.get("part_order_ids", []))},
    }


@require_GET
@role_required()
def detail(request, pk):
    note = _get_note(request, pk)
    if request.headers.get("HX-Request") == "true" and request.GET.get("partial") == "status":
        if note.status in PROCESSING_STATUSES:
            return render(request, "voice/_processing.html",
                          {"note": note, "processing_stale": _processing_stale(note)})
        response = HttpResponse("")
        response["HX-Refresh"] = "true"
        return response
    return render(request, "voice/detail.html", _detail_context(request, note))


def parse_items(request):
    """Pozycje z formularza ekranu potwierdzenia → dict dla `services.confirm_voice_note`.

    Rzuca ValidationError, gdy wybrany klient nie należy do organizacji.
    """
    post = request.POST
    customer = None
    customer_pk = _parse_pk(post.get("customer"))
    if post.get("customer"):
        customer = Customer.objects.for_org(request.organization).filter(pk=customer_pk).first() \
            if customer_pk else None
        if customer is None:
            raise ValidationError("Nieznany klient.")
    tasks = []
    for i in range(_parse_count(post.get("task_total"), MAX_TASKS + EXTRA_TASK_ROWS)):
        prefix = f"task-{i}-"
        if post.get(prefix + "remove"):
            continue
        title = (post.get(prefix + "title") or "").strip()
        if not title:
            continue
        tasks.append({"title": title, "kind": post.get(prefix + "kind"), "due_date": post.get(prefix + "due"),
                      "assignee_role": post.get(prefix + "role"), "priority": post.get(prefix + "priority")})
    service = []
    for i in range(_parse_count(post.get("service_total"), MAX_SERVICE + EXTRA_SERVICE_ROWS)):
        prefix = f"service-{i}-"
        if post.get(prefix + "remove"):
            continue
        parts = []
        for j in range(MAX_PARTS + EXTRA_PART_ROWS):
            name = (post.get(f"{prefix}part-{j}-name") or "").strip()
            if name:
                parts.append({"name": name, "quantity": post.get(f"{prefix}part-{j}-qty")})
        title = (post.get(prefix + "title") or "").strip()
        description = (post.get(prefix + "description") or "").strip()
        if not title and not description:
            continue
        service.append({"title": title, "description": description,
                        "device_hint": (post.get(prefix + "device") or "").strip(), "parts": parts})
    return {"customer": customer, "tasks": tasks, "service": service}


@require_POST
@role_required()
def confirm(request, pk):
    note = _get_note(request, pk)
    action = request.POST.get("action", "confirm")
    try:
        items = parse_items(request)
        if action == "verify":
            if note.status == VoiceStatus.DO_WERYFIKACJI:
                messages.info(request, "Notatka już czeka na weryfikację biura.")
            elif note.status not in (VoiceStatus.DO_POTWIERDZENIA, VoiceStatus.BLAD):
                messages.warning(request, "Tej notatki nie można już przekazać biuru.")
            else:
                services.send_to_verification(note, request.user, items)
                messages.success(request, "📨 Przekazano biuru do weryfikacji.")
            return redirect("voice:detail", pk=note.pk)
        if note.status == VoiceStatus.POTWIERDZONA:
            messages.info(request, "Notatka była już zatwierdzona.")
            return redirect("voice:detail", pk=note.pk)
        if note.status not in (VoiceStatus.DO_POTWIERDZENIA, VoiceStatus.DO_WERYFIKACJI):
            messages.warning(request, "Notatka nie jest gotowa do zatwierdzenia.")
            return redirect("voice:detail", pk=note.pk)
        created = services.confirm_voice_note(note, request.user, items)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("voice:detail", pk=note.pk)
    messages.success(request, f"✅ Zatwierdzono: zadań {len(created['tasks'])}, zgłoszeń serwisowych "
                              f"{len(created['tickets'])}, części {len(created['part_orders'])}. Biuro już to ma.")
    return redirect("voice:detail", pk=note.pk)


# ---------------------------------------------------------------------------------------------
# Lista i audio
# ---------------------------------------------------------------------------------------------

LIST_FILTERS = [("", "Wszystkie"), ("do_potwierdzenia", "Do potwierdzenia"), ("do_weryfikacji", "Do weryfikacji"),
                ("potwierdzone", "Potwierdzone")]
_FILTER_STATUS = {"do_potwierdzenia": VoiceStatus.DO_POTWIERDZENIA, "do_weryfikacji": VoiceStatus.DO_WERYFIKACJI,
                  "potwierdzone": VoiceStatus.POTWIERDZONA}


@require_GET
@role_required()
def note_list(request):
    office = services.is_office(request.user)
    qs = VoiceNote.objects.for_org(request.organization).select_related("author", "customer")
    if not office or request.GET.get("moje") == "1":
        qs = qs.filter(author=request.user)
    current = request.GET.get("f", "")
    if current not in _FILTER_STATUS:
        current = ""
    else:
        qs = qs.filter(status=_FILTER_STATUS[current])
    counts = {"do_weryfikacji": VoiceNote.objects.for_org(request.organization)
              .filter(status=VoiceStatus.DO_WERYFIKACJI).count()} if office else {}
    return render(request, "voice/list.html", {
        "notes": qs.order_by("-created_at")[:200], "filters": LIST_FILTERS, "current": current,
        "is_office": office, "counts": counts, "only_mine": request.GET.get("moje") == "1",
    })


@require_GET
@role_required()
def audio(request, pk):
    note = _get_note(request, pk)
    if not note.audio:
        raise Http404
    try:
        handle = note.audio.open("rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404 from exc
    mime = audio_mime_for_name(note.audio.name)
    response = FileResponse(handle, content_type=mime if mime.startswith("audio/") else "application/octet-stream",
                            as_attachment=not mime.startswith("audio/"), filename=f"notatka-{note.pk}")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response
