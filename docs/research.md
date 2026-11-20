# TinyCRM — research (listopad 2026)

## 1. Problem (z rozmowy)

- Firma: dystrybucja sprzętu medycznego, handlowcy jeżdżą po całym kraju.
- Handlowcy **nie będą nic wpisywać** do enova365 ani do żadnego innego systemu.
- Dziś: handlowiec dzwoni do osoby w biurze → ona zapisuje i „ogarnia” tematy (oferty, zamówienia, maile, przypomnienia).
- Skutek: wszystko trzyma jedna osoba w głowie lub w notatkach, rzeczy giną („coś się zapomina”), nie ma historii klienta, a szef nic nie widzi.

**Wniosek kluczowy:** produkt nie może opierać się na formularzach. Musi przechwytywać informacje tam, gdzie one już powstają: **głos/telefon, mail, WhatsApp/SMS**, a porządkowanie musi robić system (AI) albo biuro, nie handlowiec.

## 2. Co oferuje rynek

| Produkt | Dla kogo | Cena (orientacyjnie) | Uwagi pod nasz przypadek |
|---|---|---|---|
| Pipedrive | małe zespoły sprzedażowe | od ~14 €/użytk./mies. | dobry lejek i sync maili, ale handlowiec dalej musi klikać |
| Folk, Attio, Capsule | małe zespoły, relacje mailowe | ~0–35 $/użytk./mies. | nowoczesne i proste, nastawione na biuro/laptopa, nie na teren |
| Livespace (PL) | B2B, polski | od ~79 zł/użytk./mies. | polski język i faktura, mimo to „klasyczny CRM do wpisywania” |
| Firmao (PL) | małe firmy, CRM + faktury | ~69–93 zł/użytk./mies. | dużo funkcji, ciężki dla handlowca w aucie |
| Bitrix24 | wszystko w jednym | Free / ryczałt od ~49 $/firmę | przytłaczający interfejs |
| Twenty (open source) | self-host, devowie | darmowy (self-host) | ma gotowy sync Gmail/M365 (Graph), może posłużyć jako baza albo inspiracja |
| Otto, Spotio, Nutshell, Voiceline, Veeva Vault CRM | **voice-to-CRM dla terenu** | różnie, głównie USA | dokładnie ten trend: rep nagrywa notatkę, a AI tworzy rekord i zadania. Słabo z językiem polskim i z integracją z enova |

**Luka rynkowa:** nie ma prostego, polskojęzycznego narzędzia, które działa jak „wirtualna asystentka”: handlowiec mówi, system zapisuje, przypisuje zadanie biuru i pilnuje, żeby nic nie przepadło. Do tego synchronizuje kontrahentów z enova.

## 3. Proponowana koncepcja TinyCRM

### Zasada: „handlowiec tylko mówi, biuro tylko odhacza”

1. **Przechwytywanie (handlowiec)**
   - Notatka głosowa po wizycie: aplikacja PWA z jednym dużym przyciskiem albo wiadomość głosowa na WhatsApp/Telegram do bota.
   - Opcjonalnie: dedykowany numer telefonu, który nagrywa notatkę i ją transkrybuje.
   - AI (transkrypcja PL + ekstrakcja) wyciąga z notatki: klienta (dopasowanie do bazy z enova), osobę kontaktową, produkty, ustalenia oraz **zadania z terminem i właścicielem** („wyślij ofertę na USG do szpitala w Radomiu do piątku”).
   - Handlowiec dostaje krótkie podsumowanie do potwierdzenia jednym kliknięciem: „OK” / „popraw”.

2. **Zadania (biuro ↔ handlowiec)**
   - Skrzynka zadań dla biura: kto zlecił, dla jakiego klienta, termin, status.
   - Zlecanie w obie strony: biuro też może zlecić coś handlowcowi („zadzwoń do X”).
   - Automatyczne przypomnienia i eskalacja przeterminowanych zadań (push/SMS/mail); dzięki temu przestaje się „zapominać”.
   - Poranny skrót dnia dla handlowca: zadania + wizyty + co obiecał klientom.

