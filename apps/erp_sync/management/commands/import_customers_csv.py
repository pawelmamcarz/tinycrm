from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Organization
from apps.erp_sync.models import SyncStatus
from apps.erp_sync.services import import_csv


class Command(BaseCommand):
    help = "Import kontrahentów z pliku CSV (eksport z enova365/Excela) do organizacji."

    def add_arguments(self, parser):
        parser.add_argument("org_slug")
        parser.add_argument("path")

    def handle(self, org_slug, path, **options):
        org = Organization.objects.filter(slug=org_slug).first()
        if org is None:
            raise CommandError(f"Nie ma organizacji „{org_slug}”.")
        file = Path(path)
        if not file.is_file():
            raise CommandError(f"Nie ma pliku {path}.")
        run = import_csv(org, file.read_bytes())
        summary = f"Nowi: {run.created}, zmienieni: {run.updated}, pominięci: {run.skipped}."
        if run.status != SyncStatus.OK:
            raise CommandError(f"Import nieudany: {run.error} ({summary})")
        self.stdout.write(self.style.SUCCESS(f"Import zakończony. {summary}"))
        if run.error:
            self.stdout.write(run.error)
