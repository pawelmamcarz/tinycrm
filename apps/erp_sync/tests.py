"""Testy erp_sync: parser CSV, adapter enova (MockTransport), upsert, kursor, blokada, widok, izolacja."""
import datetime

import psycopg
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.db import connection
from django.urls import reverse

from apps.customers.models import Customer
from apps.erp_sync import sync as sync_module
from apps.erp_sync import tasks
from apps.erp_sync.adapters.base import CustomerRecord, CustomerSource, Page, normalize_nip
from apps.erp_sync.adapters.csv_source import CsvCustomerSource, CsvFormatError, parse_csv, sniff_delimiter
from apps.erp_sync.adapters.enova import (EnovaClient, EnovaError, EnovaSource, httpx, map_kontrahent,
                                          parse_timestamp)
from apps.erp_sync.models import SyncRun, SyncState, SyncStatus
from apps.erp_sync.registrations import NAV, PERIODIC
from apps.erp_sync.services import import_csv, sync_enova
from apps.erp_sync.sync import ALREADY_RUNNING, SOURCE_CSV, SOURCE_ENOVA, lock_key, run_sync
from apps.users.models import Role, User

CSV_PL = (
    "Kod;Nazwa;NIP;Miejscowość;Ulica;Telefon;E-mail\r\n"
    "SZP-RAD;Szpital Specjalistyczny w Radomiu;PL 796-000-00-01;Radom;ul. Lipowa 5;48 000 00 01;sekretariat@szpital-test.pl\r\n"
    "KLIN-LOD;Klinika Zdrowa Łódź;7250000002;Łódź;al. Kwiatowa 12;42 000 00 02;\r\n"
    ";;;;;;\r\n"
    "BEZ-NAZWY;;123;Kielce;;;\r\n"
)


class ListSource:
    """Źródło testowe: strony podane wprost, opcjonalnie błąd po N stronach."""

    def __init__(self, pages, fail_after=None, error=RuntimeError("enova padła")):
        self.pages = pages
        self.fail_after = fail_after
        self.error = error
        self.cursors = []

    def iter_records(self, cursor=""):
        self.cursors.append(cursor)
        for i, page in enumerate(self.pages):
            if self.fail_after is not None and i >= self.fail_after:
                raise self.error
            yield page


def rec(**kwargs):
    kwargs.setdefault("name", "Przychodnia Testowa")
    return CustomerRecord(**kwargs)


# --- adaptery: CSV --------------------------------------------------------------------------------

def test_normalize_nip():
    assert normalize_nip("PL 796-000-00-01") == "7960000001"
    assert normalize_nip(None) == ""


def test_record_strips_whitespace():
    r = CustomerRecord(external_id=" 12 ", name="  Szpital   Miejski ", nip="725-000-00-02")
    assert r.external_id == "12" and r.name == "Szpital Miejski" and r.nip == "7250000002"


def test_sniff_delimiter():
    assert sniff_delimiter("Kod;Nazwa;NIP") == ";"
    assert sniff_delimiter("Kod,Nazwa,NIP") == ","
    assert sniff_delimiter("Kod\tNazwa\tNIP") == "\t"
    assert sniff_delimiter("Nazwa") == ";"


def test_parse_csv_semicolon_cp1250_aliases():
    records, skipped, warnings = parse_csv(CSV_PL.encode("cp1250"))
    assert len(records) == 2
    first = records[0]
    assert first.external_id == "SZP-RAD" and first.short_name == "SZP-RAD"
    assert first.name == "Szpital Specjalistyczny w Radomiu"
    assert first.nip == "7960000001"
    assert first.city == "Radom" and first.address == "ul. Lipowa 5"
    assert first.phone == "48 000 00 01" and first.email == "sekretariat@szpital-test.pl"
    assert records[1].city == "Łódź" and records[1].name == "Klinika Zdrowa Łódź"
    assert skipped == 1 and "brak nazwy" in warnings[0]


def test_parse_csv_utf8_bom_comma_english_headers():
    text = ("ID,Name,Short name,VAT,City,Address,Phone,Email\n"
            "1001,\"Centrum Medyczne Słoneczne, sp. z o.o.\",CMS,7960000003,Kielce,Polna 1,41 000,biuro@cm-test.pl\n")
    records, skipped, _ = parse_csv(text.encode("utf-8-sig"))
    assert skipped == 0 and len(records) == 1
    r = records[0]
    assert r.external_id == "1001" and r.short_name == "CMS" and r.nip == "7960000003"
    assert r.name == "Centrum Medyczne Słoneczne, sp. z o.o." and r.city == "Kielce"