3. **Maile**
   - Podpięcie skrzynek (Gmail API / Microsoft Graph; najpierw trzeba sprawdzić, czego firma używa).
   - Automatyczne przypinanie wątków do klienta po domenie lub adresie.
   - AI: „z tego maila wynika zadanie”, czyli propozycja zadania jednym klikiem.
   - Wysyłka oferty/maila z poziomu zadania (szkic generowany z notatki handlowca).

4. **Klient (karta)**
   - Oś czasu: notatki głosowe, maile, zadania, zamówienia z enova.
   - „Co obiecaliśmy i czego nie zrobiliśmy”.

5. **Serwis (części, przeglądy)**
   - Ten sam mechanizm głosowy: serwisant albo handlowiec mówi „w szpitalu X padł zasilacz w aparacie Y, trzeba zamówić część”, a system tworzy **zgłoszenie serwisowe** z zadaniem „zamów część” dla biura.
   - Typy zadań: zamówienie części (status: do zamówienia → zamówione → dostarczone → zamontowane), naprawa, reklamacja/gwarancja.
   - **Baza urządzeń u klientów** (model, numer seryjny, data instalacji, gwarancja) i **harmonogram przeglądów okresowych**. Przegląd to obowiązek prawny dla wyrobów medycznych, a system sam przypomina np. 30 dni przed terminem i tworzy zadanie „umówić przegląd”.
   - Przy przeglądzie: protokół/zdjęcie protokołu dołączone do urządzenia, następny termin wyliczony automatycznie.
   - Bonus sprzedażowy: kończąca się gwarancja albo stary sprzęt to sygnał dla handlowca („czas na ofertę wymiany”).

6. **Integracja z enova365**
   - enova ma API (Dynamic WebAPI / dodatek Integrator, REST). Start: **jednostronny import kontrahentów** (i ewentualnie historii dokumentów) do TinyCRM raz dziennie.
   - Później: podgląd zamówień i należności na karcie klienta. Nie zastępujemy enova, tylko nad nią nadbudowujemy.

### Specyfika branży medycznej (do weryfikacji)
- RODO: dane kontaktowe personelu szpitali, nagrania głosowe (gdzie są przetwarzane, retencja nagrań, hosting w UE).
- Przetargi publiczne: warto mieć terminy przetargów jako typ zadania i przypomnienia.
- Serwis i przeglądy sprzętu: potencjalny moduł „przypomnij o przeglądzie urządzenia u klienta”.

## 4. MVP (propozycja, 4–6 tygodni)

1. Baza klientów (import CSV / z enova).
2. Notatka głosowa (PWA) → transkrypcja → AI ekstrakcja → potwierdzenie.
3. Zadania z przypisaniem, terminem, przypomnieniami i widokiem dla biura.
4. Podpięcie jednej skrzynki mailowej i przypinanie maili do klientów.
5. Poranny skrót dnia (push/mail).
6. Serwis w wersji minimum: zgłoszenie serwisowe / zamówienie części jako typ zadania ze statusami + lista urządzeń z datą następnego przeglądu i przypomnieniem.

Poza MVP: lejek sprzedażowy, raporty dla szefa, aplikacja natywna, dwustronny sync z enova, telefonia.

## 5. Wstępny stack (do decyzji)

- Frontend: PWA (Next.js / SvelteKit), mobile-first, nagrywanie audio w przeglądarce.
- Backend: Postgres + prosty API (Node/TS albo Python).
- AI: transkrypcja PL (np. Whisper-class / ElevenLabs STT) + LLM (Claude) do ekstrakcji zadań i dopasowania klienta.
- Mail: Gmail API lub Microsoft Graph (Mail.Read/Mail.Send).
- Hosting: UE (RODO), np. Cloudflare / Hetzner.

