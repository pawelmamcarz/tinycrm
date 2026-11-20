"""Aplikacja terenowa (PWA) handlowca: ekran „Teren”, nagrywanie notatki, manifest i service worker.

Manifest, service worker i strona offline są publiczne: przeglądarka pobiera manifest bez ciasteczek,
a service worker musi dać się zainstalować także zanim użytkownik się zaloguje.
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.core.permissions import role_required
from apps.customers.models import Customer
from apps.tasks.models import OPEN_STATUSES, Task
from apps.voice.models import VoiceNote, VoiceStatus

APP_DIR = Path(__file__).resolve().parent
SW_SOURCE = APP_DIR / "templates" / "pwa" / "sw.js"
STATIC_DIR = APP_DIR / "static" / "pwa"
THEME_COLOR = "#1e3a5f"
BACKGROUND_COLOR = "#f2f2f2"
MAX_RECORDING_SECONDS = 5 * 60
RECENT_NOTES = 5
TODAY_TASKS_LIMIT = 30

# Statyczne pliki powłoki aplikacji (cache-first w service workerze).
SHELL_STATIC = ["pwa/queue.js", "pwa/record.js", "pwa/home.js", "pwa/sw-register.js",
                "pwa/icon.svg", "pwa/icon-192.png", "pwa/icon-512.png", "pwa/apple-touch-icon.png"]


def _shell_version():
    """Wersja cache: skrót źródła SW i plików powłoki — zmiana pliku = nowy cache po wdrożeniu."""
    digest = hashlib.sha256()
    for path in [SW_SOURCE, *(STATIC_DIR / name.split("/", 1)[1] for name in SHELL_STATIC)]:
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:12]


@lru_cache(maxsize=1)
def _cached_shell_version():
    return _shell_version()


def shell_version():
    return _shell_version() if settings.DEBUG else _cached_shell_version()


def _parse_pk(value):
    """Bezpieczne parsowanie identyfikatora z parametru (cyfry ASCII; cyfry Unicode → None)."""
    value = str(value or "").strip()
    if value.isascii() and value.isdigit() and len(value) < 18:
        return int(value)
    return None


# ---------------------------------------------------------------------------------------------
# Strony
# ---------------------------------------------------------------------------------------------

@role_required()
def home(request):
    """Ekran startowy handlowca: duży przycisk „Nagraj”, zadania na dziś, ostatnie notatki."""
    org, user = request.organization, request.user
    today = timezone.localdate()
    tasks = list(
        Task.objects.for_org(org)
        .filter(assignee=user, status__in=OPEN_STATUSES, due_date__lte=today)
        .select_related("customer")
        .order_by("due_date", "-priority", "created_at")[:TODAY_TASKS_LIMIT]
    )
    notes = list(
        VoiceNote.objects.for_org(org).filter(author=user).select_related("customer")
        .order_by("-created_at", "-pk")[:RECENT_NOTES]
    )
    to_confirm = VoiceNote.objects.for_org(org).filter(author=user, status=VoiceStatus.DO_POTWIERDZENIA).count()
    return render(request, "pwa/home.html", {
        "today": today,
        "tasks": tasks,
        "overdue_count": sum(1 for t in tasks if t.due_date and t.due_date < today),
        "notes": notes,
        "to_confirm_count": to_confirm,
        "pwa_config": _client_config(request),
    })


@role_required()
def record(request):
    """Nagrywanie notatki po wizycie: audio (MediaRecorder) + dyktowanie przeglądarki + kolejka offline.

    Opcjonalnie `?klient=<id>` wstępnie wybiera klienta (tylko z własnej organizacji; obcy/nieznany = brak).
    """
    initial_customer = None
    pk = _parse_pk(request.GET.get("klient"))
    if pk is not None:
        customer = Customer.objects.for_org(request.organization).filter(pk=pk).first()
        if customer is not None:
            label = f"{customer.name} · {customer.city}" if getattr(customer, "city", "") else customer.name
            initial_customer = {"id": customer.pk, "label": label}
    return render(request, "pwa/record.html", {
        "max_minutes": MAX_RECORDING_SECONDS // 60,
        "initial_customer": initial_customer,
        "pwa_config": _client_config(request),
    })


@require_GET
def offline(request):
    """Strona zastępcza bez danych użytkownika — service worker pokazuje ją, gdy nie ma zasięgu."""
    return render(request, "pwa/offline.html", {"home_url": reverse("pwa:home"),
                                                 "record_url": reverse("pwa:record")})


def _client_config(request):
    """Konfiguracja dla JS (przekazywana przez `json_script`).

    `owner` wiąże rekordy kolejki offline (IndexedDB) z kontem — notatki innego konta na tym samym telefonie
    nie są pokazywane ani wysyłane z bieżącą sesją.
    """
    return {
        "owner": f"{request.organization.pk if request.organization else ''}:{request.user.pk}",
        "uploadUrl": reverse("voice:upload"),
        "homeUrl": reverse("pwa:home"),
        "recordUrl": reverse("pwa:record"),
        "customerSearchUrl": reverse("customers:search"),
        "swUrl": reverse("pwa:service_worker"),
        "maxSeconds": MAX_RECORDING_SECONDS,
    }


# ---------------------------------------------------------------------------------------------
# Manifest i service worker
# ---------------------------------------------------------------------------------------------

@require_GET
def manifest(request):
    data = {
        "name": "TinyCRM Teren",
        "short_name": "TinyCRM",
        "description": "Notatki głosowe po wizycie i zadania na dziś — aplikacja terenowa handlowca.",
        "lang": "pl",
        "dir": "ltr",
        "id": reverse("pwa:home"),
        "start_url": reverse("pwa:home"),
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": THEME_COLOR,
        "background_color": BACKGROUND_COLOR,
        "icons": [
            {"src": static("pwa/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("pwa/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("pwa/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            {"src": static("pwa/icon.svg"), "sizes": "any", "type": "image/svg+xml"},
        ],
        "shortcuts": [
            {"name": "Nagraj notatkę", "short_name": "Nagraj", "url": reverse("pwa:record"),
             "icons": [{"src": static("pwa/icon-192.png"), "sizes": "192x192", "type": "image/png"}]},
        ],
    }
    response = JsonResponse(data, json_dumps_params={"ensure_ascii": False})
    response["Content-Type"] = "application/manifest+json; charset=utf-8"
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
@never_cache
def service_worker(request):
    """Service worker z zakresem „/” (nagłówek Service-Worker-Allowed). Konfiguracja wstrzyknięta jako JSON."""
    config = {
        "version": shell_version(),
        "offlineUrl": reverse("pwa:offline"),
        "loginUrl": str(settings.LOGIN_URL),
        "logoutUrl": reverse("users:logout"),
        "staticUrl": settings.STATIC_URL,
        "precache": [reverse("pwa:offline"), reverse("pwa:manifest"), *(static(name) for name in SHELL_STATIC)],
        "warmPages": [reverse("pwa:home"), reverse("pwa:record")],
        "recordPath": reverse("pwa:record"),
        "pagePrefixes": [reverse("pwa:home")],
        "cdnHosts": ["cdn.jsdelivr.net", "cdn.tailwindcss.com", "unpkg.com"],
        "defaultIcon": static("pwa/icon-192.png"),
        "defaultUrl": reverse("pwa:home"),
    }
    payload = json.dumps(config).replace("<", "\\u003c")
    body = f"self.TINYCRM_SW = {payload};\n" + SW_SOURCE.read_text(encoding="utf-8")
    response = HttpResponse(body, content_type="application/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    return response