def test_parse_csv_akronim_miasto_adres_and_house_number():
    text = "Akronim;Nazwa;Miasto;Ulica;Nr domu;Nr lokalu;Kod pocztowy;Blokada\nGAB-1;Gabinet Pod Lipą;Opole;Długa;7;2;45-000;tak\n"
    (r,), _, _ = parse_csv(text.encode("utf-8"))
    assert r.external_id == "GAB-1" and r.short_name == "GAB-1" and r.city == "Opole"
    assert r.address == "Długa 7/2, 45-000"
    assert r.active is False


def test_parse_csv_without_name_column_fails():
    with pytest.raises(CsvFormatError):
        parse_csv(b"Kod;NIP\nA;1\n")
    with pytest.raises(CsvFormatError):
        parse_csv(b"   \n")


def test_csv_source_pages():
    lines = "Nazwa;NIP\n" + "".join(f"Gabinet {i};{i:010d}\n" for i in range(5))
    pages = list(CsvCustomerSource(lines.encode(), page_size=2).iter_records(""))
    assert [len(p.records) for p in pages] == [2, 2, 1]
    assert all(p.next_cursor is None for p in pages)
    assert isinstance(CsvCustomerSource(b""), CustomerSource)


# --- adaptery: enova ------------------------------------------------------------------------------

def test_map_kontrahent_nested_and_flat():
    raw = {"ID": 77, "Kod": "SZP-K", "Nazwa": "Szpital Kliniczny", "NIP": "796-000-00-09",
           "Adres": {"Miejscowosc": "Kraków", "Ulica": "Szeroka", "NrDomu": "3", "KodPocztowyS": "30-001"},
           "Kontakt": {"Telefon": "12 000", "EMAIL": "a@kliniczny-test.pl"},
           "DataModyfikacji": "2026-11-01T10:00:00+01:00", "Blokada": False}
    r = map_kontrahent(raw)
    assert r.external_id == "77" and r.short_name == "SZP-K" and r.nip == "7960000009"
    assert r.city == "Kraków" and r.address == "Szeroka 3, 30-001"
    assert r.phone == "12 000" and r.email == "a@kliniczny-test.pl"
    assert r.updated_at == datetime.datetime(2026, 11, 1, 9, 0, tzinfo=datetime.timezone.utc)
    assert r.active is True
    flat = map_kontrahent({"id": "g-1", "nazwa": "Gabinet", "adres": "ul. Krótka 1", "miejscowosc": "Gdańsk"})
    assert flat.external_id == "g-1" and flat.address == "ul. Krótka 1" and flat.city == "Gdańsk"
    assert map_kontrahent({"Nazwa": "Bez ID"}) is None
    assert map_kontrahent({"ID": 1}) is None
    assert map_kontrahent("nie-słownik") is None
    assert map_kontrahent({"Kod": "K1", "Nazwa": "Tylko kod"}).external_id == "K1"


def test_parse_timestamp_formats():
    assert parse_timestamp("/Date(1700000000000+0100)/") == datetime.datetime.fromtimestamp(
        1700000000, tz=datetime.timezone.utc)
    assert parse_timestamp("2026-11-02").tzinfo is not None
    assert parse_timestamp("bzdura") is None
    assert parse_timestamp(None) is None


def _enova_transport(dataset, calls, page_size=2, status=200):
    def handler(request):
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": "x"})
        page = int(request.url.params["page"])
        since = request.url.params.get("zmienioneOd")
        items = [d for d in dataset if not since or parse_timestamp(d["DataModyfikacji"]) >= parse_timestamp(since)]
        chunk = items[(page - 1) * page_size: page * page_size]
        return httpx.Response(200, json={"Items": chunk, "HasMore": page * page_size < len(items)})
    return httpx.MockTransport(handler)


ENOVA_DATA = [
    {"ID": 1, "Kod": "A", "Nazwa": "Szpital A", "NIP": "7960000011", "DataModyfikacji": "2026-11-01T08:00:00+00:00"},
    {"ID": 2, "Kod": "B", "Nazwa": "Szpital B", "NIP": "7960000012", "DataModyfikacji": "2026-11-03T08:00:00+00:00"},
    {"ID": 3, "Kod": "C", "Nazwa": "Klinika C", "DataModyfikacji": "2026-11-02T08:00:00+00:00"},
    {"Nazwa": "Bez identyfikatora", "DataModyfikacji": "2026-11-02T09:00:00+00:00"},
    {"ID": 5, "Kod": "E", "Nazwa": "Gabinet E", "DataModyfikacji": "2026-11-01T09:00:00+00:00"},
]


