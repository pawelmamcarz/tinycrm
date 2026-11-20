# AGENTS.md — TinyCRM

Wytyczne dla agentów AI (Claude Code, Codex). `CLAUDE.md` jest lustrem tego pliku.
Język UI, komentarzy i dokumentacji: **polski**. Identyfikatory, role i komendy: angielskie/ASCII.

## Produkt
Prosty CRM dla handlowców w terenie (pilot: dystrybutor sprzętu medycznego, 7 handlowców, biuro, serwis).
Zasada: **handlowiec tylko mówi, biuro tylko odhacza**. Zero formularzy dla handlowca, poza ekranem potwierdzenia.
Kontekst: `docs/research.md`, `docs/discovery.md`, `docs/mvp-plan.md`.

## Stack
Python 3.12+, Django 5.2, PostgreSQL 16 (bez SQLite), Celery (bez `REDIS_URL` = tryb eager), szablony Django +
HTMX + Alpine + Tailwind/DaisyUI (w prototypie z CDN), PWA. AI: Claude przez `apps/core/ai.py`.

## Uruchomienie
```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # DATABASE_URL do lokalnego Postgresa
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo      # dane demo + konta (hasło w outpucie)
.venv/bin/python manage.py runserver
.venv/bin/pytest
```

## Moduły i granice
| App | Domena | Publiczne API dla innych modułów |
|---|---|---|
| `core` | Organization, OrgScopedModel, ai, nawigacja, uprawnienia, seed_demo | `ai.ask_json`, `permissions.role_required/RoleRequiredMixin` |
| `users` | User (rola, organizacja), logowanie | `Role` |
| `customers` | klienci, osoby kontaktowe, oś czasu, dopasowanie nazw | `matching.match_customer(org, text)`, `services.create_customer(org, **pola)`, `services.update_customer(customer, dict)` |
| `tasks` | zadania, FSM, kolejka biura, „moje zadania” | `services.create_task(...)`, `services.change_status(...)`, `services.pick_office_assignee(org)`, `services.close_system_tasks(org, source_ref)` |
| `voice` | notatka głosowa → transkrypcja → ekstrakcja → potwierdzenie | `services.process_voice_note(note)` |
| `service` | urządzenia, przeglądy, zgłoszenia, zamówienia części | `services.create_ticket(...)`, `services.create_part_order(...)` |
| `notify` | powiadomienia w aplikacji, web push, skrót dnia, eskalacje | `services.notify(user, title, body, url)`, `once_per_day(key)` |
| `erp_sync` | import kontrahentów: CSV + enova365 WebAPI (adapter) | `services.import_csv(org, bytes)`, `services.sync_enova(org, full=False)`; komendy `import_customers_csv`, `sync_enova` |
| `pwa` | aplikacja terenowa: Nagraj, Dziś, Klienci; manifest, service worker | — |

Reguły:
- Moduł zmienia dane innego modułu **wyłącznie przez jego `services.py`**.
- Każdy model domenowy dziedziczy `OrgScopedModel`; każde zapytanie w widoku: `Model.objects.for_org(request.organization)`.
  Obiekt z innej organizacji → 404 (fail-closed). Testy izolacji są obowiązkowe.
- Statusy zmieniamy przyciskami (POST) walidowanymi tabelą `*_LEGAL_TRANSITIONS`, nigdy polem w formularzu edycji.
- Nawigacja: `apps/<app>/registrations.py` z listą `NAV` (patrz `apps/core/navigation.py`).
- Oś czasu klienta: `apps/<app>/registrations.py` może eksportować `TIMELINE = [fn]`, gdzie
  `fn(customer, user=None) -> list[dict(date: datetime, icon, title, text, url)]`; przy `user` źródło
  zwraca tylko wpisy, które ten użytkownik może zobaczyć w module-właścicielu (źródło bez `user` jest pomijane).
- Zadania okresowe: `apps/<app>/tasks.py` (Celery `shared_task`) + wpis w `PERIODIC` w `registrations.py`
  (`{"name", "task", "crontab": {"hour":7,"minute":0}}`); `manage.py setup_periodic` zapisuje je do beat.
- Daty: `timezone.localdate()`, nie `.date()`. Dane do JS: `json_script`.
- AI: `ask_json` zwraca `{"_error": ...}` zamiast wyjątku; zawsze obsłuż `_error` i miej fallback (tryb demo bez klucza).
- `source_ref` zadania: `voice:<id>`, `service_ticket:<id>`, `inspection:<id>` (link do źródła w `tasks:detail`).
- Ustawienia integracji (opcjonalne, `.env`): `SITE_URL`, `ENOVA_ORG_SLUG`, `ENOVA_KONTRAHENCI_ENDPOINT`,
  `ENOVA_PAGE_SIZE`, `VAPID_*` (klucze: `manage.py generate_vapid_keys`).
- Błędy HTMX (4xx/5xx) pokazuje globalny toast w `templates/base/base.html` — nie dodawaj lokalnych handlerów.
- Role: `HANDLOWIEC`, `BIURO`, `SERWIS`, `SZEF`, `ADMIN`. Strona startowa: `apps/core/views.py:ROLE_HOMES`.
- Sekrety tylko w `.env`. Repo jest publiczne: żadnych danych klienta, nazw firm, nazwisk.

## Kontrakt nazw URL (używane w szablonach między modułami)
`core:home`, `users:login`, `users:logout`,
`customers:list`, `customers:detail <pk>`, `customers:search` (HTMX), `customers:create`, `customers:edit <pk>`,
`customers:contact_add <pk>` (POST),
`tasks:office`, `tasks:mine`, `tasks:detail <pk>`, `tasks:create`, `tasks:status <pk>` (POST),
`voice:upload` (POST, multipart/JSON), `voice:detail <pk>` (ekran potwierdzenia), `voice:confirm <pk>` (POST), `voice:list`,
`service:dashboard`, `service:devices`, `service:device_detail <pk>`, `service:tickets`, `service:ticket_detail <pk>`, `service:ticket_create` (`?customer=<pk>`), `service:parts`,
`notify:list`, `notify:badge` (HTMX), `notify:subscribe` (POST), `notify:vapid_key`,
`erp_sync:status`,
`pwa:home`, `pwa:record` (`?klient=<pk>`), `pwa:manifest`, `pwa:service_worker`, `pwa:offline`.

## Testy
`pytest` (pytest-django, Postgres, `config.settings.test`). Fixtury w `conftest.py`: `org`, `other_org`,
`salesperson`, `office_user`, `service_user`, `boss`, `other_org_user`. Każdy moduł ma `apps/<app>/tests.py`
pokrywający: logikę serwisów, FSM, uprawnienia ról i izolację organizacji.
`apps/core/tests_smoke.py`: każda rola × każda pozycja NAV (200 albo 403/302, nigdy 500), strony szczegółów
z `seed_demo`, idempotencja seeda i `setup_periodic`. Nowa pozycja NAV musi przejść ten test.
Osobna baza per proces: `TEST_DB_SUFFIX=<nazwa> .venv/bin/pytest --create-db`.
