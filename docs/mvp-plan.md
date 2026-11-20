# TinyCRM — plan MVP

Status: szkic do weryfikacji po wizycie u klienta (patrz `discovery.md`).
Klient pilotażowy: dystrybutor sprzętu medycznego, ok. 30 osób, 7 handlowców w terenie, biuro obsługi, serwis, ERP enova365.

## 1. Cel MVP

> Handlowiec po wizycie nagrywa 30 sekund. Biuro dostaje gotowe zadania. Nic nie ginie, a przeglądy i części pilnują się same.

Kryteria sukcesu pilotażu (po 4 tygodniach, porównanie z „dziennikiem telefonów” sprzed wdrożenia):
- ≥ 70% wizyt pilotażowych handlowców kończy się notatką głosową,
- 0 spraw „zgubionych” (zadanie bez właściciela lub bez terminu),
- spada liczba telefonów handlowiec → biuro,
- 100% urządzeń z bazy ma wyliczony termin następnego przeglądu.

## 2. Decyzja: na czym budujemy

Porównane bazy: **Pyrlandia** (Django) i **KTM backoffice** (Open Mercato / Next.js).

| | Pyrlandia (Django + HTMX + PWA) | Open Mercato (KTM) |
|---|---|---|
| Zadania ze statusami, przypisaniem, źródłem (mail/telefon/spotkanie) | ✅ `apps/tasks` | ✅ workflows + notifications |
| Mail przychodzący + klasyfikacja AI → zadanie | ✅ `apps/email_intake` (IMAP) | ✅ kanał Gmail/IMAP, bez AI |
| Integracja z Claude (kontrakt JSON, telemetria) | ✅ `apps/core/ai.py` | ✅ AI SDK, ai-assistant |
| Rejestr urządzeń, harmonogramy przeglądów, części | ✅ `devices`, `contracts`, `warehouse`, `service` | ⚠️ trzeba dopisać |
| Mobile / PWA dla handlowca | ✅ `apps/pwa`, service worker | ⚠️ panel desktopowy |
| CRM (firmy, osoby, lejek) | ⚠️ `sites` (prostszy) | ✅ `customers` (pełny) |
| Multi-tenant (przyszły SaaS) | ❌ brak | ✅ wbudowany |
| Ciężar dla 15 użytkowników | lekki, znany stack | ciężki (Next 16, worker, ~25 modułów, wersja 0.9 z łatkami) |

**Rekomendacja: stack i moduły z Pyrlandii, nowe repo (nie fork).** Zadecydowały trzy rzeczy. Domena serwisowa (urządzenia, przeglądy, części) jest w Pyrlandii prawie 1:1. PWA dla terenu już tam działa. Stack jest lżejszy i znany.

Z KTM bierzemy **wzorce**, a nie kod:
- wzorzec synchronizacji z ERP (`example_customers_sync` / `DataSyncAdapter`): zewnętrzne ID, kursor, rekoncyliacja, brak przesuwania kursora po błędzie, brak równoległych uruchomień,
- zaszyfrowane dane dostępowe integracji + health check (`ktm_storefront/integration.ts`),
- outbox z kluczami idempotencji dla zdarzeń wychodzących,
- powiadomienie o przekroczeniu terminu (deadline-breach) jako osobne zdarzenie.

**Furtka na SaaS:** od pierwszej migracji każdy model domenowy ma FK `organization`, a zapytania idą przez manager filtrujący po organizacji. Kosztuje to ok. 1 dzień teraz zamiast miesiąca przeróbek później. Pełny multi-tenant (subdomeny, rejestracja) dopiero po pilotażu.

## 3. Stack

