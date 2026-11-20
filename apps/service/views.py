"""Widoki serwisu: pulpit, urządzenia, przeglądy, zgłoszenia, zamówienia części.

Dostęp: SERWIS/BIURO/SZEF/ADMIN — pełny. HANDLOWIEC — podgląd urządzeń swoich klientów, zgłaszanie usterek
i podgląd własnych zgłoszeń (bez zmiany statusów).
"""
import datetime
import mimetypes
import os

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.permissions import role_required, user_has_role
from apps.customers.models import Customer
from apps.users.models import Role

from . import services
from .forms import CompleteInspectionForm, DeviceForm, PartOrderForm, TicketForm, service_staff
from .models import (
    ACTIVE_PART_ORDER_STATUSES,
    OPEN_TICKET_STATUSES,
    Device,
    Inspection,
    InspectionStatus,
    PartOrder,
    PartOrderStatus,
    ServiceTicket,
    TicketStatus,
)

STAFF_ROLES = (Role.SERWIS, Role.BIURO, Role.SZEF, Role.ADMIN)
ALL_ROLES = STAFF_ROLES + (Role.HANDLOWIEC,)
PAGE_SIZE = 25
INLINE_SAFE_TYPES = {"application/pdf", "image/jpeg", "image/png"}


# --- pomocnicze ---

def _is_staff(user):
    return user_has_role(user, STAFF_ROLES)


def _parse_pk(value):
    value = (value or "").strip()
    if value.isascii() and value.isdigit() and len(value) < 18:
        return int(value)
    return None


def _parse_date(value):
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _visible_devices(request):
    qs = Device.objects.for_org(request.organization).select_related("customer")
    if not _is_staff(request.user):
        qs = qs.filter(customer__owner=request.user)
    return qs


def _visible_tickets(request):
    qs = ServiceTicket.objects.for_org(request.organization).select_related("customer", "device", "assignee",
                                                                           "reported_by")
    if not _is_staff(request.user):
        qs = qs.filter(Q(customer__owner=request.user) | Q(reported_by=request.user))
    return qs


def _safe_next(request, fallback):
    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                  require_https=request.is_secure()):
        return target
    return fallback


def _querystring(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


# --- pulpit ---

@role_required(*STAFF_ROLES)
@require_GET
def dashboard(request):
    org = request.organization
    today = timezone.localdate()
    devices = Device.objects.for_org(org).select_related("customer")
    context = {
        "stats": services.dashboard_stats(org, today=today),
        "overdue_devices": devices.filter(next_inspection_due__lt=today).order_by("next_inspection_due")[:10],
        "upcoming_devices": devices.filter(next_inspection_due__gte=today,
                                           next_inspection_due__lte=today + datetime.timedelta(days=30))
                                   .order_by("next_inspection_due")[:10],
        "open_tickets": ServiceTicket.objects.for_org(org).filter(status__in=OPEN_TICKET_STATUSES)
                                    .select_related("customer", "assignee").order_by("created_at")[:10],
        "parts_pending": PartOrder.objects.for_org(org)
                                  .filter(status__in=(PartOrderStatus.DO_ZAMOWIENIA, PartOrderStatus.ZAMOWIONE))
                                  .select_related("customer", "ticket").order_by("created_at")[:10],
        "warranty_devices": services.warranty_expiring_devices(org, today=today).select_related("customer")
                                    .order_by("warranty_until")[:10],
    }
    return render(request, "service/dashboard.html", context)


# --- urządzenia ---

@role_required(*ALL_ROLES)
@require_GET
def device_list(request):
    qs = _visible_devices(request)
    q = request.GET.get("q", "").strip()
    customer_pk = _parse_pk(request.GET.get("customer"))
    overdue = request.GET.get("overdue") in ("1", "tak", "on")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(serial_number__icontains=q) | Q(manufacturer__icontains=q)
                       | Q(customer__name__icontains=q) | Q(customer__short_name__icontains=q))
    customer = None
    if customer_pk:
        customer = Customer.objects.for_org(request.organization).filter(pk=customer_pk).first()
        qs = qs.filter(customer_id=customer_pk)
    if overdue:
        qs = qs.filter(next_inspection_due__lt=timezone.localdate())
    page = Paginator(qs.order_by("next_inspection_due", "name", "pk"), PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page": page, "q": q, "overdue": overdue, "customer": customer,
        "querystring": _querystring(request), "is_staff": _is_staff(request.user),
    }
    return render(request, "service/devices.html", context)


