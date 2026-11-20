"""Silnik synchronizacji kontrahentów (wzorzec DataSync).

Zasady:
- klucz zewnętrzny: `Customer.enova_id` = `CustomerRecord.external_id` (unikalny w organizacji),
- gdy klient nie ma jeszcze `enova_id` — dopasowanie po NIP (tylko jednoznaczne), dla rekordów bez ID
  także po dokładnej nazwie,
- pola prowadzone w CRM (`owner`, `notes`, `email_domain`) NIGDY nie są nadpisywane, a puste wartości
  ze źródła nie czyszczą danych w CRM,
- każda strona w osobnej transakcji; błąd = przebieg BLAD i kursor bez zmian,
- równoległe przebiegi tej samej pary (organizacja, źródło) blokuje `pg_try_advisory_lock`.

Granice modułów: odczyt przez model `Customer` (zawsze `for_org`), zapis wyłącznie przez
`apps.customers.services.create_customer` / `update_customer`.
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.utils import timezone

from apps.customers.models import Customer
from apps.customers.services import create_customer, update_customer

from .adapters.base import CustomerRecord
from .models import SyncRun, SyncState, SyncStatus

logger = logging.getLogger(__name__)

SOURCE_CSV = "csv:kontrahenci"
SOURCE_ENOVA = "enova:kontrahenci"
ALREADY_RUNNING = "Synchronizacja tego źródła już trwa — pominięto."
MAX_WARNINGS = 50

# Pola nadpisywane danymi z ERP (gdy niepuste w źródle). owner/notes/email_domain — nigdy.
SYNCED_FIELDS = ("name", "short_name", "nip", "city", "address", "phone", "email")
_MAX_LEN = {name: Customer._meta.get_field(name).max_length for name in SYNCED_FIELDS}
_ENOVA_ID_MAX = Customer._meta.get_field("enova_id").max_length


def lock_key(organization, source_name: str) -> int:
    """Stabilny 64-bitowy klucz blokady doradczej dla pary (organizacja, źródło)."""
    digest = hashlib.blake2b(f"tinycrm:erp_sync:{organization.pk}:{source_name}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def advisory_lock(organization, source_name: str):
    """Blokada sesyjna Postgresa; zwraca True, gdy uzyskana. Zawsze zwalniana w `finally`."""
    key = lock_key(organization, source_name)
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", [key])
        acquired = bool(cur.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", [key])


@dataclass
class _PageResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def _clean(record: CustomerRecord) -> dict:
    """Wartości do zapisu: tylko niepuste, przycięte do długości pól, e-mail poprawny."""
    values = {}
    for name in SYNCED_FIELDS:
        value = getattr(record, name) or ""
        if name == "email" and value:
            try:
                validate_email(value)
            except ValidationError:
                value = ""
        if value:
            values[name] = value[: _MAX_LEN[name]]
    return values


def _find_existing(organization, record: CustomerRecord, warnings: list[str]):
    base = Customer.objects.for_org(organization)
    if record.external_id:
        customer = base.filter(enova_id=record.external_id).first()
        if customer:
            return customer
    if record.nip:
        candidates = list(base.filter(nip=record.nip, enova_id="")[:2])
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            warnings.append(f"NIP {record.nip} ma kilku klientów bez ID enova — nie dowiązano „{record.name}”.")
            return None
    if not record.external_id and record.name:
        candidates = list(base.filter(name__iexact=record.name, enova_id="")[:2])
        if len(candidates) == 1:
            return candidates[0]
    return None


def upsert_record(organization, record: CustomerRecord, result: _PageResult):
    if not record.name:
        result.skipped += 1
        return
    if len(record.external_id) > _ENOVA_ID_MAX:
        result.skipped += 1
        result.warnings.append(f"ID „{record.external_id[:20]}…” jest za długie — pominięto „{record.name}”.")
        return
    values = _clean(record)
    customer = _find_existing(organization, record, result.warnings)
    if customer is None:
        create_customer(organization, enova_id=record.external_id,
                        is_active=record.active if record.active is not None else True, **values)
        result.created += 1
        return
    updates = dict(values)
    if record.external_id and not customer.enova_id:
        updates["enova_id"] = record.external_id
    if record.active is not None:
        updates["is_active"] = record.active
    if update_customer(customer, updates):
        result.updated += 1
    else:
        result.skipped += 1


def _finish(run: SyncRun, status: str, error: str = ""):
    run.status = status
    run.error = error
    run.finished_at = timezone.now()
    run.save()
    return run


def run_sync(organization, source, source_name: str, full: bool = False) -> SyncRun:
    """Uruchamia synchronizację `source` dla organizacji. Zwraca zapisany `SyncRun`.

    `full=True` ignoruje zapisany kursor (pełny przebieg), ale po sukcesie kursor jest zapisywany.
    """
    with advisory_lock(organization, source_name) as acquired:
        if not acquired:
            run = SyncRun.objects.create(organization=organization, source=source_name)
            return _finish(run, SyncStatus.BLAD, ALREADY_RUNNING)

        state, _ = SyncState.objects.get_or_create(organization=organization, source=source_name)
        run = SyncRun.objects.create(organization=organization, source=source_name)
        warnings: list[str] = []
        next_cursor = None
        try:
            for page in source.iter_records("" if full else state.cursor):
                page_result = _PageResult(skipped=page.skipped, warnings=list(page.warnings))
                with transaction.atomic():
                    for record in page.records:
                        upsert_record(organization, record, page_result)
                # Strona zatwierdzona — dopiero teraz doliczamy jej wyniki.
                run.created += page_result.created
                run.updated += page_result.updated
                run.skipped += page_result.skipped
                warnings.extend(page_result.warnings)
                if page.next_cursor is not None:
                    next_cursor = page.next_cursor
                run.save(update_fields=["created", "updated", "skipped", "updated_at"])
        except Exception as exc:  # noqa: BLE001 — każdy błąd kończy przebieg jako BLAD, kursor bez zmian
            logger.exception("Synchronizacja %s (org %s) nieudana", source_name, organization.pk)
            message = str(exc) or exc.__class__.__name__
            return _finish(run, SyncStatus.BLAD, f"{message}"[:2000])

        if next_cursor is not None:
            state.cursor = next_cursor[: SyncState._meta.get_field("cursor").max_length]
        state.last_success_at = timezone.now()
        state.save(update_fields=["cursor", "last_success_at", "updated_at"])
        note = ""
        if warnings:
            extra = len(warnings) - MAX_WARNINGS
            note = "Uwagi:\n" + "\n".join(warnings[:MAX_WARNINGS])
            if extra > 0:
                note += f"\n… i {extra} kolejnych."
        return _finish(run, SyncStatus.OK, note)
