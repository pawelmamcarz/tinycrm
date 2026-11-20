"""Dane demonstracyjne prototypu: organizacja „Demo Med”, konta per rola, fikcyjni klienci, urządzenia,
zadania, zgłoszenia serwisowe i jedna przetworzona notatka głosowa.

Idempotentne: ponowne uruchomienie niczego nie dubluje (klucze: slug organizacji, login, nazwa klienta,
numer seryjny, `source_ref="demo:..."`, `client_uuid` notatki). `--reset` usuwa wszystkie dane organizacji
demo (i jej użytkowników) przed ponownym utworzeniem.

Wszystkie nazwy, NIP-y i osoby są FIKCYJNE. NIP-y zaczynają się od „000” (nieistniejący kod urzędu
skarbowego), więc nie mogą należeć do prawdziwej firmy, a mimo to mają poprawną sumę kontrolną.
"""
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.models import Organization
from apps.users.models import Role, User

DEMO_SLUG = "demo"
DEMO_ORG_NAME = "Demo Med"
DEFAULT_PASSWORD = "Demo-TinyCRM-2026"

USERS = [
    # login, imię, nazwisko, rola
    ("jan", "Jan", "Handlowski", Role.HANDLOWIEC),
    ("ewa", "Ewa", "Terenowa", Role.HANDLOWIEC),
    ("ola", "Ola", "Biurowa", Role.BIURO),
    ("marek", "Marek", "Serwisowy", Role.SERWIS),
    ("szef", "Szymon", "Kierowniczy", Role.SZEF),
    ("admin", "Adam", "Administracyjny", Role.ADMIN),
]

# nazwa, skrót, miasto, opiekun (login)
CUSTOMERS = [
    ("Szpital Specjalistyczny w Radomiu", "SS Radom", "Radom", "jan"),
    ("Wojewódzki Szpital Zespolony w Kielcach", "WSZ Kielce", "Kielce", "jan"),
    ("Szpital Miejski w Łodzi", "SM Łódź", "Łódź", "ewa"),
    ("Klinika Zdrowie Lublin", "Klinika Zdrowie", "Lublin", "ewa"),
    ("Centrum Medyczne Słoneczna w Zamościu", "CM Słoneczna", "Zamość", "jan"),
    ("Szpital Powiatowy w Puławach", "SP Puławy", "Puławy", "ewa"),
    ("Przychodnia Pod Lipami w Siedlcach", "Pod Lipami", "Siedlce", "jan"),
    ("Szpital Kliniczny Nr 3 w Krakowie", "SK3 Kraków", "Kraków", "ewa"),
    ("Centrum Diagnostyki Obrazowej Vita w Warszawie", "CDO Vita", "Warszawa", "jan"),
    ("Szpital Dziecięcy w Gdańsku", "SD Gdańsk", "Gdańsk", "ewa"),
    ("Klinika Kardiologiczna Serce w Poznaniu", "Klinika Serce", "Poznań", "jan"),
    ("Szpital Wojewódzki w Rzeszowie", "SW Rzeszów", "Rzeszów", "ewa"),
    ("Gabinet Ginekologiczny Novum w Opolu", "Novum Opole", "Opole", "jan"),
    ("Szpital Specjalistyczny w Białymstoku", "SS Białystok", "Białystok", "ewa"),
    ("Centrum Rehabilitacji Ruch w Toruniu", "CR Ruch", "Toruń", "jan"),
    ("Szpital Miejski w Częstochowie", "SM Częstochowa", "Częstochowa", "ewa"),
    ("Klinika Okulistyczna Jasny Wzrok w Olsztynie", "Jasny Wzrok", "Olsztyn", "jan"),
    ("Szpital Powiatowy w Sandomierzu", "SP Sandomierz", "Sandomierz", "ewa"),
    ("Centrum Medyczne Zdrój w Szczecinie", "CM Zdrój", "Szczecin", "jan"),
    ("Szpital Specjalistyczny we Wrocławiu", "SS Wrocław", "Wrocław", "ewa"),
    ("Przychodnia Rodzinna Pod Dębem w Ostrowcu", "Pod Dębem", "Ostrowiec Świętokrzyski", "jan"),
    ("Szpital Onkologiczny w Bydgoszczy", "SO Bydgoszcz", "Bydgoszcz", "ewa"),
    ("Klinika Ortopedyczna Krok w Katowicach", "Klinika Krok", "Katowice", "jan"),
    ("Szpital Powiatowy w Skarżysku", "SP Skarżysko", "Skarżysko-Kamienna", "ewa"),
    ("Centrum Medyczne Promyk w Pionkach", "CM Promyk", "Pionki", "jan"),
]

