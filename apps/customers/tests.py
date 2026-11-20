"""Testy modułu klientów: dopasowanie nazw, wyszukiwarka, oś czasu, uprawnienia i izolacja organizacji."""
import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.customers import services
from apps.customers.forms import CustomerForm, nip_is_valid
from apps.customers.matching import match_customer, normalize
from apps.customers.models import Contact, Customer
from apps.customers.registrations import NAV, TIMELINE

pytestmark = pytest.mark.django_db

VALID_NIP = "1234563218"  # fikcyjny NIP z poprawną sumą kontrolną


def mk(org, name, city="", **kw):
    return Customer.objects.create(organization=org, name=name, city=city, **kw)


# ---------------------------------------------------------------- matching

@pytest.fixture
def hospitals(org):
    return {
        "radom": mk(org, "Szpital Specjalistyczny w Radomiu", "Radom"),
        "zdrowie": mk(org, "Klinika Zdrowie Lublin", "Lublin"),
        "kielce": mk(org, "Wojewódzki Szpital Zespolony", "Kielce", short_name="WSZ Kielce"),
        "lodz": mk(org, "Przychodnia Pod Lipami", "Łódź"),
        "zamosc": mk(org, "Szpital im. Jana Pawła II", "Zamość"),
    }


def test_normalize_polish_chars():
    assert normalize("Łódź, Śródmieście!") == "lodz  srodmiescie "


def test_match_inflected_city(org, hospitals):
    customer, score = match_customer(org, "byłem w szpitalu w Radomiu u ordynatora")
    assert customer == hospitals["radom"]
    assert score >= 0.5


def test_match_inflected_type_and_name(org, hospitals):
    customer, score = match_customer(org, "Dzisiaj byłem w klinice Zdrowie, chcą ofertę na USG")
    assert customer == hospitals["zdrowie"]
    assert score >= 0.7


def test_match_name_inflection_zdrowiu(org, hospitals):
    customer, _ = match_customer(org, "spotkanie w Zdrowiu w Lublinie")
    assert customer == hospitals["zdrowie"]


def test_match_lodz_locative(org, hospitals):
    customer, _ = match_customer(org, "w przychodni w Łodzi zepsuł się aparat")
    assert customer == hospitals["lodz"]


def test_match_short_name(org, hospitals):
    customer, score = match_customer(org, "zadzwonić do WSZ Kielce w sprawie przetargu")
    assert customer == hospitals["kielce"]
    assert score >= 0.9


def test_match_city_and_type_when_name_not_spoken(org, hospitals):
    customer, _ = match_customer(org, "byłem w szpitalu w Zamościu")
    assert customer == hospitals["zamosc"]


def test_match_prefers_type_over_same_distinctive_word(org, hospitals):
    mk(org, "Przychodnia Zdrowie", "Radom")
    customer, _ = match_customer(org, "w klinice Zdrowie")
    assert customer == hospitals["zdrowie"]


def test_no_match_for_unrelated_text(org, hospitals):
    customer, score = match_customer(org, "spotkanie w Krakowie z dyrektorem")
    assert customer is None
    assert score < 0.5


def test_no_match_for_empty_text(org, hospitals):
    assert match_customer(org, "") == (None, 0.0)
    assert match_customer(org, "w u i") == (None, 0.0)


def test_ambiguous_city_is_penalized(org):
    mk(org, "Szpital Miejski", "Radom")
    mk(org, "Szpital Wojewódzki", "Radom")
    customer, score = match_customer(org, "w szpitalu w Radomiu", min_score=0.7)
    assert customer is None
    assert score < 0.8


def test_match_ignores_inactive(org):
    mk(org, "Szpital Specjalistyczny w Radomiu", "Radom", is_active=False)
    assert match_customer(org, "szpital w Radomiu")[0] is None


def test_match_does_not_cross_organizations(org, other_org):
    mk(other_org, "Szpital Specjalistyczny w Radomiu", "Radom")
    customer, _ = match_customer(org, "byłem w szpitalu w Radomiu")
    assert customer is None
    assert match_customer(None, "szpital w Radomiu")[0] is None


def test_match_signature_min_score(org, hospitals):
    customer, score = match_customer(org, "w klinice Zdrowie", min_score=0.99)
    assert customer is None and score > 0


# ---------------------------------------------------------------- services

