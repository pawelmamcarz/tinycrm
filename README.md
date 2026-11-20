# TinyCRM

Prosty CRM dla handlowców w terenie: notatki głosowe → zadania dla biura, integracja z mailem i enova365.

- Research: [docs/research.md](docs/research.md)
- Plan wizyty u klienta: [docs/discovery.md](docs/discovery.md)
- Plan MVP: [docs/mvp-plan.md](docs/mvp-plan.md)

## Prototyp — jak uruchomić

Wymagania: Python 3.12+, PostgreSQL 16 (lokalnie), opcjonalnie Redis (bez niego Celery działa w trybie
eager — zadania wykonują się od razu, bez workera).

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                       # ustaw DATABASE_URL do lokalnego Postgresa
createdb tinycrm                           # albo przez psql: CREATE DATABASE tinycrm;
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo       # dane demo + konta (hasło na końcu outputu)
.venv/bin/python manage.py setup_periodic  # zadania okresowe do django-celery-beat (--dry-run = podgląd)
.venv/bin/python manage.py runserver       # http://127.0.0.1:8000/
```

Testy (osobna baza testowa, pełny zestaw ok. 20 s):

```bash
TEST_DB_SUFFIX=local .venv/bin/pytest --create-db
.venv/bin/python manage.py check && .venv/bin/python manage.py makemigrations --check --dry-run
```

### Konta demo

`seed_demo` tworzy organizację **Demo Med** z fikcyjnymi danymi (25 szpitali i klinik, 30 urządzeń,
zadania, 2 zgłoszenia serwisowe z częściami i 1 notatkę głosową czekającą na potwierdzenie).
Domyślne hasło wszystkich kont: **`Demo-TinyCRM-2026`** — tylko przy `DJANGO_DEBUG=1`; poza trybem deweloperskim
`seed_demo` wymaga własnego hasła (`seed_demo --password ...`). Logowanie ma limit nieudanych prób (per login i per IP).

| Login | Rola | Co zobaczy po zalogowaniu |
|---|---|---|
| `jan` | Handlowiec | aplikacja terenowa `/teren/` — „Nagraj notatkę”, zadania na dziś, notatka z Radomia do potwierdzenia |
| `ewa` | Handlowiec | aplikacja terenowa, własni klienci |
| `ola` | Biuro | kolejka biura `/zadania/biuro/` |
| `marek` | Serwis | pulpit serwisu `/serwis/` — przeglądy po terminie, zgłoszenia, części |
| `szef` | Szef | kolejka biura + eskalacje zaległych zadań |
| `admin` | Administrator | jak szef + panel `/admin/` (superuser) |

`seed_demo` jest idempotentne (ponowne uruchomienie niczego nie dubluje); `seed_demo --reset` usuwa dane
organizacji demo i tworzy je od nowa. Wszystkie nazwy, osoby i NIP-y są fikcyjne (NIP-y zaczynają się od
`000` — taki kod urzędu skarbowego nie istnieje).

### Scenariusz pokazowy

1. Zaloguj się jako `jan` → `/teren/` → „Potwierdź” przy notatce z Radomia → popraw pozycje → „✅ Zatwierdź”.
   Powstają zadania dla biura i handlowca oraz zgłoszenie serwisowe z zamówieniem głowicy.
2. Zaloguj się jako `ola` → kolejka biura: nowe zadanie z ikoną 🎙️ i linkiem do notatki; zmień status przyciskami.
3. Zaloguj się jako `marek` → `/serwis/` → zgłoszenie czeka na część; „Dostarczone” wysyła powiadomienie (🔔).

### Tryb demo i integracje

- **AI / STT**: bez `ANTHROPIC_API_KEY` ekstrakcja działa heurystycznie; `STT_PROVIDER=demo` używa tekstu
  z dyktowania przeglądarki (Web Speech). Samo audio w trybie demo trafia do weryfikacji przez biuro.
- **Web push**: `manage.py generate_vapid_keys` → klucze do `.env` (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`).
  Na iOS push działa tylko z aplikacji dodanej do ekranu początkowego.
- **enova365**: `ENOVA_BASE_URL` + `ENOVA_API_TOKEN` (+ `ENOVA_ORG_SLUG`) włączają codzienną synchronizację
  o 5:00; import jednorazowy z CSV: `/integracje/` albo `manage.py import_customers_csv demo plik.csv`.
- **Zadania okresowe** (wymagają Redis + `celery -A config worker` i `celery -A config beat`): planowanie
  przeglądów, alerty gwarancji, poranny skrót, eskalacje zaległych zadań, eskalacja niepotwierdzonych
  notatek (po 2 h notatka demo przechodzi do weryfikacji biura), retencja nagrań.
- Prototyp ładuje Tailwind/DaisyUI/HTMX/Alpine z CDN — wymaga internetu w przeglądarce.