CONTACTS = [
    # indeks klienta, imię i nazwisko, stanowisko
    (0, "dr Piotr Nowak", "Ordynator ginekologii"),
    (0, "Anna Zaopatrzeniowa", "Dział zaopatrzenia"),
    (1, "dr Karolina Wiśniewska", "Kierownik pracowni USG"),
    (2, "Tomasz Techniczny", "Dział aparatury medycznej"),
    (3, "dr Marta Lubelska", "Dyrektor medyczny"),
    (4, "Beata Zamojska", "Pielęgniarka oddziałowa"),
    (7, "prof. Jerzy Krakowski", "Kierownik kliniki kardiologii"),
    (8, "Michał Obrazowy", "Kierownik CDO"),
    (10, "dr Ewelina Sercowa", "Ordynator kardiologii"),
    (11, "Robert Rzeszowski", "Inżynier aparatury"),
]

# klient, model, producent, typ przeglądu: (dni od dziś do następnego przeglądu), gwarancja (dni od dziś)
DEVICE_MODELS = [
    ("USG Vivid S70", "MedSonic"),
    ("Kardiomonitor CM-12", "CardioTech"),
    ("Defibrylator AED Pro", "LifeStart"),
    ("Pompa infuzyjna PI-200", "InfuMed"),
    ("Kardiomonitor CM-8", "CardioTech"),
    ("USG przenośny LOGIQ e", "MedSonic"),
    ("Defibrylator DX-5", "LifeStart"),
    ("Pompa strzykawkowa PS-50", "InfuMed"),
    ("Aparat EKG 12-kanałowy", "CardioTech"),
    ("Pulsoksymetr stacjonarny PO-3", "OxyCare"),
]
# Przesunięcia terminu następnego przeglądu (dni od dziś): część po terminie, część w ciągu 30 dni.
INSPECTION_OFFSETS = [-40, -12, -3, 2, 7, 12, 18, 25, 29, 45, 60, 90, 120, 150, 200]
WARRANTY_OFFSETS = [-200, 20, 300, 700, -30, 15, 500, None, 25, 900]

VOICE_SAMPLE = (
    "Byłem w szpitalu w Radomiu u doktora Nowaka. Chcą ofertę na dwa kardiomonitory do piątku. "
    "W USG na ginekologii padła głowica, trzeba zamówić nową głowicę konweksową. "
    "Zadzwonić do nich w poniedziałek."
)
VOICE_UUID = "demo-voice-radom"


def fictional_nip(seq):
    """Fikcyjny NIP z poprawną sumą kontrolną; prefiks 000 nie jest kodem żadnego urzędu skarbowego."""
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    n = seq * 7 + 11
    while True:
        digits = [int(c) for c in f"000{n:06d}"]
        check = sum(w * d for w, d in zip(weights, digits)) % 11
        if check != 10:
            return "".join(map(str, digits)) + str(check)
        n += 1


