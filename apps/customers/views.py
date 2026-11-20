"""Widoki klientów: lista, karta (z osią czasu), formularze biura, wyszukiwarka HTMX/JSON."""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.permissions import role_required, user_has_role
from apps.users.models import Role

from . import services
from .forms import ContactForm, CustomerForm
from .models import Customer

OFFICE_ROLES = (Role.BIURO, Role.SZEF, Role.ADMIN)
PAGE_SIZE = 25


def _is_truthy(value):
    return (value or "").lower() in ("1", "true", "tak", "on")


@role_required()
@require_GET
def customer_list(request):
    q = request.GET.get("q", "").strip()
    mine = _is_truthy(request.GET.get("moi"))
    show_inactive = _is_truthy(request.GET.get("nieaktywni"))
    qs = Customer.objects.for_org(request.organization).select_related("owner")
    if not show_inactive:
        qs = qs.filter(is_active=True)
    if q:
        cond = Q(name__icontains=q) | Q(short_name__icontains=q) | Q(city__icontains=q) | Q(nip__icontains=q)
        digits = "".join(ch for ch in q if ch.isascii() and ch.isdigit())
        if len(digits) >= 3:
            cond |= Q(nip__icontains=digits)
        qs = qs.filter(cond)
    if mine:
        qs = qs.filter(owner=request.user)
    page = Paginator(qs.order_by("name", "pk"), PAGE_SIZE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    context = {
        "page": page,
        "q": q,
        "mine": mine,
        "show_inactive": show_inactive,
        "querystring": params.urlencode(),
        "can_edit": user_has_role(request.user, OFFICE_ROLES),
    }
    template = "customers/_list_results.html" if request.headers.get("HX-Request") else "customers/list.html"
    return render(request, template, context)


@role_required()
@require_GET
def customer_detail(request, pk):
    customer = get_object_or_404(
        Customer.objects.for_org(request.organization).select_related("owner"), pk=pk
    )
    context = {
        "customer": customer,
        "contacts": customer.contacts.filter(organization=request.organization),
        "open_tasks": services.open_tasks_count(customer),
        "timeline": services.build_timeline(customer, user=request.user),
        "contact_form": ContactForm(),
        "can_edit": user_has_role(request.user, OFFICE_ROLES),
    }
    return render(request, "customers/detail.html", context)


@role_required(*OFFICE_ROLES)
@require_http_methods(["GET", "POST"])
def customer_create(request):
    form = CustomerForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        customer = form.save(commit=False)
        customer.organization = request.organization
        customer.save()
        messages.success(request, "Dodano klienta.")
        return redirect("customers:detail", pk=customer.pk)
    return render(request, "customers/form.html", {"form": form, "customer": None})


@role_required(*OFFICE_ROLES)
@require_http_methods(["GET", "POST"])
def customer_edit(request, pk):
    customer = get_object_or_404(Customer.objects.for_org(request.organization), pk=pk)
    form = CustomerForm(request.POST or None, instance=customer, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Zapisano zmiany.")
        return redirect("customers:detail", pk=customer.pk)
    return render(request, "customers/form.html", {"form": form, "customer": customer})


@role_required()
@require_POST
def contact_add(request, pk):
    customer = get_object_or_404(Customer.objects.for_org(request.organization), pk=pk)
    form = ContactForm(request.POST)
    if form.is_valid():
        contact = form.save(commit=False)
        contact.customer = customer
        contact.organization = request.organization
        contact.save()
        messages.success(request, "Dodano osobę kontaktową.")
        return redirect("customers:detail", pk=customer.pk)
    context = {
        "customer": customer,
        "contacts": customer.contacts.filter(organization=request.organization),
        "open_tasks": services.open_tasks_count(customer),
        "timeline": services.build_timeline(customer, user=request.user),
        "contact_form": form,
        "can_edit": user_has_role(request.user, OFFICE_ROLES),
    }
    return render(request, "customers/detail.html", context, status=400)


@role_required()
@require_GET
def customer_search(request):
    """Wyszukiwarka: fragment HTML (HTMX) albo JSON `{"results": [{"id","name","city"}]}` przy ?format=json."""
    q = request.GET.get("q", "")
    results = list(services.search_queryset(request.organization, q, user=request.user)[: services.SEARCH_LIMIT])
    if request.GET.get("format") == "json":
        return JsonResponse({"results": [{"id": c.pk, "name": c.name, "city": c.city} for c in results]})
    return render(request, "customers/_search_results.html", {"results": results, "q": q.strip()})