def test_search_queryset_owner_first_and_filters(org, salesperson):
    a = mk(org, "Alfa Klinika", "Radom")
    b = mk(org, "Beta Szpital", "Lublin", owner=salesperson)
    mk(org, "Gamma", "Radom", is_active=False)
    assert list(services.search_queryset(org, "", user=salesperson)) == [b, a]
    assert list(services.search_queryset(org, "radom")) == [a]
    assert list(services.search_queryset(org, "beta", user=salesperson)) == [b]


def test_search_queryset_by_nip_with_dashes(org):
    c = mk(org, "Delta", nip=VALID_NIP)
    assert list(services.search_queryset(org, "123-456-32")) == [c]


def test_search_queryset_other_org_empty(org, other_org):
    mk(other_org, "Obcy Szpital", "Radom")
    assert not services.search_queryset(org, "Obcy").exists()


def test_open_tasks_count(org):
    from apps.tasks.models import Task, TaskStatus

    c = mk(org, "Szpital")
    Task.objects.create(organization=org, title="A", customer=c, status=TaskStatus.NOWE)
    Task.objects.create(organization=org, title="B", customer=c, status=TaskStatus.W_TOKU)
    Task.objects.create(organization=org, title="C", customer=c, status=TaskStatus.ZROBIONE)
    assert services.open_tasks_count(c) == 2


def test_build_timeline_sorted_and_tolerant(org, monkeypatch):
    c = mk(org, "Szpital")
    now = timezone.now()

    def ok(customer):
        return [
            {"date": now - datetime.timedelta(days=2), "icon": "📝", "title": "starsze", "text": "", "url": "/x/"},
            {"date": timezone.localdate(), "icon": "📅", "title": "dzisiejsza data", "text": "", "url": ""},
            {"date": None, "title": "bez daty"},
        ]

    def broken(customer):
        raise RuntimeError("boom")

    monkeypatch.setattr(services, "collect_timeline_sources", lambda: [ok, broken, lambda cust: None])
    entries = services.build_timeline(c)
    assert [e["title"] for e in entries] == ["dzisiejsza data", "starsze"]
    assert all(timezone.is_aware(e["date"]) for e in entries)


def test_collect_timeline_sources_includes_own_and_tolerates_broken(monkeypatch):
    real_import = services.import_module

    def fake_import(name):
        if name == "apps.tasks.registrations":
            raise RuntimeError("moduł chwilowo uszkodzony")
        if name == "apps.service.registrations":
            raise ModuleNotFoundError(name=name)
        return real_import(name)

    monkeypatch.setattr(services, "import_module", fake_import)
    sources = services.collect_timeline_sources()
    assert services.customer_timeline in sources


def test_customer_timeline_includes_contacts(org):
    c = mk(org, "Szpital")
    Contact.objects.create(organization=org, customer=c, name="Anna Nowak", position="ordynator")
    titles = [e["title"] for e in services.build_timeline(c)]
    assert "Dodano klienta" in titles and "Nowa osoba kontaktowa" in titles


def test_registrations():
    assert NAV[0]["url_name"] == "customers:list"
    assert NAV[0]["order"] == 30 and NAV[0]["roles"] == []
    assert TIMELINE


# ---------------------------------------------------------------- forms

def test_nip_checksum():
    assert nip_is_valid(VALID_NIP)
    assert not nip_is_valid("1234563217")
    assert not nip_is_valid("123")


def test_customer_form_owner_limited_to_org(org, salesperson, other_org_user):
    form = CustomerForm(organization=org)
    owners = list(form.fields["owner"].queryset)
    assert salesperson in owners and other_org_user not in owners
    form = CustomerForm({"name": "X", "owner": other_org_user.pk}, organization=org)
    assert not form.is_valid() and "owner" in form.errors


def test_customer_form_nip_normalized(org):
    form = CustomerForm({"name": "X", "nip": "123-456-32-18", "is_active": True}, organization=org)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["nip"] == VALID_NIP
    form = CustomerForm({"name": "X", "nip": "1234563217"}, organization=org)
    assert not form.is_valid()


def test_customer_form_enova_unique_per_org(org, other_org):
    mk(org, "A", enova_id="E1")
    mk(other_org, "B", enova_id="E2")
    assert not CustomerForm({"name": "X", "enova_id": "E1"}, organization=org).is_valid()
    assert CustomerForm({"name": "X", "enova_id": "E2"}, organization=org).is_valid()


# ---------------------------------------------------------------- views: list/detail

