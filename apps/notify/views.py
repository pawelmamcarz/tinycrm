"""Widoki powiadomień: lista, licznik (HTMX), web push (klucz VAPID, subskrypcja)."""
import json

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.core.permissions import role_required

from . import services
from .models import Notification

PAGE_SIZE = 50


@require_GET
@role_required()
def notification_list(request):
    # Powiadomienia są osobiste: filtr po użytkowniku (a ten należy do request.organization).
    qs = Notification.objects.filter(user=request.user).order_by("-created_at", "-pk")
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("strona"))
    return render(request, "notify/list.html", {
        "page": page,
        "unread": services.unread_count(request.user),
        "service_worker_url": services.reverse_or("pwa:service_worker", fallback=""),
        "push_configured": bool(settings.VAPID_PUBLIC_KEY),
    })


@require_POST
@role_required()
def mark_all(request):
    count = services.mark_all_read(request.user)
    if count:
        messages.success(request, f"Oznaczono jako przeczytane: {count}.")
    return redirect("notify:list")


@require_POST
@role_required()
def open_notification(request, pk):
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    services.mark_read(notification)
    list_url = services.reverse_or("notify:list")
    return redirect(services.safe_local_url(notification.url, fallback=list_url))


@require_GET
@role_required()
def badge(request):
    return render(request, "notify/_badge.html", {"count": services.unread_count(request.user)})


@require_GET
@role_required()
def vapid_key(request):
    return JsonResponse({"publicKey": settings.VAPID_PUBLIC_KEY or ""})


def _json_body(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


@require_POST
@role_required()
def subscribe(request):
    data = _json_body(request)
    if data is None:
        return HttpResponseBadRequest("Nieprawidłowy JSON.")
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    try:
        _sub, created = services.upsert_subscription(
            request.user, data.get("endpoint"), keys.get("p256dh"), keys.get("auth"))
    except services.SubscriptionConflict as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    except services.SubscriptionError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "created": created}, status=201 if created else 200)


@require_POST
@role_required()
def unsubscribe(request):
    data = _json_body(request)
    if data is None:
        return HttpResponseBadRequest("Nieprawidłowy JSON.")
    endpoint = data.get("endpoint") if isinstance(data.get("endpoint"), str) else None
    deleted = services.remove_subscription(request.user, endpoint)
    return JsonResponse({"ok": True, "deleted": deleted})
