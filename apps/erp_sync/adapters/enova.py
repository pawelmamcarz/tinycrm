"""Adapter enova365 WebAPI (kontrahenci).

UWAGA — do potwierdzenia u klienta: dokładna ścieżka endpointu, nazwy parametrów stronicowania
i filtra „zmienione od” oraz nazwy pól zależą od wersji enova365 i od tego, jak wdrożeniowiec
skonfigurował Dynamic WebAPI (lub własny serwis WebAPI). Domyślne wartości poniżej
(`/api/Kontrahenci`, `page`/`pageSize`/`zmienioneOd`, pola `ID`/`Kod`/`Nazwa`/`NIP`/`Adres`/`Kontakt`)
są założeniem prototypu — przed uruchomieniem produkcyjnym sprawdź je na środowisku testowym
klienta i nadpisz przez ustawienia (`ENOVA_KONTRAHENCI_ENDPOINT`, `ENOVA_PAGE_SIZE`,
`ENOVA_PARAM_NAMES`) albo argumenty `EnovaClient`.

Mapowanie pól jest defensywne: klucze bez względu na wielkość liter, kilka aliasów, zagnieżdżone
obiekty (`Adres.Miejscowosc`) i płaskie (`AdresMiejscowosc`). Rekord bez ID albo nazwy jest pomijany.
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Iterator

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

try:  # pragma: no cover — zależnie od środowiska
    import httpx
except ModuleNotFoundError:  # pragma: no cover — w venv jest fork `httpx2` (zależność SDK anthropic)
    import httpx2 as httpx

from .base import CustomerRecord, Page

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "/api/Kontrahenci"
DEFAULT_PAGE_SIZE = 100
DEFAULT_PARAM_NAMES = {"page": "page", "page_size": "pageSize", "changed_since": "zmienioneOd"}
MAX_PAGES = 1000  # bezpiecznik: serwer ignorujący stronicowanie nie zapętli synchronizacji


class EnovaError(RuntimeError):
    """Błąd komunikacji z enova365 (sieć, HTTP, niepoprawny JSON). Komunikat bez sekretów."""


def _lower_keys(data: dict) -> dict:
    return {str(k).lower(): v for k, v in data.items()} if isinstance(data, dict) else {}


def _pick(data: dict, *paths: str) -> Any:
    """Pierwsza niepusta wartość spośród ścieżek („Adres.Miejscowosc”), bez względu na wielkość liter."""
    for path in paths:
        current: Any = data
        for part in path.split("."):
            current = _lower_keys(current).get(part.lower()) if isinstance(current, dict) else None
            if current is None:
                break
        if current not in (None, "", [], {}):
            return current
    return None


def _text(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


_MS_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def parse_timestamp(value) -> datetime.datetime | None:
    """ISO 8601, sama data albo format WCF „/Date(1700000000000+0100)/”. Zwraca aware datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        result = value
    else:
        text = str(value).strip()
        match = _MS_DATE.fullmatch(text)
        if match:
            return datetime.datetime.fromtimestamp(int(match.group(1)) / 1000, tz=datetime.timezone.utc)
        try:
            result = parse_datetime(text)
        except ValueError:
            result = None
        if result is None:
            try:
                day = parse_date(text)
            except ValueError:
                day = None
            if day is None:
                return None
            result = datetime.datetime.combine(day, datetime.time.min)
    if timezone.is_naive(result):
        result = timezone.make_aware(result)
    return result


def _active(raw: dict) -> bool | None:
    blocked = _pick(raw, "Blokada", "Zablokowany", "Archiwalny", "Archived")
    if isinstance(blocked, bool):
        return not blocked
    if isinstance(blocked, str) and blocked.strip().lower() in ("true", "tak", "1"):
        return False
    if isinstance(blocked, str) and blocked.strip().lower() in ("false", "nie", "0"):
        return True
    return None


def map_kontrahent(raw: dict) -> CustomerRecord | None:
    """Mapuje surowy obiekt kontrahenta z enova na CustomerRecord. None = rekord nieużywalny."""
    if not isinstance(raw, dict):
        return None
    external_id = _text(_pick(raw, "ID", "Id", "Guid", "IdKontrahenta", "KontrahentID"))
    code = _text(_pick(raw, "Kod", "Akronim", "Symbol"))
    name = _text(_pick(raw, "Nazwa", "NazwaPelna", "PelnaNazwa", "Name"))
    if not external_id and code:
        external_id = code  # w części konfiguracji kod jest jedynym stabilnym kluczem
    if not external_id or not name:
        return None

    street = _text(_pick(raw, "Adres.Ulica", "AdresUlica", "Ulica"))
    house = _text(_pick(raw, "Adres.NrDomu", "AdresNrDomu", "NrDomu"))
    flat = _text(_pick(raw, "Adres.NrLokalu", "AdresNrLokalu", "NrLokalu"))
    postal = _text(_pick(raw, "Adres.KodPocztowyS", "Adres.KodPocztowy", "AdresKodPocztowy", "KodPocztowy"))
    address = " ".join(p for p in (street, house) if p)
    if flat:
        address = f"{address}/{flat}" if address else flat
    if postal:
        address = f"{address}, {postal}" if address else postal
    if not address:  # adres jako jeden napis („Adres”: „ul. Lipowa 5”) albo linia adresu
        address = _text(_pick(raw, "Adres.Linia1")) or _text(_pick(raw, "Adres"))

    return CustomerRecord(
        external_id=external_id,
        name=name,
        short_name=code,
        nip=_text(_pick(raw, "NIP", "EuVAT", "Nip")),
        city=_text(_pick(raw, "Adres.Miejscowosc", "AdresMiejscowosc", "Miejscowosc", "Miasto")),
        address=address,
        phone=_text(_pick(raw, "Kontakt.Telefon", "KontaktTelefon", "Telefon", "Kontakt.TelefonKomorkowy")),
        email=_text(_pick(raw, "Kontakt.EMAIL", "Kontakt.Email", "KontaktEMAIL", "EMAIL", "Email", "E-mail")),
        updated_at=parse_timestamp(_pick(raw, "DataModyfikacji", "ZmienionyO", "Zmieniono", "Stamp",
                                         "ModifiedAt", "LastModified")),
        active=_active(raw),
    )


