"""Zadania okresowe: codzienny import kontrahentów z enova365."""
import logging

from celery import shared_task
from django.conf import settings

from .adapters.enova import EnovaError
from .services import enova_organizations, sync_enova

logger = logging.getLogger(__name__)


@shared_task
def daily_enova_sync():
    """Codzienna synchronizacja kontrahentów. Bez `ENOVA_BASE_URL` — pomijamy (tryb CSV)."""
    if not getattr(settings, "ENOVA_BASE_URL", ""):
        logger.info("enova: brak ENOVA_BASE_URL — pomijam synchronizację.")
        return {"skipped": "brak konfiguracji enova"}
    orgs = enova_organizations()
    if not orgs:
        logger.warning("enova: nie wiadomo, którą organizację synchronizować (ustaw ENOVA_ORG_SLUG).")
        return {"skipped": "brak organizacji"}
    results = {}
    for org in orgs:
        try:
            run = sync_enova(org)
        except EnovaError as exc:  # np. błędna konfiguracja klienta
            logger.error("enova: %s", exc)
            results[org.slug] = "BLAD"
            continue
        results[org.slug] = run.status
        logger.info("enova %s: %s (nowi %s, zmienieni %s, pominięci %s)", org.slug, run.status, run.created,
                    run.updated, run.skipped)
    return results