def test_list_requires_login(client):
    r = client.get(reverse("customers:list"))
    assert r.status_code == 302


def test_list_renders_search_and_mine(client, org, salesperson):
    mk(org, "Szpital Alfa", "Radom", owner=salesperson)
    mk(org, "Klinika Beta", "Lublin")
    client.force_login(salesperson)
    r = client.get(reverse("customers:list"))
    assert r.status_code == 200
    assert "Szpital Alfa" in r.content.decode() and "Klinika Beta" in r.content.decode()
    r = client.get(reverse("customers:list"), {"q": "lublin"})
    assert "Klinika Beta" in r.content.decode() and "Szpital Alfa" not in r.content.decode()
    r = client.get(reverse("customers:list"), {"moi": "1"})
    assert "Szpital Alfa" in r.content.decode() and "Klinika Beta" not in r.content.decode()
    r = client.get(reverse("customers:list"), {"q": "radom"}, HTTP_HX_REQUEST="true")
    assert "<html" not in r.content.decode() and "Szpital Alfa" in r.content.decode()


def test_list_paginates_25(client, org, salesperson):
    for i in range(30):
        mk(org, f"Klient {i:02d}")
    client.force_login(salesperson)
    r = client.get(reverse("customers:list"))
    assert len(r.context["page"].object_list) == 25
    r = client.get(reverse("customers:list"), {"page": 2})
    assert len(r.context["page"].object_list) == 5


def test_list_hides_other_org(client, org, other_org, salesperson):
    mk(other_org, "Obcy Szpital")
    client.force_login(salesperson)
    assert "Obcy Szpital" not in client.get(reverse("customers:list")).content.decode()


def test_detail_renders(client, org, salesperson):
    from apps.tasks.models import Task, TaskStatus

    c = mk(org, "Szpital Alfa", "Radom", nip=VALID_NIP)
    Contact.objects.create(organization=org, customer=c, name="Jan Kowalski", position="ordynator")
    Task.objects.create(organization=org, title="Oferta", customer=c, status=TaskStatus.NOWE)
    client.force_login(salesperson)
    r = client.get(reverse("customers:detail", args=[c.pk]))
    assert r.status_code == 200
    body = r.content.decode()
    assert "Jan Kowalski" in body and VALID_NIP in body
    assert r.context["open_tasks"] == 1
    assert any(e["title"] == "Dodano klienta" for e in r.context["timeline"])


def test_detail_other_org_404(client, other_org, salesperson):
    c = mk(other_org, "Obcy")
    client.force_login(salesperson)
    assert client.get(reverse("customers:detail", args=[c.pk])).status_code == 404


def test_user_without_org_forbidden(client, django_user_model):
    user = django_user_model.objects.create_user(username="bezorg", password="haslo-testowe-123")
    client.force_login(user)
    assert client.get(reverse("customers:list")).status_code == 403


# ---------------------------------------------------------------- views: create/edit (role)

@pytest.mark.parametrize("who,expected", [("salesperson", 403), ("service_user", 403), ("office_user", 200),
                                          ("boss", 200)])
def test_create_role_gate(client, request, who, expected):
    client.force_login(request.getfixturevalue(who))
    assert client.get(reverse("customers:create")).status_code == expected


def test_create_customer_sets_org(client, org, office_user, salesperson):
    client.force_login(office_user)
    r = client.post(reverse("customers:create"), {"name": "Nowy Szpital", "city": "Radom", "owner": salesperson.pk,
                                                  "is_active": "on"})
    assert r.status_code == 302
    c = Customer.objects.get(name="Nowy Szpital")
    assert c.organization == org and c.owner == salesperson


def test_create_customer_invalid(client, office_user):
    client.force_login(office_user)
    r = client.post(reverse("customers:create"), {"name": ""})
    assert r.status_code == 200 and r.context["form"].errors
    assert not Customer.objects.exists()


def test_salesperson_cannot_post_create(client, salesperson):
    client.force_login(salesperson)
    assert client.post(reverse("customers:create"), {"name": "X"}).status_code == 403
    assert not Customer.objects.exists()


def test_edit_customer(client, org, boss):
    c = mk(org, "Stara nazwa")
    client.force_login(boss)
    assert client.get(reverse("customers:edit", args=[c.pk])).status_code == 200
    r = client.post(reverse("customers:edit", args=[c.pk]), {"name": "Nowa nazwa", "is_active": "on"})
    assert r.status_code == 302
    c.refresh_from_db()
    assert c.name == "Nowa nazwa" and c.organization == org