class EnovaClient:
    """Klient HTTP enova365 (Bearer token). Stronicuje GET-em z filtrem „zmienione od”."""

    def __init__(self, base_url: str | None = None, token: str | None = None, endpoint: str | None = None,
                 page_size: int | None = None, param_names: dict | None = None, timeout: float = 30.0,
                 transport=None, max_pages: int = MAX_PAGES):
        self.base_url = (base_url if base_url is not None else settings.ENOVA_BASE_URL or "").rstrip("/")
        if not self.base_url:
            raise EnovaError("Brak adresu enova365 (ENOVA_BASE_URL).")
        token = token if token is not None else getattr(settings, "ENOVA_API_TOKEN", "")
        self.endpoint = endpoint or getattr(settings, "ENOVA_KONTRAHENCI_ENDPOINT", "") or DEFAULT_ENDPOINT
        self.page_size = int(page_size or getattr(settings, "ENOVA_PAGE_SIZE", 0) or DEFAULT_PAGE_SIZE)
        self.params = {**DEFAULT_PARAM_NAMES, **(getattr(settings, "ENOVA_PARAM_NAMES", None) or {}),
                       **(param_names or {})}
        self.max_pages = max_pages
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=self.base_url, headers=headers, transport=transport,
                                    timeout=httpx.Timeout(timeout, connect=10.0))

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def fetch_page(self, page: int, changed_since: str = "") -> tuple[list[dict], bool]:
        """Jedna strona: (lista surowych kontrahentów, czy są kolejne strony)."""
        params = {self.params["page"]: page, self.params["page_size"]: self.page_size}
        if changed_since:
            params[self.params["changed_since"]] = changed_since
        try:
            response = self._client.get(self.endpoint, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise EnovaError(f"enova365 odpowiedziała błędem HTTP {exc.response.status_code}.") from exc
        except httpx.HTTPError as exc:
            raise EnovaError(f"Błąd połączenia z enova365: {exc.__class__.__name__}.") from exc
        except ValueError as exc:
            raise EnovaError("enova365 zwróciła niepoprawny JSON.") from exc
        return self._items_and_more(payload, page)

    def _items_and_more(self, payload, page: int) -> tuple[list[dict], bool]:
        if isinstance(payload, list):
            return payload, len(payload) >= self.page_size
        if not isinstance(payload, dict):
            raise EnovaError("enova365 zwróciła nieoczekiwany format odpowiedzi.")
        items = _pick(payload, "Items", "Value", "Data", "Rows", "Results", "Kontrahenci")
        if items is None:
            items = []
        if not isinstance(items, list):
            raise EnovaError("enova365 zwróciła nieoczekiwany format listy kontrahentów.")
        has_more = _pick(payload, "HasMore", "HasNextPage", "MoreRecords")
        if isinstance(has_more, bool):
            return items, has_more and bool(items)
        total_pages = _pick(payload, "TotalPages", "PageCount")
        if isinstance(total_pages, int):
            return items, page < total_pages and bool(items)
        return items, len(items) >= self.page_size

    def iter_pages(self, changed_since: str = "") -> Iterator[list[dict]]:
        page = 1
        while True:
            if page > self.max_pages:
                raise EnovaError(f"Przekroczono limit {self.max_pages} stron — sprawdź stronicowanie WebAPI.")
            items, has_more = self.fetch_page(page, changed_since)
            yield items
            if not has_more:
                return
            page += 1


class EnovaSource:
    """Źródło kontrahentów z enova365. Kursor = najpóźniejsza data modyfikacji (ISO 8601).

    Filtr „zmienione od” jest włącznie (>=), więc ostatni rekord wraca w kolejnym przebiegu —
    to bezpieczne, bo upsert jest idempotentny.
    """

    def __init__(self, client: EnovaClient):
        self.client = client

    def iter_records(self, cursor: str = "") -> Iterator[Page]:
        latest = parse_timestamp(cursor) if cursor else None
        for raw_items in self.client.iter_pages(cursor):
            records, skipped = [], 0
            for raw in raw_items:
                record = map_kontrahent(raw)
                if record is None:
                    skipped += 1
                    continue
                records.append(record)
                if record.updated_at and (latest is None or record.updated_at > latest):
                    latest = record.updated_at
            warnings = [f"Pominięto {skipped} rekordów enova bez ID lub nazwy."] if skipped else []
            next_cursor = latest.isoformat() if latest else None
            yield Page(records=records, next_cursor=next_cursor, skipped=skipped, warnings=warnings)