def test_enova_client_paging_auth_and_params(settings):
    settings.ENOVA_API_TOKEN = "token-testowy"
    calls = []
    client = EnovaClient(base_url="https://enova.test", page_size=2, transport=_enova_transport(ENOVA_DATA, calls))
    pages = list(client.iter_pages())
    assert [len(p) for p in pages] == [2, 2, 1]
    assert calls[0].headers["Authorization"] == "Bearer token-testowy"
    assert calls[0].url.path == "/api/Kontrahenci"
    assert calls[0].url.params["pageSize"] == "2" and "zmienioneOd" not in calls[0].url.params
    assert [c.url.params["page"] for c in calls] == ["1", "2", "3"]


def test_enova_client_list_payload_and_total_pages():
    def handler(request):
        page = int(request.url.params["strona"])
        if request.url.path == "/lista":
            return httpx.Response(200, json=[{"ID": page}] * (2 if page == 1 else 1))
        return httpx.Response(200, json={"value": [{"ID": page}], "TotalPages": 2})
    transport = httpx.MockTransport(handler)
    client = EnovaClient(base_url="https://enova.test", endpoint="/lista", page_size=2, transport=transport,
                         param_names={"page": "strona"})
    assert [len(p) for p in client.iter_pages()] == [2, 1]
    client = EnovaClient(base_url="https://enova.test", endpoint="/inne", page_size=2, transport=transport,
                         param_names={"page": "strona"})
    assert len(list(client.iter_pages())) == 2


def test_enova_client_errors_and_guard():
    client = EnovaClient(base_url="https://enova.test", transport=_enova_transport([], [], status=500))
    with pytest.raises(EnovaError, match="HTTP 500"):
        list(client.iter_pages())
    bad_json = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(EnovaError, match="JSON"):
        list(EnovaClient(base_url="https://enova.test", transport=bad_json).iter_pages())

    def broken(request):
        raise httpx.ConnectError("odmowa", request=request)
    with pytest.raises(EnovaError, match="połączenia"):
        list(EnovaClient(base_url="https://enova.test", transport=httpx.MockTransport(broken)).iter_pages())
    endless = httpx.MockTransport(lambda r: httpx.Response(200, json={"Items": [{"ID": 1}], "HasMore": True}))
    with pytest.raises(EnovaError, match="limit"):
        list(EnovaClient(base_url="https://enova.test", transport=endless, max_pages=3).iter_pages())


def test_enova_client_requires_base_url(settings):
    settings.ENOVA_BASE_URL = ""
    with pytest.raises(EnovaError):
        EnovaClient()


def test_enova_source_cursor_is_max_updated_at():
    calls = []
    client = EnovaClient(base_url="https://enova.test", page_size=2, transport=_enova_transport(ENOVA_DATA, calls))
    pages = list(EnovaSource(client).iter_records(""))
    assert sum(len(p.records) for p in pages) == 4
    assert sum(p.skipped for p in pages) == 1
    assert pages[-1].next_cursor == "2026-11-03T08:00:00+00:00"


# --- synchronizacja -------------------------------------------------------------------------------

def test_csv_import_creates_customers(org):
    run = import_csv(org, CSV_PL.encode("cp1250"))
    assert run.status == SyncStatus.OK
    assert (run.created, run.updated, run.skipped) == (2, 0, 1)
    assert "brak nazwy" in run.error
    c = Customer.objects.get(organization=org, enova_id="SZP-RAD")
    assert c.nip == "7960000001" and c.city == "Radom" and c.is_active
    state = SyncState.objects.get(organization=org, source=SOURCE_CSV)
    assert state.last_success_at is not None and state.cursor == ""


