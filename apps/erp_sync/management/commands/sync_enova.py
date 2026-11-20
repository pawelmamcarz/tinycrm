from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Organization
from apps.erp_sync.adapters.enova import EnovaError
from apps.erp_sync.models import SyncStatus
from apps.erp_sync.services import sync_enova


class Command(BaseCommand):
    help = "Synchronizacja kontrahentów z enova365 WebAPI (od zapisanego kursora)."

    def add_arguments(self, parser):
        parser.add_argument("org_slug")
        parser.add_argument("--full", action="store_true", help="Pełny przebieg — ignoruj zapisany kursor.")

    def handle(self, org_slug, full=False, **options):
        org = Organization.objects.filter(slug=org_slug).first()
        if org is None:
            raise CommandError(f"Nie ma organizacji „{org_slug}”.")
        if not getattr(settings, "ENOVA_BASE_URL", ""):
            raise CommandError("Brak ENOVA_BASE_URL — skonfiguruj połączenie albo użyj import_customers_csv.")
        try:
            run = sync_enova(org, full=full)
        except EnovaError as exc:
            raise CommandError(str(exc)) from exc
        summary = f"Nowi: {run.created}, zmienieni: {run.updated}, pominięci: {run.skipped}."
        if run.status != SyncStatus.OK:
            raise CommandError(f"Synchronizacja nieudana: {run.error} ({summary})")
        self.stdout.write(self.style.SUCCESS(f"Synchronizacja zakończona. {summary}"))
        if run.error:
            self.stdout.write(run.error)