@role_required(*ALL_ROLES)
@require_GET
def device_detail(request, pk):
    device = get_object_or_404(_visible_devices(request), pk=pk)
    return _render_device_detail(request, device, CompleteInspectionForm())


def _render_device_detail(request, device, complete_form, status=200):
    org = request.organization
    context = {
        "device": device,
        "inspections": Inspection.objects.for_org(org).filter(device=device).select_related("performed_by")
                                 .order_by("-due_date", "-pk"),
        "open_inspection": services.open_inspection(device),
        "tickets": _visible_tickets(request).filter(device=device).order_by("-created_at"),
        "complete_form": complete_form,
        "is_staff": _is_staff(request.user),
    }
    return render(request, "service/device_detail.html", context, status=status)


@role_required(*STAFF_ROLES)
@require_http_methods(["GET", "POST"])
def device_create(request):
    initial = {}
    customer_pk = _parse_pk(request.GET.get("customer"))
    if customer_pk:
        initial["customer"] = customer_pk
    form = DeviceForm(request.POST or None, initial=initial, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        device = form.save(commit=False)
        device.organization = request.organization
        if not device.next_inspection_due:
            device.next_inspection_due = services.initial_next_due(device)
        device.save()
        messages.success(request, "Dodano urządzenie.")
        return redirect("service:device_detail", pk=device.pk)
    return render(request, "service/device_form.html", {"form": form, "device": None})


@role_required(*STAFF_ROLES)
@require_http_methods(["GET", "POST"])
def device_edit(request, pk):
    device = get_object_or_404(Device.objects.for_org(request.organization), pk=pk)
    form = DeviceForm(request.POST or None, instance=device, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        device = form.save(commit=False)
        if not device.next_inspection_due:
            device.next_inspection_due = services.initial_next_due(device)
        device.save()
        messages.success(request, "Zapisano zmiany.")
        return redirect("service:device_detail", pk=device.pk)
    return render(request, "service/device_form.html", {"form": form, "device": device})


# --- przeglądy ---

@role_required(*STAFF_ROLES)
@require_POST
def inspection_complete(request, pk):
    device = get_object_or_404(Device.objects.for_org(request.organization).select_related("customer"), pk=pk)
    form = CompleteInspectionForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            services.complete_device_inspection(device, form.cleaned_data["performed_at"], request.user,
                                                protocol=form.cleaned_data.get("protocol"),
                                                notes=form.cleaned_data.get("notes", ""))
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            device.refresh_from_db()
            messages.success(request, f"Przegląd zapisany. Następny do {device.next_inspection_due:%d.%m.%Y}.")
            return redirect("service:device_detail", pk=device.pk)
    return _render_device_detail(request, device, form, status=400)


@role_required(*STAFF_ROLES)
@require_POST
def inspection_status(request, pk):
    inspection = get_object_or_404(Inspection.objects.for_org(request.organization), pk=pk)
    new_status = request.POST.get("status", "")
    if new_status == InspectionStatus.WYKONANY:
        messages.error(request, "Wykonanie przeglądu zapisz formularzem z datą i protokołem.")
    else:
        try:
            services.change_inspection_status(inspection, new_status, user=request.user,
                                              scheduled_for=_parse_date(request.POST.get("scheduled_for")))
            messages.success(request, f"Przegląd: {inspection.get_status_display()}.")
        except ValidationError:
            messages.error(request, "Niedozwolona zmiana statusu przeglądu.")
    return redirect(_safe_next(request, f"/serwis/urzadzenia/{inspection.device_id}/"))


@role_required(*ALL_ROLES)
@require_GET
def inspection_redirect(request, pk):
    """Stały adres przeglądu (np. `source_ref` zadania `inspection:<id>`) → karta urządzenia."""
    inspection = get_object_or_404(Inspection.objects.for_org(request.organization), pk=pk)
    get_object_or_404(_visible_devices(request), pk=inspection.device_id)
    return redirect("service:device_detail", pk=inspection.device_id)


@role_required(*ALL_ROLES)
@require_GET
def inspection_protocol(request, pk):
    """Protokół przeglądu przez autoryzowany widok (ta sama bramka co karta urządzenia), nie surowy URL media."""
    inspection = get_object_or_404(Inspection.objects.for_org(request.organization), pk=pk)
    get_object_or_404(_visible_devices(request), pk=inspection.device_id)
    if not inspection.protocol:
        raise Http404
    filename = os.path.basename(inspection.protocol.name)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    inline = content_type in INLINE_SAFE_TYPES
    try:
        handle = inspection.protocol.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404 from None
    response = FileResponse(handle, as_attachment=not inline, filename=filename,
                            content_type=content_type if inline else "application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    return response


# --- zgłoszenia ---

STATUS_FILTERS = [("otwarte", "Otwarte")] + list(TicketStatus.choices) + [("wszystkie", "Wszystkie")]


@role_required(*STAFF_ROLES)
@require_GET
def ticket_list(request):
    qs = ServiceTicket.objects.for_org(request.organization).select_related("customer", "device", "assignee")
    status = request.GET.get("status", "otwarte")
    if status == "otwarte":
        qs = qs.filter(status__in=OPEN_TICKET_STATUSES)
    elif status in TicketStatus.values:
        qs = qs.filter(status=status)
    elif status != "wszystkie":
        status = "otwarte"
        qs = qs.filter(status__in=OPEN_TICKET_STATUSES)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(title__icontains=q) | Q(customer__name__icontains=q)
                       | Q(customer__short_name__icontains=q) | Q(device__serial_number__icontains=q))
    if request.GET.get("moje") in ("1", "tak", "on"):
        qs = qs.filter(assignee=request.user)
    page = Paginator(qs.order_by("-created_at", "-pk"), PAGE_SIZE).get_page(request.GET.get("page"))
    context = {"page": page, "status": status, "status_filters": STATUS_FILTERS, "q": q,
               "querystring": _querystring(request)}
    return render(request, "service/tickets.html", context)


@role_required(*ALL_ROLES)
@require_GET
def ticket_detail(request, pk):
    ticket = get_object_or_404(_visible_tickets(request), pk=pk)
    return _render_ticket_detail(request, ticket, PartOrderForm())


def _render_ticket_detail(request, ticket, part_form, status=200):
    is_staff = _is_staff(request.user)
    context = {
        "ticket": ticket,
        "part_orders": PartOrder.objects.for_org(request.organization).filter(ticket=ticket).order_by("created_at"),
        "part_form": part_form,
        "is_staff": is_staff,
        "staff_users": service_staff(request.organization) if is_staff else [],
    }
    return render(request, "service/ticket_detail.html", context, status=status)


@role_required(*ALL_ROLES)
@require_http_methods(["GET", "POST"])
def ticket_create(request):
    initial = {}
    device_pk = _parse_pk(request.GET.get("device"))
    customer_pk = _parse_pk(request.GET.get("customer"))
    if device_pk:
        device = _visible_devices(request).filter(pk=device_pk).first()
        if device:
            initial.update(device=device.pk, customer=device.customer_id,
                           is_warranty=device.is_under_warranty)
    elif customer_pk:
        initial["customer"] = customer_pk
    form = TicketForm(request.POST or None, initial=initial, user=request.user, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            ticket = services.create_ticket(
                request.organization, customer=data["customer"], device=data.get("device"), title=data["title"],
                description=data.get("description", ""), reported_by=request.user,
                is_warranty=data.get("is_warranty", False), assignee=data.get("assignee"),
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f"Utworzono zgłoszenie {ticket.number}.")
            return redirect("service:ticket_detail", pk=ticket.pk)
    return render(request, "service/ticket_form.html", {"form": form})


@role_required(*STAFF_ROLES)
@require_POST
def ticket_status(request, pk):
    ticket = get_object_or_404(ServiceTicket.objects.for_org(request.organization), pk=pk)
    try:
        services.change_ticket_status(ticket, request.POST.get("status", ""), user=request.user)
        messages.success(request, f"{ticket.number}: {ticket.get_status_display()}.")
    except ValidationError:
        messages.error(request, "Niedozwolona zmiana statusu zgłoszenia.")
    return redirect(_safe_next(request, services.ticket_url(ticket)))


@role_required(*STAFF_ROLES)
@require_POST
def ticket_assign(request, pk):
    ticket = get_object_or_404(ServiceTicket.objects.for_org(request.organization), pk=pk)
    assignee_pk = _parse_pk(request.POST.get("assignee"))
    assignee = None
    if assignee_pk:
        assignee = service_staff(request.organization).filter(pk=assignee_pk).first()
        if assignee is None:
            raise Http404
    services.assign_ticket(ticket, assignee, user=request.user)
    messages.success(request, "Zapisano serwisanta.")
    return redirect("service:ticket_detail", pk=ticket.pk)


# --- części ---

@role_required(*STAFF_ROLES)
@require_POST
def part_order_add(request, pk):
    ticket = get_object_or_404(_visible_tickets(request), pk=pk)
    form = PartOrderForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        services.create_part_order(
            request.organization, customer=ticket.customer, ticket=ticket, part_name=data["part_name"],
            quantity=data["quantity"], part_number=data.get("part_number", ""), supplier=data.get("supplier", ""),
            expected_at=data.get("expected_at"),
        )
        messages.success(request, "Dodano część do zamówienia.")
        return redirect("service:ticket_detail", pk=ticket.pk)
    ticket.refresh_from_db()
    return _render_ticket_detail(request, ticket, form, status=400)


@role_required(*STAFF_ROLES)
@require_POST
def part_order_status(request, pk):
    order = get_object_or_404(PartOrder.objects.for_org(request.organization).select_related("ticket"), pk=pk)
    try:
        services.change_part_order_status(order, request.POST.get("status", ""), user=request.user,
                                          expected_at=_parse_date(request.POST.get("expected_at")))
        messages.success(request, f"{order.part_name}: {order.get_status_display()}.")
    except ValidationError:
        messages.error(request, "Niedozwolona zmiana statusu części.")
    fallback = services.ticket_url(order.ticket) if order.ticket_id else "/serwis/czesci/"
    return redirect(_safe_next(request, fallback))


@role_required(*STAFF_ROLES)
@require_GET
def parts(request):
    orders = (PartOrder.objects.for_org(request.organization)
              .filter(status__in=ACTIVE_PART_ORDER_STATUSES)
              .select_related("customer", "ticket").order_by("expected_at", "created_at"))
    by_status = {status: [] for status in ACTIVE_PART_ORDER_STATUSES}
    for order in orders:
        by_status[order.status].append(order)
    groups = [{"status": s, "label": PartOrderStatus(s).label, "orders": by_status[s]}
              for s in ACTIVE_PART_ORDER_STATUSES]
    recent_installed = (PartOrder.objects.for_org(request.organization)
                        .filter(status=PartOrderStatus.ZAMONTOWANE)
                        .select_related("customer", "ticket").order_by("-installed_at")[:10])
    return render(request, "service/parts.html", {"groups": groups, "recent_installed": recent_installed,
                                                  "today": timezone.localdate()})