def test_upsert_idempotent_second_run_updates_not_duplicates(org):
    import_csv(org, CSV_PL.encode("cp1250"))
    again = import_csv(org, CSV_PL.encode("cp1250"))
    assert (again.created, again.updated) == (0, 0)
    assert again.skipped == 3  # 2 bez zmian + 1 bez nazwy
    changed = CSV_PL.replace("ul. Lipowa 5", "ul. Lipowa 7")
    third = import_csv(org, changed.encode("cp1250"))
    assert (third.created, third.updated) == (0, 1)
    assert Customer.objects.filter(organization=org).count() == 2
    assert Customer.objects.get(enova_id="SZP-RAD").address == "ul. Lipowa 7"


def test_nip_fallback_links_existing_customer(org):
    existing = Customer.objects.create(organization=org, name="Szpital w Radomiu (ręcznie)", nip="7960000001")
    run = run_sync(org, ListSource([Page([rec(external_id="E-1", name="Szpital Specjalistyczny", nip="796-000-00-01")])]),
                   SOURCE_ENOVA)
    assert (run.created, run.updated) == (0, 1)
    existing.refresh_from_db()
    assert existing.enova_id == "E-1" and existing.name == "Szpital Specjalistyczny"
    assert Customer.objects.filter(organization=org).count() == 1


def test_nip_fallback_skips_customer_already_linked_and_ambiguous(org):
    Customer.objects.create(organization=org, name="Oddział 1", nip="7960000001", enova_id="X-1")
    run = run_sync(org, ListSource([Page([rec(external_id="X-2", name="Oddział 2", nip="7960000001")])]), SOURCE_ENOVA)
    assert run.created == 1  # oddziały z tym samym NIP-em to osobni kontrahenci
    Customer.objects.create(organization=org, name="Bez ID 1", nip="7960000099")
    Customer.objects.create(organization=org, name="Bez ID 2", nip="7960000099")
    run = run_sync(org, ListSource([Page([rec(external_id="X-3", name="Nowy", nip="7960000099")])]), SOURCE_ENOVA)
    assert run.created == 1 and "kilku klientów" in run.error


def test_record_without_id_matches_by_nip_then_name(org):
    c1 = Customer.objects.create(organization=org, name="Gabinet Uśmiech", nip="7960000005")
    c2 = Customer.objects.create(organization=org, name="Przychodnia Pod Dębem")
    run = run_sync(org, ListSource([Page([rec(name="Gabinet Uśmiech Sp. z o.o.", nip="7960000005", city="Kielce"),
                                          rec(name="przychodnia pod dębem", phone="600 000 000")])]), SOURCE_CSV)
    assert (run.created, run.updated) == (0, 2)
    c1.refresh_from_db()
    c2.refresh_from_db()
    assert c1.city == "Kielce" and c1.enova_id == "" and c2.phone == "600 000 000"


def test_owner_notes_email_domain_preserved_and_empty_not_cleared(org, salesperson):
    c = Customer.objects.create(organization=org, name="Stara nazwa", enova_id="E-9", owner=salesperson,
                                notes="Lubi kawę", email_domain="szpital-test.pl", phone="111", city="Radom")
    run_sync(org, ListSource([Page([rec(external_id="E-9", name="Nowa nazwa", phone="", city="")])]), SOURCE_ENOVA)
    c.refresh_from_db()
    assert c.name == "Nowa nazwa"
    assert c.owner == salesperson and c.notes == "Lubi kawę" and c.email_domain == "szpital-test.pl"
    assert c.phone == "111" and c.city == "Radom"


def test_invalid_email_ignored_and_long_values_truncated(org):
    run = run_sync(org, ListSource([Page([rec(external_id="L", name="N" * 300, email="to-nie-mail",
                                              short_name="S" * 150)])]), SOURCE_ENOVA)
    assert run.status == SyncStatus.OK
    c = Customer.objects.get(enova_id="L")
    assert c.email == "" and len(c.name) == 255 and len(c.short_name) == 100
    run = run_sync(org, ListSource([Page([rec(external_id="X" * 60)])]), SOURCE_ENOVA)
    assert run.skipped == 1 and "za długie" in run.error


def test_active_flag_from_source(org):
    c = Customer.objects.create(organization=org, name="Gabinet", enova_id="A1")
    run_sync(org, ListSource([Page([rec(external_id="A1", name="Gabinet", active=False)])]), SOURCE_ENOVA)
    c.refresh_from_db()
    assert c.is_active is False
    run_sync(org, ListSource([Page([rec(external_id="A1", name="Gabinet")])]), SOURCE_ENOVA)
    c.refresh_from_db()
    assert c.is_active is False  # brak informacji = bez zmian