def test_edit_forbidden_for_salesperson(client, org, salesperson):
    c = mk(org, "X")
    client.force_login(salesperson)
    assert client.post(reverse("customers:edit", args=[c.pk]), {"name": "Y"}).status_code == 403


def test_edit_other_org_404(client, other_org, office_user):
    c = mk(other_org, "Obcy")
    client.force_login(office_user)
    assert client.post(reverse("customers:edit", args=[c.pk]), {"name": "Y"}).status_code == 404
    c.refresh_from_db()
    assert c.name == "Obcy"


# ---------------------------------------------------------------- views: contacts

@pytest.mark.parametrize("who", ["salesperson", "service_user", "office_user", "boss"])
def test_add_contact_any_role(client, org, request, who):
    c = mk(org, "Szpital")
    client.force_login(request.getfixturevalue(who))
    r = client.post(reverse("customers:contact_add", args=[c.pk]), {"name": "Ewa Zielińska", "position": "oddz."})
    assert r.status_code == 302
    contact = c.contacts.get()
    assert contact.organization == org and contact.name == "Ewa Zielińska"


def test_add_contact_invalid(client, org, salesperson):
    c = mk(org, "Szpital")
    client.force_login(salesperson)
    r = client.post(reverse("customers:contact_add", args=[c.pk]), {"name": ""})
    assert r.status_code == 400 and not c.contacts.exists()


def test_add_contact_get_not_allowed(client, org, salesperson):
    c = mk(org, "Szpital")
    client.force_login(salesperson)
    assert client.get(reverse("customers:contact_add", args=[c.pk])).status_code == 405


def test_add_contact_other_org_404(client, other_org, salesperson):
    c = mk(other_org, "Obcy")
    client.force_login(salesperson)
    r = client.post(reverse("customers:contact_add", args=[c.pk]), {"name": "X"})
    assert r.status_code == 404 and not Contact.objects.exists()


# ---------------------------------------------------------------- views: search

def test_search_html_fragment(client, org, salesperson):
    c = mk(org, "Szpital Alfa", "Radom")
    client.force_login(salesperson)
    r = client.get(reverse("customers:search"), {"q": "alfa"})
    body = r.content.decode()
    assert r.status_code == 200 and "<html" not in body
    assert f'data-customer-id="{c.pk}"' in body and "Radom" in body


def test_search_json_shape(client, org, salesperson):
    c = mk(org, "Szpital Alfa", "Radom", short_name="SA")
    client.force_login(salesperson)
    r = client.get(reverse("customers:search"), {"q": "alfa", "format": "json"})
    assert r.json() == {"results": [{"id": c.pk, "name": "Szpital Alfa", "city": "Radom"}]}


def test_search_limit_15(client, org, salesperson):
    for i in range(20):
        mk(org, f"Szpital {i:02d}")
    client.force_login(salesperson)
    r = client.get(reverse("customers:search"), {"q": "szpital", "format": "json"})
    assert len(r.json()["results"]) == 15


def test_search_empty_q_owner_first(client, org, salesperson):
    mk(org, "Aaa Klinika")
    mine = mk(org, "Zzz Szpital", owner=salesperson)
    client.force_login(salesperson)
    results = client.get(reverse("customers:search"), {"format": "json"}).json()["results"]
    assert results[0]["id"] == mine.pk and len(results) == 2


def test_search_org_isolation(client, org, other_org, salesperson):
    mk(other_org, "Obcy Szpital")
    client.force_login(salesperson)
    assert client.get(reverse("customers:search"), {"q": "Obcy", "format": "json"}).json() == {"results": []}
    assert "Brak klientów" in client.get(reverse("customers:search"), {"q": "Obcy"}).content.decode()


def test_search_requires_login(client):
    assert client.get(reverse("customers:search"), {"format": "json"}).status_code == 302


def test_nav_visible(client, org, salesperson):
    from apps.core.navigation import nav_for_user

    assert any(i["url_name"] == "customers:list" for i in nav_for_user(salesperson))


# --- API zapisu dla innych modułów (create_customer / update_customer) ---------------------------

