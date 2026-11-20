"""Publiczne API modułu erp_sync dla innych modułów i komend."""
from __future__ import annotations

from django.conf import settings

from apps.core.models import Organization

from .adapters.csv_source import CsvCustomerSource
from .adapters.enova import EnovaClient, EnovaSource
from .sync import SOURCE_CSV, SOURCE_ENOVA, run_sync


def import_csv(organization, data: bytes):
    """Import kontrahentów z bajtów pliku CSV. Zwraca SyncRun."""
    return run_sync(organization, CsvCustomerSource(data), SOURCE_CSV)


def sync_enova(organization, full: bool = False, client: EnovaClient | None = None):
    """Synchronizacja z enova365 WebAPI. Zwraca SyncRun (BLAD przy braku konfiguracji/połączenia)."""
    own_client = client is None
    if own_client:
        client = EnovaClient()
    try:
        return run_sync(organization, EnovaSource(client), SOURCE_ENOVA, full=full)
    finally:
        if own_client:
            client.close()


def enova_organizations():
    """Organizacje synchronizowane z enova: `ENOVA_ORG_SLUG` albo jedyna organizacja w systemie (pilot)."""
    slug = getattr(settings, "ENOVA_ORG_SLUG", "")
    if slug:
        return list(Organization.objects.filter(slug=slug))
    orgs = list(Organization.objects.all()[:2])
    return orgs if len(orgs) == 1 else []
