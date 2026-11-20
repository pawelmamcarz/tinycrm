"""Widok integracji: historia przebiegów synchronizacji i ręczny import CSV (biuro)."""
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.core.permissions import role_required
from apps.users.models import Role

from .forms import CsvImportForm
from .models import SyncRun, SyncState, SyncStatus
from .services import import_csv

OFFICE_ROLES = (Role.BIURO, Role.SZEF, Role.ADMIN)
RUNS_LIMIT = 20


@role_required(*OFFICE_ROLES)
@require_http_methods(["GET", "POST"])
def status(request):
    org = request.organization
    if request.method == "POST":
        form = CsvImportForm(request.POST, request.FILES)
        if form.is_valid():
            run = import_csv(org, form.cleaned_data["file"].read())
            summary = f"nowi: {run.created}, zmienieni: {run.updated}, pominięci: {run.skipped}"
            if run.status == SyncStatus.OK:
                messages.success(request, f"Import zakończony — {summary}.")
            else:
                messages.error(request, f"Import nieudany: {run.error}")
            return redirect(f"{request.path}?przebieg={run.pk}")
    else:
        form = CsvImportForm()
    runs = list(SyncRun.objects.for_org(org).order_by("-created_at", "-pk")[:RUNS_LIMIT])
    highlight = request.GET.get("przebieg", "")
    return render(request, "erp_sync/status.html", {
        "form": form,
        "runs": runs,
        "states": SyncState.objects.for_org(org).order_by("source"),
        "highlight": highlight if highlight.isascii() and highlight.isdigit() else "",
    }, status=400 if request.method == "POST" else 200)