@pytest.mark.django_db
def test_create_customer_service(org, salesperson, other_org_user):
    from apps.customers.services import create_customer

    customer = create_customer(org, name="Klinika Testowa", city="Radom", owner=salesperson)
    assert customer.organization == org and customer.owner == salesperson
    with pytest.raises(ValueError):
        create_customer(org, name="X", owner=other_org_user)
    with pytest.raises(ValueError):
        create_customer(org, name="X", id=999)


@pytest.mark.django_db
def test_update_customer_service_reports_changes(org, other_org_user):
    from apps.customers.models import Customer
    from apps.customers.services import update_customer

    customer = Customer.objects.create(organization=org, name="Klinika", city="Radom")
    assert update_customer(customer, {"city": "Radom"}) == []
    assert update_customer(customer, {"city": "Kielce", "phone": "123"}) == ["city", "phone"]
    customer.refresh_from_db()
    assert customer.city == "Kielce"
    with pytest.raises(ValueError):
        update_customer(customer, {"owner": other_org_user})
    with pytest.raises(ValueError):
        update_customer(customer, {"organization_id": 999})


# ---------------------------------------------------------------- oś czasu: widoczność per użytkownik

@pytest.fixture
def timeline_world(org, salesperson):
    """Klient handlowca A z jego notatką, zadaniem i zgłoszeniem — oglądany przez innych użytkowników."""
    from apps.service.services import create_ticket
    from apps.tasks.services import create_task
    from apps.users.models import Role, User
    from apps.voice.models import VoiceNote, VoiceStatus

    other = User.objects.create_user(username="handlowiec_b", password="haslo-testowe-123", organization=org,
                                     role=Role.HANDLOWIEC)
    customer = mk(org, "Szpital Tajny", "Radom", owner=salesperson)
    VoiceNote.objects.create(organization=org, author=salesperson, customer=customer,
                             status=VoiceStatus.DO_POTWIERDZENIA, transcript="x",
                             extraction={"summary": "Rabat 17% dla ordynatora"})
    create_task(org, title="Poufne zadanie A", created_by=salesperson, customer=customer)
    create_ticket(org, customer=customer, title="Poufne zgłoszenie A", reported_by=salesperson)
    return {"customer": customer, "other": other}


SECRETS = ("Rabat 17%", "Poufne zadanie A", "Poufne zgłoszenie A")


def test_timeline_hides_other_salesperson_entries(client, timeline_world):
    """Regresja: handlowiec B na karcie klienta A nie widzi notatek, zadań ani zgłoszeń A."""
    client.force_login(timeline_world["other"])
    content = client.get(reverse("customers:detail", args=[timeline_world["customer"].pk])).content.decode()
    assert "Szpital Tajny" in content
    for secret in SECRETS:
        assert secret not in content


def test_timeline_owner_and_office_see_everything(client, salesperson, office_user, boss, timeline_world):
    for user in (salesperson, office_user, boss):
        client.force_login(user)
        content = client.get(reverse("customers:detail", args=[timeline_world["customer"].pk])).content.decode()
        for secret in SECRETS:
            assert secret in content, (user.username, secret)


def test_timeline_service_role_sees_tasks_and_tickets_not_voice(service_user, timeline_world):
    entries = services.build_timeline(timeline_world["customer"], user=service_user)
    text = " ".join(e["title"] + " " + e["text"] for e in entries)
    assert "Poufne zadanie A" in text and "Poufne zgłoszenie A" in text
    assert "Rabat 17%" not in text


def test_timeline_other_salesperson_sees_own_items(org, timeline_world):
    from apps.service.services import create_ticket
    from apps.tasks.services import create_task

    other, customer = timeline_world["other"], timeline_world["customer"]
    create_task(org, title="Zadanie B", created_by=other, customer=customer)
    create_ticket(org, customer=customer, title="Zgłoszenie B", reported_by=other)
    titles = " ".join(e["title"] for e in services.build_timeline(customer, user=other))
    assert "Zadanie B" in titles and "Zgłoszenie B" in titles
    assert "Poufne" not in titles


def test_timeline_source_without_user_param_is_skipped_for_users(org, salesperson, monkeypatch):
    c = mk(org, "Szpital")
    now = timezone.now()
    monkeypatch.setattr(services, "collect_timeline_sources",
                        lambda: [lambda customer: [{"date": now, "title": "stare źródło"}]])
    assert services.build_timeline(c, user=salesperson) == []
    assert [e["title"] for e in services.build_timeline(c)] == ["stare źródło"]
