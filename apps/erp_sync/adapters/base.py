"""Kontrakt adapterów: rekord kontrahenta, strona danych i protokół źródła.

Źródło (CSV, enova365, w przyszłości inne ERP) zwraca strony rekordów. Każda strona może nieść
`next_cursor` — kursor, od którego następna synchronizacja ma zacząć. Kursor zapisuje dopiero
`sync.run_sync` i wyłącznie po udanym przebiegu całego źródła.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Iterator, Protocol, runtime_checkable


def normalize_nip(value) -> str:
    """NIP jako same cyfry: „PL 123-456-78-90” → „1234567890”."""
    return re.sub(r"[^0-9]", "", str(value or ""))


@dataclass
class CustomerRecord:
    """Kontrahent w neutralnym formacie, niezależnym od źródła.

    `external_id` — stabilny identyfikator w ERP (w TinyCRM: `Customer.enova_id`). Pusty = brak ID
    (np. CSV bez kolumny ID) i dopasowanie po NIP / nazwie.
    """

    external_id: str = ""
    name: str = ""
    short_name: str = ""
    nip: str = ""
    city: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    updated_at: datetime.datetime | None = None
    # None = źródło nie wie (nie zmieniamy `is_active`); True/False = stan z ERP.
    active: bool | None = None

    def __post_init__(self):
        for name in ("external_id", "name", "short_name", "city", "address", "phone", "email"):
            value = getattr(self, name)
            setattr(self, name, " ".join(str(value or "").split()))
        self.nip = normalize_nip(self.nip)


@dataclass
class Page:
    """Partia rekordów przetwarzana w jednej transakcji."""

    records: list[CustomerRecord]
    next_cursor: str | None = None  # None = źródło nie przesuwa kursora
    skipped: int = 0  # rekordy odrzucone już przez adapter (np. bez nazwy)
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class CustomerSource(Protocol):
    """Źródło kontrahentów. `iter_records(cursor)` zwraca iterator stron (`Page`)."""

    def iter_records(self, cursor: str) -> Iterator[Page]:  # pragma: no cover — protokół
        ...