- Python 3.12, Django 5.2, PostgreSQL 16+, Celery + Redis + django-celery-beat
- HTMX + Alpine.js + Tailwind 3 + DaisyUI 4 (build esbuild, assety commitowane jak w Pyrlandii)
- PWA: service worker, MediaRecorder API, kolejka offline w IndexedDB (nagranie w aucie bez zasięgu wysyła się później)
- AI: Claude przez kontrakt z `core/ai.py` (strict JSON, fallback, `{"_error"}` zamiast wyjątków)
- Speech-to-text PL: do wyboru po teście na 20 prawdziwych nagraniach (ElevenLabs Scribe / OpenAI transcribe / Deepgram), za interfejsem `Transcriber`, żeby dało się zmienić dostawcę
- Web Push: `pywebpush` (VAPID), fallback na mail/SMS (smsapi, jest w Pyrlandii)
- Pliki: S3/R2 (django-storages), serwowane przez autoryzowane widoki
- Hosting: Railway (region EU), jeden obraz z rolami web/worker/beat, jak `start.sh` w Pyrlandii
- Testy: pytest-django + factory_boy (warstwy pure/db/migration), Playwright E2E pl-PL, CI z GitHub Actions skopiowane z Pyrlandii

## 4. Moduły (Django apps)

| App | Skąd | Zakres w MVP |
|---|---|---|
| `core` | kopia z Pyrlandii, odchudzona | ai, mail, digest, periodic, audit (simple-history), załączniki, uprawnienia, nawigacja |
| `users` | kopia + nowe role | role: HANDLOWIEC, BIURO, SERWIS, SZEF, ADMIN; 2FA opcjonalnie |
| `customers` | na bazie `sites` | firma (NIP, ID z enova), lokalizacje/oddziały szpitala, osoby kontaktowe, oś czasu |
| `tasks` | kopia `apps/tasks` | maszyna stanów, przypisanie, termin, źródło (GŁOS/MAIL/RĘCZNE), typy: oferta, zamówienie, kontakt, serwis, inne; widoki „moje” i „biuro” |
| `voice` | **nowy** | nagranie → transkrypcja → ekstrakcja AI → ekran potwierdzenia → zadania/notatki/zgłoszenia |
| `mailbox` | na bazie `email_intake` | IMAP na start; Gmail API lub Microsoft Graph (OAuth) zależnie od discovery; przypinanie do klienta, propozycja zadania |
| `service` | wycinek `devices` + `contracts` + `service` | urządzenie u klienta (model, S/N, instalacja, gwarancja), plan przeglądów (interwał, następny termin), zgłoszenie serwisowe, zamówienie części ze statusami |
| `erp_sync` | **nowy**, wzorzec z KTM | import kontrahentów z enova365 (Dynamic WebAPI) raz dziennie + import CSV jako plan B |
| `notify` | **nowy** (+ `core/digest`) | push, poranny skrót dnia, eskalacja przeterminowanych zadań do szefa |
| `pwa` | kopia `apps/pwa` | ekran „Nagraj”, „Moje zadania na dziś”, „Klient” |

### Kluczowe statusy

- Zadanie: `NOWE → PRZYPISANE → W TOKU → CZEKA → ZROBIONE` (+ `ODRZUCONE`, `ODŁOŻONE`). Przejścia tylko przyciskami walidowanymi tabelą `LEGAL_TRANSITIONS` (konwencja z Pyrlandii).
- Zamówienie części: `DO ZAMÓWIENIA → ZAMÓWIONE → DOSTARCZONE → ZAMONTOWANE` (+ `ANULOWANE`).
- Przegląd: `ZAPLANOWANY → UMÓWIONY → WYKONANY` (+ protokół jako załącznik; wykonanie wylicza kolejny termin).

## 5. Główny przepływ: notatka głosowa

1. PWA: duży przycisk „Nagraj” (opcjonalnie wybór klienta z listy „w pobliżu / ostatni”). Nagranie trafia do kolejki offline.
2. Upload → `VoiceNote` (audio w R2) → zadanie Celery.
3. `Transcriber.transcribe()` → tekst PL.
4. Claude, ekstrakcja do JSON wg schematu: `customer_guess` (dopasowanie fuzzy po nazwie/mieście do bazy klientów), `contact_person`, `summary`, `tasks[] {typ, opis, termin, dla_kogo}`, `service[] {urządzenie, problem, części}`, `follow_up_date`.
5. Push do handlowca: „Zrozumiałem tak: … [OK] [Popraw]”. Edycja to formularz z wypełnionymi polami, bez pisania od zera.
6. Po OK: zadania trafiają do kolejki biura, notatka na oś czasu klienta, zgłoszenia serwisowe do serwisu.
7. Nie potwierdzone w ciągu 2 h: trafia do biura jako „do weryfikacji” (nic nie wisi w próżni).