def test_success_saves_cursor_and_passes_it_next_time(org):
    source = ListSource([Page([rec(external_id="1")], next_cursor="2026-11-01T00:00:00+00:00"),
                         Page([rec(external_id="2")], next_cursor="2026-11-02T00:00:00+00:00")])
    run = run_sync(org, source, SOURCE_ENOVA)
    assert run.status == SyncStatus.OK and run.finished_at
    state = SyncState.objects.get(organization=org, source=SOURCE_ENOVA)
    assert state.cursor == "2026-11-02T00:00:00+00:00"
    run_sync(org, source, SOURCE_ENOVA)
    assert source.cursors == ["", "2026-11-02T00:00:00+00:00"]
    run_sync(org, source, SOURCE_ENOVA, full=True)
    assert source.cursors[-1] == ""


def test_failure_keeps_cursor_and_marks_run_blad(org):
    state = SyncState.objects.create(organization=org, source=SOURCE_ENOVA, cursor="2026-10-01T00:00:00+00:00")
    source = ListSource([Page([rec(external_id="1")], next_cursor="2026-11-05T00:00:00+00:00"),
                         Page([rec(external_id="2")])], fail_after=1)
    run = run_sync(org, source, SOURCE_ENOVA)
    assert run.status == SyncStatus.BLAD and "enova padła" in run.error and run.finished_at
    assert run.created == 1  # pierwsza strona zatwierdzona (upsert idempotentny)
    state.refresh_from_db()
    assert state.cursor == "2026-10-01T00:00:00+00:00" and state.last_success_at is None


def test_failure_inside_page_rolls_back_that_page(org, monkeypatch):
    original = sync_module.upsert_record

    def flaky(organization, record, result):
        if record.external_id == "BOOM":
            raise ValueError("zły rekord")
        return original(organization, record, result)

    monkeypatch.setattr(sync_module, "upsert_record", flaky)
    run = run_sync(org, ListSource([Page([rec(external_id="ok-1"), rec(external_id="BOOM")])]), SOURCE_ENOVA)
    assert run.status == SyncStatus.BLAD and run.created == 0
    assert not Customer.objects.filter(organization=org).exists()


def test_lock_released_after_run(org):
    run_sync(org, ListSource([Page([rec(external_id="1")])], fail_after=0), SOURCE_ENOVA)
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
        assert cur.fetchone()[0] == 0


def test_lock_keys_differ_per_org_and_source(org, other_org):
    assert lock_key(org, SOURCE_ENOVA) != lock_key(other_org, SOURCE_ENOVA)
    assert lock_key(org, SOURCE_ENOVA) != lock_key(org, SOURCE_CSV)
    assert lock_key(org, SOURCE_ENOVA) == lock_key(org, SOURCE_ENOVA)


def _second_connection():
    s = connection.settings_dict
    return psycopg.connect(dbname=s["NAME"], user=s["USER"], password=s["PASSWORD"], host=s["HOST"] or None,
                           port=s["PORT"] or None, autocommit=True)


def test_concurrent_run_is_skipped(org):
    key = lock_key(org, SOURCE_ENOVA)
    with _second_connection() as other:
        other.execute("SELECT pg_advisory_lock(%s)", [key])
        source = ListSource([Page([rec(external_id="1")])])
        run = run_sync(org, source, SOURCE_ENOVA)
        assert run.status == SyncStatus.BLAD and run.error == ALREADY_RUNNING
        assert source.cursors == []  # źródło nawet nie zostało odpytane
        assert not Customer.objects.filter(organization=org).exists()
        # Inne źródło tej samej organizacji nie jest blokowane.
        assert run_sync(org, ListSource([Page([rec(external_id="1")])]), SOURCE_CSV).status == SyncStatus.OK
        other.execute("SELECT pg_advisory_unlock(%s)", [key])
    assert run_sync(org, ListSource([Page([rec(external_id="2")])]), SOURCE_ENOVA).status == SyncStatus.OK


def test_org_isolation_in_sync(org, other_org):
    foreign = Customer.objects.create(organization=other_org, name="Obcy szpital", enova_id="E-1", nip="7960000001")
    run = run_sync(org, ListSource([Page([rec(external_id="E-1", name="Nasz szpital", nip="7960000001")])]),
                   SOURCE_ENOVA)
    assert run.created == 1
    foreign.refresh_from_db()
    assert foreign.name == "Obcy szpital"
    assert Customer.objects.filter(organization=org, enova_id="E-1").exists()
    assert not SyncState.objects.filter(organization=other_org).exists()


