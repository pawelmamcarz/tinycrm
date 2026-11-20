"""Widoki zadań: kolejka biura, moje zadania, szczegóły, tworzenie, zmiana statusu/przypisania (HTMX)."""
from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.core.permissions import role_required
from apps.customers.models import Customer
from apps.users.models import Role

from . import services
from .forms import TaskForm
from .models import OPEN_STATUSES, Task, TaskKind, TaskSource, TaskStatus

OFFICE_ROLES = services.OFFICE_ROLES

OFFICE_TABS = [
    ("nieprzypisane", "Nieprzypisane"),
    ("po_terminie", "Po terminie"),
    ("dzis", "Na dziś"),
    ("otwarte", "Wszystkie otwarte"),
    ("zrobione", "Zrobione (7 dni)"),
]


def _parse_pk(value):
    """Bezpieczne parsowanie identyfikatora z GET/POST (cyfry Unicode nie dadzą 500)."""
    value = (value or "").strip()
    if value.isascii() and value.isdigit():
        return int(value)
    return None


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _org_tasks(request):
    return Task.objects.for_org(request.organization).select_related("customer", "assignee", "created_by")


def _get_task(request, pk):
    task = get_object_or_404(_org_tasks(request), pk=pk)
    if not services.can_view(task, request.user):
        raise Http404
    return task


def _tab_queryset(qs, tab, today):
    open_qs = qs.filter(status__in=OPEN_STATUSES)
    if tab == "nieprzypisane":
        return open_qs.filter(assignee__isnull=True)
    if tab == "po_terminie":
        return open_qs.filter(due_date__lt=today)
    if tab == "dzis":
        return open_qs.filter(due_date=today)
    if tab == "zrobione":
        return qs.filter(status=TaskStatus.ZROBIONE, done_at__gte=timezone.now() - timedelta(days=7)).order_by(
            "-done_at")
    return open_qs


def _row_context(request, task):
    return {"task": task, "assignable_users": services.assignable_users(request.organization)}


@role_required(*OFFICE_ROLES)
def office(request):
    org = request.organization
    today = timezone.localdate()
    tab = request.GET.get("tab", "nieprzypisane")
    if tab not in dict(OFFICE_TABS):
        tab = "nieprzypisane"

    base = _org_tasks(request)
    # Filtry: niesparsowaną wartość ignorujemy.
    filters = {"assignee": request.GET.get("assignee", ""), "kind": request.GET.get("kind", ""),
               "customer": request.GET.get("customer", "")}
    if filters["assignee"] == "brak":
        base = base.filter(assignee__isnull=True)
    elif (assignee_id := _parse_pk(filters["assignee"])) is not None:
        base = base.filter(assignee_id=assignee_id)
    if filters["kind"] in TaskKind.values:
        base = base.filter(kind=filters["kind"])
    if (customer_id := _parse_pk(filters["customer"])) is not None:
        base = base.filter(customer_id=customer_id)

    tabs = [{"key": key, "label": label, "count": _tab_queryset(base, key, today).count()}
            for key, label in OFFICE_TABS]
    tasks = _tab_queryset(base, tab, today)[:200]
    users = services.assignable_users(org)
    query = request.GET.copy()
    query.pop("tab", None)
    return render(request, "tasks/office.html", {
        "tab": tab, "tabs": tabs, "tasks": tasks, "assignable_users": users, "filters": filters,
        "kinds": TaskKind.choices, "customers": Customer.objects.for_org(org).filter(is_active=True),
        "filter_query": query.urlencode(),
    })


@role_required()
def mine(request):
    today = timezone.localdate()
    tasks = list(_org_tasks(request).filter(assignee=request.user, status__in=OPEN_STATUSES))
    groups = [
        ("Po terminie", "error", [t for t in tasks if t.due_date and t.due_date < today]),
        ("Dziś", "warning", [t for t in tasks if t.due_date == today]),
        ("Najbliższe", "info", sorted([t for t in tasks if t.due_date and t.due_date > today],
                                      key=lambda t: t.due_date)),
        ("Bez terminu", "ghost", [t for t in tasks if not t.due_date]),
    ]
    return render(request, "tasks/mine.html", {
        "groups": [{"label": label, "color": color, "tasks": items} for label, color, items in groups],
        "total": len(tasks),
    })


