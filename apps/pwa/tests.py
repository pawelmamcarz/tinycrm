"""Testy aplikacji terenowej (PWA): manifest, service worker, ekran „Teren”, nagrywanie, role i izolacja."""
import datetime
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.pwa import views as pwa_views
from apps.pwa.registrations import NAV
from apps.tasks.models import Priority, Task, TaskStatus
from apps.users.models import Role, User
from apps.voice.models import VoiceNote, VoiceStatus

pytestmark = pytest.mark.django_db
PWA_DIR = Path(pwa_views.__file__).resolve().parent


def _login(client, user):
    client.force_login(user)
    return client


def _task(org, title, assignee, due, status=TaskStatus.PRZYPISANE, **kw):
    return Task.objects.create(organization=org, title=title, assignee=assignee, due_date=due, status=status, **kw)


def _note(org, author, status=VoiceStatus.DO_POTWIERDZENIA, **kw):
    return VoiceNote.objects.create(organization=org, author=author, status=status, typed_text="tekst", **kw)


# ---------------------------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------------------------

def test_manifest_is_valid_json_with_required_fields(client):
    resp = client.get(reverse("pwa:manifest"))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/manifest+json")
    data = json.loads(resp.content.decode("utf-8"))
    assert data["name"] == "TinyCRM Teren"
    assert data["short_name"] == "TinyCRM"
    assert data["start_url"] == "/teren/"
    assert data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["theme_color"].startswith("#") and data["background_color"].startswith("#")
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon["src"].endswith("icon.svg") for icon in data["icons"])


def test_manifest_url_matches_base_template_link():
    assert reverse("pwa:manifest") == "/teren/manifest.webmanifest"


def test_manifest_is_public(client):
    # Przeglądarki pobierają manifest bez ciasteczek — nie może wymagać logowania.
    assert client.get(reverse("pwa:manifest")).status_code == 200


def test_icon_files_exist_and_are_png():
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        raw = (PWA_DIR / "static" / "pwa" / name).read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        width = int.from_bytes(raw[16:20], "big")
        height = int.from_bytes(raw[20:24], "big")
        assert (width, height) == (size, size)
    assert (PWA_DIR / "static" / "pwa" / "icon.svg").read_text().lstrip().startswith("<svg")


# ---------------------------------------------------------------------------------------------
# Service worker
# ---------------------------------------------------------------------------------------------

def test_service_worker_headers_and_content(client):
    resp = client.get(reverse("pwa:service_worker"))
    assert resp.status_code == 200
    assert reverse("pwa:service_worker") == "/teren/sw.js"
    assert resp["Content-Type"].startswith("application/javascript")
    assert resp["Service-Worker-Allowed"] == "/"
    assert "no-cache" in resp.get("Cache-Control", "") or "max-age=0" in resp.get("Cache-Control", "")
    body = resp.content.decode("utf-8")
    assert body.startswith("self.TINYCRM_SW = ")
    for marker in ('addEventListener("push"', 'addEventListener("notificationclick"',
                   'addEventListener("fetch"', 'addEventListener("install"'):
        assert marker in body


def test_service_worker_config_is_valid_json(client):
    body = client.get(reverse("pwa:service_worker")).content.decode("utf-8")
    first_line = body.split("\n", 1)[0]
    config = json.loads(first_line[len("self.TINYCRM_SW = "):].rstrip(";"))
    assert config["offlineUrl"] == reverse("pwa:offline")
    assert reverse("pwa:offline") in config["precache"]
    assert reverse("pwa:home") in config["warmPages"]
    assert config["version"]
    assert any(url.endswith("pwa/queue.js") for url in config["precache"])


def test_shell_version_changes_with_content(monkeypatch, tmp_path):
    fake = tmp_path / "sw.js"
    fake.write_text("a")
    monkeypatch.setattr(pwa_views, "SW_SOURCE", fake)
    v1 = pwa_views._shell_version()
    fake.write_text("b")
    assert pwa_views._shell_version() != v1