def test_sync_enova_service_end_to_end(org):
    calls = []
    client = EnovaClient(base_url="https://enova.test", page_size=2, transport=_enova_transport(ENOVA_DATA, calls))
    run = sync_enova(org, client=client)
    assert run.status == SyncStatus.OK and (run.created, run.skipped) == (4, 1)
    assert SyncState.objects.get(organization=org, source=SOURCE_ENOVA).cursor == "2026-11-03T08:00:00+00:00"
    calls.clear()
    second = sync_enova(org, client=client)
    assert calls[0].url.params["zmienioneOd"] == "2026-11-03T08:00:00+00:00"
    assert (second.created, second.updated, second.skipped) == (0, 0, 1)
    assert Customer.objects.filter(organization=org).count() == 4


def test_sync_enova_http_error_keeps_cursor(org):
    SyncState.objects.create(organization=org, source=SOURCE_ENOVA, cursor="2026-10-01T00:00:00+00:00")
    client = EnovaClient(base_url="https://enova.test", transport=_enova_transport([], [], status=503))
    run = sync_enova(org, client=client)
    assert run.status == SyncStatus.BLAD and "HTTP 503" in run.error
    assert SyncState.objects.get(organization=org, source=SOURCE_ENOVA).cursor == "2026-10-01T00:00:00+00:00"


# --- zadanie okresowe, komendy, rejestracje -------------------------------------------------------

def test_daily_task_skips_without_config(settings, org):
    settings.ENOVA_BASE_URL = ""
    assert "skipped" in tasks.daily_enova_sync()
    assert not SyncRun.objects.exists()


def test_daily_task_runs_for_configured_org(settings, org, other_org, monkeypatch):
    settings.ENOVA_BASE_URL = "https://enova.test"
    settings.ENOVA_ORG_SLUG = org.slug
    seen = []

    def fake_sync(organization, full=False, client=None):
        seen.append(organization)
        return SyncRun.objects.create(organization=organization, source=SOURCE_ENOVA, status=SyncStatus.OK)

    monkeypatch.setattr(tasks, "sync_enova", fake_sync)
    assert tasks.daily_enova_sync() == {org.slug: SyncStatus.OK}
    assert seen == [org]


def test_daily_task_ambiguous_org_skips(settings, org, other_org):
    settings.ENOVA_BASE_URL = "https://enova.test"
    settings.ENOVA_ORG_SLUG = ""
    assert tasks.daily_enova_sync() == {"skipped": "brak organizacji"}


def test_import_customers_csv_command(org, tmp_path, capsys):
    path = tmp_path / "kontrahenci.csv"
    path.write_bytes(CSV_PL.encode("cp1250"))
    call_command("import_customers_csv", org.slug, str(path))
    assert "Nowi: 2" in capsys.readouterr().out
    assert Customer.objects.filter(organization=org).count() == 2
    with pytest.raises(CommandError):
        call_command("import_customers_csv", "brak", str(path))
    with pytest.raises(CommandError):
        call_command("import_customers_csv", org.slug, str(tmp_path / "nie-ma.csv"))
    bad = tmp_path / "zly.csv"
    bad.write_bytes(b"Kod;NIP\n1;2\n")
    with pytest.raises(CommandError, match="nieudany"):
        call_command("import_customers_csv", org.slug, str(bad))


def test_sync_enova_command(settings, org, monkeypatch):
    settings.ENOVA_BASE_URL = ""
    with pytest.raises(CommandError, match="ENOVA_BASE_URL"):
        call_command("sync_enova", org.slug)
    settings.ENOVA_BASE_URL = "https://enova.test"
    from apps.erp_sync.management.commands import sync_enova as cmd

    monkeypatch.setattr(cmd, "sync_enova", lambda organization, full=False: SyncRun.objects.create(
        organization=organization, source=SOURCE_ENOVA, status=SyncStatus.OK, created=3))
    call_command("sync_enova", org.slug, "--full")
    monkeypatch.setattr(cmd, "sync_enova", lambda organization, full=False: SyncRun.objects.create(
        organization=organization, source=SOURCE_ENOVA, status=SyncStatus.BLAD, error="awaria"))
    with pytest.raises(CommandError, match="awaria"):
        call_command("sync_enova", org.slug)