@role_required()
def detail(request, pk):
    task = _get_task(request, pk)
    return render(request, "tasks/detail.html", {
        "task": task,
        "comments": task.comments.select_related("author"),
        "can_change": services.can_change_status(task, request.user),
        "is_office": services.is_office(request.user),
        "assignable_users": services.assignable_users(request.organization),
    })


@role_required()
def create(request):
    org = request.organization
    if request.method == "POST":
        form = TaskForm(request.POST, organization=org)
        if form.is_valid():
            data = form.cleaned_data
            assignee = data["assignee"]
            if assignee is None and request.user.role == Role.HANDLOWIEC:
                assignee = services.pick_office_assignee(org)
            task = services.create_task(
                org, title=data["title"], description=data["description"], kind=data["kind"],
                priority=data["priority"], customer=data["customer"], assignee=assignee,
                created_by=request.user, due_date=data["due_date"], source=TaskSource.RECZNE,
            )
            messages.success(request, "Zadanie dodane.")
            return redirect("tasks:detail", pk=task.pk)
    else:
        initial = {}
        if (customer_id := _parse_pk(request.GET.get("customer"))) is not None:
            customer = Customer.objects.for_org(org).filter(pk=customer_id).first()
            if customer:
                initial["customer"] = customer
        form = TaskForm(initial=initial, organization=org)
    return render(request, "tasks/create.html", {"form": form})


@require_POST
@role_required()
def status_change(request, pk):
    task = _get_task(request, pk)
    if not services.can_change_status(task, request.user):
        raise PermissionDenied
    new_status = request.POST.get("status", "")
    try:
        services.change_status(task, new_status, user=request.user)
    except ValidationError:
        message = "Niedozwolona zmiana statusu."
        if _is_htmx(request):
            return HttpResponseBadRequest(message)
        messages.error(request, message)
        return redirect("tasks:detail", pk=pk)
    if _is_htmx(request):
        task = _get_task(request, pk)
        if request.POST.get("view") == "card":
            return render(request, "tasks/_card.html", {"task": task})
        return render(request, "tasks/_office_row.html", _row_context(request, task))
    messages.success(request, f"Status: {task.get_status_display()}.")
    return redirect(_safe_next(request) or reverse("tasks:detail", args=[pk]))


def _safe_next(request):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return ""


@require_POST
@role_required(*OFFICE_ROLES)
def assign(request, pk):
    task = _get_task(request, pk)
    raw = request.POST.get("assignee", "")
    assignee = None
    if raw:
        assignee_id = _parse_pk(raw)
        assignee = services.assignable_users(request.organization).filter(pk=assignee_id).first() \
            if assignee_id is not None else None
        if assignee is None:
            return HttpResponseBadRequest("Nieprawidłowy wykonawca.")
    services.assign_task(task, assignee, user=request.user)
    if _is_htmx(request):
        return render(request, "tasks/_office_row.html", _row_context(request, _get_task(request, pk)))
    messages.success(request, "Przypisanie zapisane.")
    return redirect(_safe_next(request) or reverse("tasks:detail", args=[pk]))


@require_POST
@role_required()
def comment(request, pk):
    task = _get_task(request, pk)
    try:
        services.add_comment(task, author=request.user, text=request.POST.get("text", ""))
    except ValidationError as exc:
        if _is_htmx(request):
            return HttpResponseBadRequest(exc.messages[0])
        messages.error(request, exc.messages[0])
        return redirect("tasks:detail", pk=pk)
    if _is_htmx(request):
        return render(request, "tasks/_comments.html", {"task": task, "comments": task.comments.select_related(
            "author")})
    return redirect("tasks:detail", pk=pk)