## 6. Otwarte pytania do klienta

1. Ilu jest handlowców, a ile osób w biurze?
2. Jaka poczta: Google Workspace czy Microsoft 365?
3. Jaka wersja enova365 i czy jest dodatek API/Integrator?
4. Czego używają handlowcy: WhatsApp, SMS, zwykłe rozmowy telefoniczne? Android czy iPhone?
5. Jakie tematy najczęściej „giną”: oferty, zamówienia, reklamacje, serwis, przetargi?
6. Czy szef chce raportów (wizyty, lejek), czy chodzi tylko o to, żeby nic nie przepadało?
7. Serwis: czy mają własnych serwisantów, czy robią to handlowcy? Gdzie dziś jest lista urządzeń u klientów i terminy przeglądów (Excel, enova, nigdzie)? Skąd zamawiają części (producent, hurtownia) i czy to idzie przez enova?
8. Budżet i model: budujemy własny produkt (SaaS dla wielu firm) czy rozwiązanie dla tej jednej firmy?

## Źródła

- [Attio – Pipedrive alternatives](https://attio.com/f/pipedrive-crm-alternatives), [Attio – best CRM for small business](https://attio.com/f/best-crm-for-small-business)
- [Capsule – best CRM for small teams](https://capsulecrm.com/blog/best-crm-for-small-teams/), [Capsule – Folk alternatives](https://capsulecrm.com/blog/folk-crm-alternatives/)
- [dev.to – Best CRM for small business 2026](https://dev.to/konabayev/best-crm-for-small-business-2026-7-tools-compared-free-to-99mo-5g4c)
- [Breakcold – CRMs for teams under 5](https://www.breakcold.com/blog/best-crms-for-team-under-5-people)
- [Deviniti – ranking CRM dla handlowców](https://deviniti.com/pl/blog/crm/ranking-2025-poznaj-top-6-systemow-crm-dla-handlowcow/), [Poradnik Przedsiębiorcy – ranking CRM 2026](https://poradnikprzedsiebiorcy.pl/-ranking-crm-7-najlepszych-systemow-crm)
- [Symfonia – recenzja Livespace](https://symfonia.pl/blog/branze/handel/livespace-recenzja-czy-warto-korzystac-z-tego-systemu/), [Capterra – Firmao](https://www.capterra.com/p/215327/Firmao-CRM/), [Bitrix24](https://www.bitrix24.com/crm/free_crm_small_business_new.php)
- [Twenty – email & calendar](https://docs.twenty.com/getting-started/core-concepts/calendar-and-email), [Twenty – Microsoft 365](https://www.mintlify.com/twentyhq/twenty/integrations/email/microsoft-365)
- [Spotio – voice to CRM](https://spotio.com/blog/voice-to-crm/), [Nutshell – voice-to-text logging](https://www.nutshell.com/blog/voice-to-text-activity-logging), [Otto – voice AI CRM](https://cxotoday.com/media-coverage/otto-launches-worlds-first-voice-ai-crm-letting-field-sales-reps-update-deals-with-a-phone-call/), [Veeva – MedTech AI field](https://www.veeva.com/medtech/wp-content/uploads/2026/03/MedTech_Unlocking-Field-Potential-with-AI-Enablement_Infographic.pdf), [Bliro – voice to CRM 2026](https://www.bliro.io/en/blog/voice-to-crm-2026-how-ai-automates-crm-maintenance)
- [enova365 Dynamic WebAPI docs](https://altone-public.github.io/enova-products-docs/AltOne.Integracje/docs/DokumentacjaDynamicWebAPI.html), [Alumio – enova365](https://alumio.com/platforms/enova-365)

*Ceny pochodzą z porównywarek i blogów dostawców i różnią się między źródłami. Przed decyzją trzeba je zweryfikować u dostawców.*