Retencja audio: np. 30 dni, potem zostaje sam tekst (RODO, do potwierdzenia z klientem).

## 6. Harmonogram (6 tygodni, 1 developer + Claude Code)

| Tydzień | Zakres | Demo na koniec tygodnia |
|---|---|---|
| 0 | discovery u klienta, dziennik telefonów, decyzje: poczta, STT, dostęp do enova | mapa procesów, top 5 bolączek |
| 1 | scaffold z Pyrlandii (core, users, CI, Railway staging), `customers` + import CSV, model `organization` | logowanie, lista klientów z enova (CSV) |
| 2 | `tasks` + widok biura + poranny skrót + web push + eskalacja | biuro zleca handlowcowi i odwrotnie, przypomnienia działają |
| 3 | `voice` end-to-end + PWA „Nagraj” z kolejką offline; test STT na prawdziwych nagraniach | nagranie w aucie → zadania w biurze |
| 4 | `service`: urządzenia, przeglądy z przypomnieniami, zgłoszenia, zamówienia części | serwis widzi „przeglądy w tym miesiącu” i status części |
| 5 | `mailbox` (Gmail/Graph lub IMAP) + `erp_sync` enova (o ile jest dostęp do API) | maile na karcie klienta, codzienny import kontrahentów |
| 6 | pilotaż z 2 handlowcami + biuro; poprawki; pomiar vs dziennik telefonów | raport z pilotażu dla szefa |

Bufor: realnie 6–8 tygodni. Najbardziej ryzykowne elementy (STT po polsku w aucie, enova API) testujemy jak najwcześniej, nawet przed tygodniem, w którym je budujemy.

## 7. Poza MVP

Lejek sprzedażowy i prognozy, raporty dla szefa (poza prostym dashboardem), przetargi jako osobny moduł, bot WhatsApp/Telegram, dzwonienie na numer z nagrywaniem, dwustronny sync z enova (zamówienia, należności), aplikacja natywna, pełny multi-tenant/SaaS, portal klienta.

## 8. Ryzyka i decyzje do podjęcia

| Ryzyko / decyzja | Jak sprawdzamy | Kiedy |
|---|---|---|
| Handlowcy nie będą nagrywać | test „głosówka na WhatsApp do biura” w tygodniu 0 | przed startem |
| Jakość STT po polsku (auto, szum, nazwy szpitali i sprzętu) | 20 prawdziwych nagrań przez 2–3 dostawców; słownik nazw w prompcie | tydzień 0–1 |
| Dostęp do enova365 API (wersja, licencja Integratora, wdrożeniowiec) | pytanie do IT/partnera enova; plan B: eksport CSV | tydzień 0 |
| Poczta: Google czy M365 | discovery | tydzień 0 |
| RODO: nagrania, dane personelu szpitali, dostawcy AI/STT | umowy powierzenia (DPA), hosting w UE, retencja audio | przed pilotażem |
| Jedna firma czy SaaS | rozmowa z klientem o modelu współpracy (design partner) | przed tygodniem 1 |

## 9. Konwencje (z Pyrlandii)

- `CLAUDE.md` = `AGENTS.md` (lustro), każda app ma własny `AGENTS.md` z regułami domeny.
- Migracje kompatybilne wstecz; schema, dane i constrainty w osobnych migracjach.
- Testy tylko na Postgresie, warstwy z markerami; `makemigrations --check` w CI.
- Sekrety tylko w `.env`, wzór w `.env.example`.
- `CHANGELOG.md` dla zmian widocznych dla użytkownika.
