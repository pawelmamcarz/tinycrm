"""Import kontrahentów z pliku CSV (plan B, gdy WebAPI enova365 jest niedostępne).

Obsługuje typowy eksport z enova365/Excela:
- separator `;` (polski Excel) albo `,` (wykrywany z nagłówka), także tabulator,
- kodowanie UTF-8 (z BOM lub bez), a gdy się nie da — Windows-1250,
- nagłówki polskie lub angielskie, bez względu na wielkość liter i polskie znaki
  (np. „Kod”/„Akronim”, „Nazwa”, „NIP”, „Miejscowość”/„Miasto”, „Ulica”/„Adres”, „Telefon”, „E-mail”).
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Iterator

from .base import CustomerRecord, Page

PAGE_SIZE = 200


class CsvFormatError(ValueError):
    """Pliku nie da się zinterpretować jako listy kontrahentów."""


def normalize_header(text: str) -> str:
    text = (text or "").replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


# Pole rekordu -> aliasy nagłówka (po normalize_header). Kolejność = priorytet.
ALIASES: dict[str, tuple[str, ...]] = {
    "external_id": ("id", "idkontrahenta", "identyfikator", "externalid", "enovaid", "guid",
                    "kod", "kodkontrahenta", "akronim", "code"),
    "short_name": ("akronim", "nazwaskrocona", "skrot", "shortname", "kod", "kodkontrahenta", "code"),
    "name": ("nazwa", "nazwapelna", "pelnanazwa", "nazwakontrahenta", "name", "fullname", "kontrahent"),
    "nip": ("nip", "euvat", "nipue", "vatid", "taxid", "vat"),
    "city": ("miejscowosc", "miasto", "city", "town"),
    "street": ("ulica", "adres", "address", "street", "adresulica"),
    "house_no": ("nrdomu", "numerdomu", "nrbudynku", "budynek", "houseno", "housenumber"),
    "flat_no": ("nrlokalu", "numerlokalu", "lokal", "flatno"),
    "postal_code": ("kodpocztowy", "kodpoczt", "postalcode", "zipcode", "zip"),
    "phone": ("telefon", "tel", "telefonkomorkowy", "phone", "telephone", "mobile"),
    "email": ("email", "mail", "adresemail", "emailaddress"),
    "active": ("aktywny", "active"),
    "blocked": ("zablokowany", "blokada", "archiwalny", "blocked", "archived"),
}

_TRUE = {"1", "tak", "t", "true", "yes", "y", "x"}
_FALSE = {"0", "nie", "n", "false", "no", ""}


def decode(data: bytes) -> str:
    """UTF-8 (z BOM) albo, gdy niepoprawne, Windows-1250 (typowy eksport z polskiego Windows)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1250", errors="replace")


def sniff_delimiter(header_line: str) -> str:
    counts = {d: header_line.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ";"


def _flag(value: str):
    v = (value or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def map_columns(headers: list[str]) -> dict[str, int]:
    """Pole -> indeks kolumny. Jedna kolumna może zasilić dwa pola (np. „Kod” = ID i nazwa skrócona)."""
    normalized = [normalize_header(h) for h in headers]
    mapping = {}
    for field_name, aliases in ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field_name] = normalized.index(alias)
                break
    return mapping


def parse_csv(data: bytes) -> tuple[list[CustomerRecord], int, list[str]]:
    """Zwraca (rekordy, liczba pominiętych wierszy, ostrzeżenia). Rzuca CsvFormatError."""
    text = decode(data)
    if not text.strip():
        raise CsvFormatError("Plik jest pusty.")
    first_line = text.lstrip("\r\n").splitlines()[0]
    reader = csv.reader(io.StringIO(text), delimiter=sniff_delimiter(first_line))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise CsvFormatError("Plik jest pusty.")
    mapping = map_columns(rows[0])
    if "name" not in mapping:
        raise CsvFormatError("Brak kolumny z nazwą kontrahenta (oczekiwano np. „Nazwa”).")

    records, skipped, warnings = [], 0, []
    for line_no, row in enumerate(rows[1:], start=2):
        def cell(key, row=row):
            idx = mapping.get(key)
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        name = cell("name")
        if not name:
            skipped += 1
            warnings.append(f"Wiersz {line_no}: brak nazwy — pominięto.")
            continue
        street = cell("street")
        number = cell("house_no")
        if number:
            street = f"{street} {number}".strip()
            if cell("flat_no"):
                street = f"{street}/{cell('flat_no')}"
        if cell("postal_code"):
            street = f"{street}, {cell('postal_code')}" if street else cell("postal_code")
        active = _flag(cell("active")) if "active" in mapping else None
        if "blocked" in mapping and _flag(cell("blocked")):
            active = False
        records.append(CustomerRecord(
            external_id=cell("external_id"), name=name, short_name=cell("short_name"), nip=cell("nip"),
            city=cell("city"), address=street, phone=cell("phone"), email=cell("email"), active=active,
        ))
    return records, skipped, warnings


class CsvCustomerSource:
    """Źródło CSV: cały plik w partiach po PAGE_SIZE wierszy. Nie ma kursora (`next_cursor=None`)."""

    def __init__(self, data: bytes, page_size: int = PAGE_SIZE):
        self.data = data
        self.page_size = page_size

    def iter_records(self, cursor: str = "") -> Iterator[Page]:
        records, skipped, warnings = parse_csv(self.data)
        if not records:
            yield Page(records=[], skipped=skipped, warnings=warnings)
            return
        for start in range(0, len(records), self.page_size):
            first = start == 0
            yield Page(records=records[start:start + self.page_size],
                       skipped=skipped if first else 0, warnings=warnings if first else [])