class Command(BaseCommand):
    help = "Tworzy (idempotentnie) dane demo: organizacja „Demo Med”, konta, klienci, urządzenia, zadania."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Usuń wszystkie dane organizacji demo i utwórz od nowa")
        parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Hasło kont demo")

    def handle(self, *args, **options):
        password = options["password"]
        if password == DEFAULT_PASSWORD and not settings.DEBUG:
            # Domyślne hasło jest w publicznym repo — poza trybem deweloperskim wymagamy własnego.
            raise CommandError("Poza DEBUG podaj własne hasło kont demo: --password <hasło>.")
        if options["reset"]:
            self.reset()
        with transaction.atomic():
            org, _ = Organization.objects.get_or_create(slug=DEMO_SLUG, defaults={"name": DEMO_ORG_NAME})
            users = self.seed_users(org, password)
            customers = self.seed_customers(org, users)
            devices = self.seed_devices(org, customers)
            self.seed_inspections(org, devices)
            self.seed_tasks(org, users, customers)
            self.seed_tickets(org, users, customers, devices)
        # Notatka głosowa poza główną transakcją — `process_voice_note` zarządza własnymi transakcjami.
        note = self.seed_voice_note(org, users, customers)

        self.stdout.write(self.style.SUCCESS("Dane demo gotowe (organizacja „Demo Med”, slug: demo)."))
        self.stdout.write(f"  klienci: {len(customers)}, urządzenia: {len(devices)}, notatka do potwierdzenia: #{note.pk}")
        self.stdout.write("Konta demo (login → rola):")
        for username, _first, _last, role in USERS:
            self.stdout.write(f"  {username:<6} → {role.label}")
        self.stdout.write(self.style.WARNING(f"Hasło wszystkich kont demo: {password}"))

    # --- reset ---------------------------------------------------------------------------------

    def reset(self):
        org = Organization.objects.filter(slug=DEMO_SLUG).first()
        if org is None:
            return
        # Kolejność usuwania respektuje FK PROTECT (urządzenie → klient, notatka → autor, użytkownik → organizacja).
        order = [
            "voice.VoiceNote", "service.PartOrder", "service.ServiceTicket", "service.Inspection", "service.Device",
            "tasks.TaskComment", "tasks.Task", "customers.Contact", "customers.Customer",
            "erp_sync.SyncRun", "erp_sync.SyncState",
        ]
        with transaction.atomic():
            for label in order:
                try:
                    model = apps.get_model(label)
                except LookupError:
                    continue
                model.objects.filter(organization=org).delete()
            User.objects.filter(organization=org).delete()
            org.delete()
        self.stdout.write("Usunięto dane organizacji demo.")

    # --- seedy ---------------------------------------------------------------------------------

    def seed_users(self, org, password):
        users = {}
        for username, first, last, role in USERS:
            user = User.objects.filter(username=username).first()
            if user is not None and user.organization_id not in (None, org.pk):
                raise SystemExit(f"Login „{username}” należy do innej organizacji — przerwano.")
            if user is None:
                user = User(username=username)
            user.first_name, user.last_name, user.role, user.organization = first, last, role, org
            user.email = f"{username}@demo.example.com"
            user.is_active = True
            user.is_staff = role == Role.ADMIN
            user.is_superuser = role == Role.ADMIN
            user.set_password(password)
            user.save()
            users[username] = user
        return users

    def seed_customers(self, org, users):
        from apps.customers.models import Contact, Customer
        from apps.customers.services import create_customer

        customers = []
        for i, (name, short, city, owner) in enumerate(CUSTOMERS):
            customer = Customer.objects.for_org(org).filter(name=name).first()
            if customer is None:
                slug = short.lower().replace(" ", "-")
                customer = create_customer(
                    org, name=name, short_name=short, city=city, nip=fictional_nip(i), owner=users[owner],
                    phone=f"+48 500 000 {i:03d}", email=f"sekretariat@{slug}.example.com",
                    email_domain=f"{slug}.example.com", enova_id=f"DEMO-{i + 1:03d}",
                    address=f"ul. Przykładowa {i + 1}",
                )
            customers.append(customer)
        for idx, contact_name, position in CONTACTS:
            customer = customers[idx]
            Contact.objects.get_or_create(
                organization=org, customer=customer, name=contact_name,
                defaults={"position": position, "phone": f"+48 600 000 {idx:03d}"},
            )
        return customers

    def seed_devices(self, org, customers):
        from apps.service.models import Device

        today = timezone.localdate()
        devices = []
        for i in range(30):
            customer = customers[i % 12]  # urządzenia skupione u pierwszych 12 klientów
            model, manufacturer = DEVICE_MODELS[i % len(DEVICE_MODELS)]
            serial = f"DEMO-SN-{i + 1:04d}"
            device = Device.objects.for_org(org).filter(serial_number=serial).first()
            if device is None:
                next_due = today + timedelta(days=INSPECTION_OFFSETS[i % len(INSPECTION_OFFSETS)])
                warranty = WARRANTY_OFFSETS[i % len(WARRANTY_OFFSETS)]
                device = Device.objects.create(
                    organization=org, customer=customer, name=model, manufacturer=manufacturer,
                    serial_number=serial, location=["Ginekologia", "Kardiologia", "SOR", "OIOM", "Pracownia USG"][i % 5],
                    installed_at=today - timedelta(days=400 + i * 20),
                    warranty_until=(today + timedelta(days=warranty)) if warranty is not None else None,
                    inspection_interval_months=12,
                    last_inspection_at=next_due - timedelta(days=365),
                    next_inspection_due=next_due,
                )
            devices.append(device)
        return devices

    def seed_inspections(self, org, devices):
        from apps.service.services import ensure_upcoming_inspections
        from apps.service.tasks import plan_inspection_reminders

        ensure_upcoming_inspections(org)
        plan_inspection_reminders(org)

    def seed_tasks(self, org, users, customers):
        from apps.tasks.models import Task, TaskKind, TaskStatus
        from apps.tasks.services import change_status, create_task

        today = timezone.localdate()
        jan, ewa, ola, marek, szef = (users[u] for u in ("jan", "ewa", "ola", "marek", "szef"))
        # klucz, tytuł, typ, klient, zlecający, wykonawca, termin (dni), priorytet, ścieżka statusów
        specs = [
            ("t1", "Przygotować ofertę na kardiomonitory", TaskKind.OFERTA, 1, jan, ola, -3, 3, []),
            ("t2", "Wysłać cennik pomp infuzyjnych", TaskKind.OFERTA, 2, ewa, ola, -1, 2, [TaskStatus.W_TOKU]),
            ("t3", "Potwierdzić termin dostawy USG", TaskKind.ZAMOWIENIE, 8, jan, ola, 0, 2, []),
            ("t4", "Zadzwonić do kierownika pracowni USG", TaskKind.KONTAKT, 1, jan, jan, 0, 2, []),
            ("t5", "Umówić prezentację defibrylatorów", TaskKind.KONTAKT, 3, ewa, ewa, 3, 2, []),
            ("t6", "Sprawdzić warunki przetargu na aparaty EKG", TaskKind.PRZETARG, 11, szef, ola, 7, 3, []),
            ("t7", "Zamówić akcesoria do kardiomonitorów", TaskKind.ZAMOWIENIE, 10, ewa, ola, 5, 2,
             [TaskStatus.W_TOKU, TaskStatus.CZEKA]),
            ("t8", "Wystawić ofertę serwisową na rok", TaskKind.OFERTA, 7, ewa, ola, -10, 2,
             [TaskStatus.W_TOKU, TaskStatus.ZROBIONE]),
            ("t9", "Odesłać protokół przeglądu klientowi", TaskKind.SERWIS, 0, marek, ola, -2, 2,
             [TaskStatus.ZROBIONE]),
            ("t10", "Zebrać zapotrzebowanie na pompy strzykawkowe", TaskKind.KONTAKT, 5, jan, None, 10, 1, []),
            ("t11", "Przekazać dokumentację techniczną USG", TaskKind.INNE, 4, ewa, None, None, 2, []),
            ("t12", "Wycena naprawy defibrylatora", TaskKind.SERWIS, 2, marek, marek, -5, 3, [TaskStatus.W_TOKU]),
        ]
        for key, title, kind, cust_idx, creator, assignee, due, priority, path in specs:
            ref = f"demo:{key}"
            if Task.objects.for_org(org).filter(source_ref=ref).exists():
                continue
            task = create_task(
                org, title=title, created_by=creator, assignee=assignee, customer=customers[cust_idx],
                kind=kind, due_date=(today + timedelta(days=due)) if due is not None else None, priority=priority,
                description="Zadanie demonstracyjne.", source_ref=ref,
            )
            for status in path:
                change_status(task, status, user=assignee or ola)

    def seed_tickets(self, org, users, customers, devices):
        from apps.service.models import PartOrderStatus, ServiceTicket, TicketStatus
        from apps.service.services import change_part_order_status, change_ticket_status, create_part_order, create_ticket

        marek, jan, ola = users["marek"], users["jan"], users["ola"]
        specs = [
            # klucz, klient, tytuł, opis, części [(nazwa, ilość, ścieżka statusów)]
            ("tk1", 1, "Awaria: USG — brak obrazu z głowicy", "Głowica liniowa nie wykrywana przez aparat.",
             [("Głowica liniowa L12", 1, [PartOrderStatus.ZAMOWIONE])]),
            ("tk2", 2, "Kardiomonitor restartuje się", "Restart co kilka godzin, podejrzenie zasilacza.",
             [("Zasilacz kardiomonitora", 1, [PartOrderStatus.ZAMOWIONE, PartOrderStatus.DOSTARCZONE]),
              ("Kabel EKG 5-odprowadzeniowy", 2, [])]),
        ]
        for key, cust_idx, title, description, parts in specs:
            ref = f"demo:{key}"
            if ServiceTicket.objects.for_org(org).filter(source_ref=ref).exists():
                continue
            customer = customers[cust_idx]
            device = next((d for d in devices if d.customer_id == customer.pk), None)
            ticket = create_ticket(org, customer=customer, title=title, description=description, device=device,
                                   reported_by=jan, source_ref=ref, assignee=marek)
            change_ticket_status(ticket, TicketStatus.W_TOKU, user=marek)
            for part_name, qty, path in parts:
                order = create_part_order(org, customer=customer, part_name=part_name, quantity=qty, ticket=ticket,
                                          supplier="Dostawca Demo")
                for status in path:
                    change_part_order_status(order, status, user=ola)

    def seed_voice_note(self, org, users, customers):
        from apps.voice.models import VoiceNote
        from apps.voice.services import process_voice_note

        note = VoiceNote.objects.for_org(org).filter(client_uuid=VOICE_UUID).first()
        if note is None:
            note = VoiceNote.objects.create(
                organization=org, author=users["jan"], typed_text=VOICE_SAMPLE, client_uuid=VOICE_UUID,
                recorded_at=timezone.now(),
            )
            note = process_voice_note(note)
        return note