def test_offline_page_is_public_and_has_no_user_data(client, salesperson):
    _login(client, salesperson)
    resp = client.get(reverse("pwa:offline"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Brak połączenia" in content
    assert salesperson.username not in content and "csrf" not in content.lower()


# ---------------------------------------------------------------------------------------------
# Logowanie i role
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["pwa:home", "pwa:record"])
def test_login_required(client, name):
    resp = client.get(reverse(name))
    assert resp.status_code == 302
    assert "/logowanie/" in resp["Location"]


@pytest.mark.parametrize("fixture", ["salesperson", "office_user", "service_user", "boss"])
@pytest.mark.parametrize("name", ["pwa:home", "pwa:record"])
def test_all_roles_can_open(client, request, fixture, name):
    _login(client, request.getfixturevalue(fixture))
    assert client.get(reverse(name)).status_code == 200


def test_user_without_organization_is_denied(client, db):
    user = User.objects.create_user(username="bezorg", password="haslo-testowe-123", role=Role.HANDLOWIEC)
    _login(client, user)
    assert client.get(reverse("pwa:home")).status_code == 403
    assert client.get(reverse("pwa:record")).status_code == 403


def test_nav_registration():
    assert NAV == [{"label": "Teren", "url_name": "pwa:home", "roles": [], "order": 5, "icon": "📱"}]


def test_nav_visible_for_salesperson(client, salesperson):
    _login(client, salesperson)
    resp = client.get(reverse("pwa:home"))
    assert any(item["url_name"] == "pwa:home" for item in resp.context["nav_items"])


# ---------------------------------------------------------------------------------------------
# Ekran „Teren”
# ---------------------------------------------------------------------------------------------

def test_home_shows_today_and_overdue_own_tasks_only(client, org, other_org, salesperson, office_user,
                                                     other_org_user):
    today = timezone.localdate()
    yesterday = today - datetime.timedelta(days=1)
    tomorrow = today + datetime.timedelta(days=1)
    _task(org, "Zadzwonić do ordynatora dziś", salesperson, today, priority=Priority.PILNY)
    _task(org, "Oferta zaległa", salesperson, yesterday)
    _task(org, "Jutrzejsze zadanie", salesperson, tomorrow)
    _task(org, "Zadanie bez terminu", salesperson, None)
    _task(org, "Zrobione dziś", salesperson, today, status=TaskStatus.ZROBIONE)
    _task(org, "Odrzucone wczoraj", salesperson, yesterday, status=TaskStatus.ODRZUCONE)
    _task(org, "Cudze zadanie biura", office_user, today)
    _task(other_org, "Obca organizacja", salesperson, today)
    _task(other_org, "Obcy użytkownik", other_org_user, today)

    _login(client, salesperson)
    resp = client.get(reverse("pwa:home"))
    assert resp.status_code == 200
    titles = [t.title for t in resp.context["tasks"]]
    assert titles == ["Oferta zaległa", "Zadzwonić do ordynatora dziś"]
    assert resp.context["overdue_count"] == 1
    content = resp.content.decode()
    assert "Nagraj notatkę po wizycie" in content
    assert reverse("pwa:record") in content
    assert "po terminie" in content and "pilne" in content
    for hidden in ("Jutrzejsze", "bez terminu", "Zrobione dziś", "Cudze zadanie", "Obca organizacja", "Obcy użytkownik"):
        assert hidden not in content


def test_home_shows_last_five_own_notes_with_confirm_link(client, org, other_org, salesperson, office_user,
                                                          other_org_user):
    customer = Customer.objects.create(organization=org, name="Szpital Powiatowy w Zielonkach", city="Zielonki")
    old = [_note(org, salesperson, status=VoiceStatus.POTWIERDZONA) for _ in range(3)]
    to_confirm = _note(org, salesperson, customer=customer)
    error_note = _note(org, salesperson, status=VoiceStatus.BLAD)
    newest = _note(org, salesperson, status=VoiceStatus.DO_WERYFIKACJI)
    foreign_same_org = _note(org, office_user)
    foreign_org = _note(other_org, other_org_user)

    _login(client, salesperson)
    resp = client.get(reverse("pwa:home"))
    notes = resp.context["notes"]
    assert [n.pk for n in notes] == [newest.pk, error_note.pk, to_confirm.pk, old[2].pk, old[1].pk]
    assert foreign_same_org not in notes and foreign_org not in notes
    assert resp.context["to_confirm_count"] == 1
    content = resp.content.decode()
    assert "Potwierdź" in content
    assert reverse("voice:detail", args=[to_confirm.pk]) in content
    assert "Szpital Powiatowy" in content or "Zielonki" in content
    assert reverse("voice:detail", args=[foreign_org.pk]) not in content
    assert reverse("voice:detail", args=[foreign_same_org.pk]) not in content


def test_home_empty_state(client, salesperson):
    _login(client, salesperson)
    content = client.get(reverse("pwa:home")).content.decode()
    assert "Nic na dziś" in content
    assert "Nie masz jeszcze notatek" in content


def test_home_has_queue_banner_config_and_scripts(client, salesperson):
    _login(client, salesperson)
    resp = client.get(reverse("pwa:home"))
    content = resp.content.decode()
    assert 'id="queue-banner"' in content
    assert 'id="pwa-config"' in content and "pwa/home.js" in content
    config = resp.context["pwa_config"]
    assert config["uploadUrl"] == reverse("voice:upload")
    assert config["swUrl"] == reverse("pwa:service_worker")
    assert config["customerSearchUrl"] == reverse("customers:search")


# ---------------------------------------------------------------------------------------------
# Nagrywanie
# ---------------------------------------------------------------------------------------------

def test_record_page_renders(client, salesperson):
    _login(client, salesperson)
    resp = client.get(reverse("pwa:record"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Tekst (możesz poprawić lub wpisać)" in content
    assert "Nie wiem / AI rozpozna" in content
    assert 'id="rec-btn"' in content and 'id="send-btn"' in content
    assert "pwa/record.js" in content
    assert resp.context["pwa_config"]["maxSeconds"] == 300


def test_record_preselects_own_org_customer(client, org, salesperson):
    customer = Customer.objects.create(organization=org, name="Klinika Pod Lipami", city="Brzozów")
    _login(client, salesperson)
    resp = client.get(reverse("pwa:record"), {"klient": customer.pk})
    assert resp.context["initial_customer"] == {"id": customer.pk, "label": "Klinika Pod Lipami · Brzozów"}


@pytest.mark.parametrize("raw", ["abc", "١٢", "", "99999999"])
def test_record_ignores_invalid_or_unknown_customer(client, salesperson, raw):
    _login(client, salesperson)
    resp = client.get(reverse("pwa:record"), {"klient": raw})
    assert resp.status_code == 200
    assert resp.context["initial_customer"] is None


def test_record_does_not_preselect_foreign_customer(client, other_org, salesperson):
    foreign = Customer.objects.create(organization=other_org, name="Obca przychodnia", city="Gdzieś")
    _login(client, salesperson)
    resp = client.get(reverse("pwa:record"), {"klient": foreign.pk})
    assert resp.context["initial_customer"] is None
    assert "Obca przychodnia" not in resp.content.decode()


# ---------------------------------------------------------------------------------------------
# JS
# ---------------------------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="brak Node.js")
def test_javascript_syntax():
    files = sorted((PWA_DIR / "static" / "pwa").glob("*.js")) + [PWA_DIR / "templates" / "pwa" / "sw.js"]
    for path in files:
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_record_js_contract():
    js = (PWA_DIR / "static" / "pwa" / "record.js").read_text()
    queue = (PWA_DIR / "static" / "pwa" / "queue.js").read_text()
    assert "audio/webm;codecs=opus" in js and "audio/mp4" in js
    assert "pl-PL" in js and "webkitSpeechRecognition" in js
    assert "Zapisano offline – wyślę, gdy wróci zasięg" in js
    for field in ("audio", "typed_text", "customer_id", "client_uuid", "recorded_at"):
        assert f'form.append("{field}"' in queue
    assert "X-CSRFToken" in queue and "csrftoken" in queue


# ---------------------------------------------------------------------------------------------
# Zachowanie JS (node --test): kolejka offline i service worker
# ---------------------------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="brak Node.js")
def test_js_behaviour_suites():
    """Kolejka: rekordy związane z kontem, wyścig „Wyślij” z trwającą wysyłką.
    Service worker: przekierowania nie czyszczą cache, logowanie czyści i dogrzewa ekran nagrywania,
    `?klient=` nie trafia do cache, kliknięcie powiadomienia nie niszczy ekranu nagrywania."""
    tests = sorted(str(p) for p in (PWA_DIR / "js_tests").glob("*.test.mjs"))
    assert tests
    result = subprocess.run(["node", "--test", *tests], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_client_config_binds_queue_to_user(client, org, salesperson):
    _login(client, salesperson)
    for name in ("pwa:home", "pwa:record"):
        content = client.get(reverse(name)).content.decode()
        assert f'"owner": "{org.pk}:{salesperson.pk}"' in content


def test_service_worker_config_has_record_path(client):
    body = client.get(reverse("pwa:service_worker")).content.decode("utf-8")
    config = json.loads(body.split("\n", 1)[0][len("self.TINYCRM_SW = "):].rstrip(";"))
    assert config["recordPath"] == reverse("pwa:record")
    assert reverse("pwa:record") in config["warmPages"]


def test_record_js_discard_and_text_source_contract():
    js = (PWA_DIR / "static" / "pwa" / "record.js").read_text()
    queue = (PWA_DIR / "static" / "pwa" / "queue.js").read_text()
    # „Usuń nagranie” i „Nagraj od nowa” czyszczą tekst z dyktowania poprzedniego ujęcia.
    discard = js[js.index('els.discard.addEventListener("click"'):]
    assert discard.index("clearTextForNewTake()") < discard.index("});")
    start = js[js.index("async function startRecording()"):]
    assert "if (state.blob) clearTextForNewTake();" in start[:400]
    # Serwer dostaje źródło tekstu (dyktowanie vs ręczny), żeby urwane dyktowanie nie zastąpiło nagrania.
    assert 'form.append("text_source"' in queue and "textSource:" in js
    assert "AI przepisze nagranie" not in js