def test_registrations():
    assert NAV[0]["url_name"] == "erp_sync:status" and NAV[0]["order"] == 80
    assert set(NAV[0]["roles"]) == {"SZEF", "ADMIN", "BIURO"}
    assert PERIODIC == [{"name": "erp-enova-daily", "task": "apps.erp_sync.tasks.daily_enova_sync",
                         "crontab": {"hour": 5, "minute": 0}}]


# --- widok ----------------------------------------------------------------------------------------

URL = "erp_sync:status"


@pytest.mark.parametrize("fixture", ["office_user", "boss"])
def test_status_view_allowed_roles(client, request, fixture, org):
    client.force_login(request.getfixturevalue(fixture))
    SyncRun.objects.create(organization=org, source=SOURCE_CSV, status=SyncStatus.OK, created=5)
    response = client.get(reverse(URL))
    assert response.status_code == 200
    assert "Ostatnie przebiegi" in response.content.decode()


def test_status_view_admin_role(client, org):
    admin = User.objects.create_user(username="adm", password="haslo-testowe-123", organization=org, role=Role.ADMIN)
    client.force_login(admin)
    assert client.get(reverse(URL)).status_code == 200


@pytest.mark.parametrize("fixture", ["salesperson", "service_user"])
def test_status_view_forbidden_roles(client, request, fixture):
    client.force_login(request.getfixturevalue(fixture))
    assert client.get(reverse(URL)).status_code == 403
    upload = SimpleUploadedFile("k.csv", CSV_PL.encode("cp1250"), content_type="text/csv")
    assert client.post(reverse(URL), {"file": upload}).status_code == 403
    assert not Customer.objects.exists()


def test_status_view_requires_login(client):
    response = client.get(reverse(URL))
    assert response.status_code == 302 and "next=" in response["Location"]


def test_status_view_org_isolation(client, office_user, org, other_org):
    SyncRun.objects.create(organization=other_org, source="obca:zrodlo-xyz", status=SyncStatus.BLAD, error="tajne")
    SyncState.objects.create(organization=other_org, source="obca:zrodlo-xyz", cursor="2026-01-01")
    client.force_login(office_user)
    body = client.get(reverse(URL)).content.decode()
    assert "obca:zrodlo-xyz" not in body and "tajne" not in body


def test_csv_upload_runs_import_for_own_org(client, office_user, org, other_org):
    client.force_login(office_user)
    upload = SimpleUploadedFile("kontrahenci.CSV", CSV_PL.encode("cp1250"), content_type="text/csv")
    response = client.post(reverse(URL), {"file": upload}, follow=True)
    assert response.status_code == 200
    body = response.content.decode()
    assert "Import zakończony" in body and "nowi: 2" in body
    assert Customer.objects.filter(organization=org).count() == 2
    assert not Customer.objects.filter(organization=other_org).exists()
    run = SyncRun.objects.get(organization=org)
    assert response.redirect_chain[-1][0].endswith(f"?przebieg={run.pk}")


def test_csv_upload_bad_content_shows_error(client, office_user, org):
    client.force_login(office_user)
    upload = SimpleUploadedFile("k.csv", b"Kod;NIP\n1;2\n", content_type="text/csv")
    body = client.post(reverse(URL), {"file": upload}, follow=True).content.decode()
    assert "Import nieudany" in body
    assert SyncRun.objects.get(organization=org).status == SyncStatus.BLAD


def test_csv_upload_validation(client, office_user, monkeypatch):
    client.force_login(office_user)
    wrong_ext = SimpleUploadedFile("k.xlsx", b"Nazwa\nA\n")
    response = client.post(reverse(URL), {"file": wrong_ext})
    assert response.status_code == 400 and ".csv" in response.content.decode()
    from apps.erp_sync import forms
    monkeypatch.setattr(forms, "MAX_CSV_BYTES", 10)
    too_big = SimpleUploadedFile("k.csv", b"Nazwa\n" + b"A" * 20)
    response = client.post(reverse(URL), {"file": too_big})
    assert response.status_code == 400 and "za duży" in response.content.decode()
    assert client.post(reverse(URL), {}).status_code == 400
    assert not SyncRun.objects.exists()


def test_nav_visible_for_office(client, office_user, salesperson):
    from apps.core.navigation import nav_for_user

    assert any(i["url_name"] == URL for i in nav_for_user(office_user))
    assert not any(i["url_name"] == URL for i in nav_for_user(salesperson))

