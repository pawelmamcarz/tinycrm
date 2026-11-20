"""Testy modułu notatek głosowych: transkrypcja, ekstrakcja (heurystyka + AI), pipeline, zatwierdzenie,
FSM, zadania okresowe, uprawnienia ról, izolacja organizacji i widoki."""
import json
from datetime import date, timedelta
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.notify.models import Notification
from apps.service.models import PartOrder, ServiceTicket
from apps.tasks.models import Task
from apps.users.models import Role, User
from apps.voice import extraction, registrations, services, tasks as voice_tasks, transcription, views
from apps.voice.models import VOICE_LEGAL_TRANSITIONS, VoiceNote, VoiceStatus

TODAY = date(2026, 11, 18)  # środa
SAMPLE = ("Byłem w szpitalu w Radomiu u doktora Nowaka. Chcą ofertę na dwa kardiomonitory do piątku. "
          "W USG na ginekologii padła głowica, trzeba zamówić nową głowicę konweksową. "
          "Zadzwonić do nich w poniedziałek.")


@pytest.fixture
def radom(org):
    return Customer.objects.create(organization=org, name="Szpital Specjalistyczny w Radomiu", city="Radom")


@pytest.fixture
def kielce(org):
    return Customer.objects.create(organization=org, name="Przychodnia Zdrowie Plus", city="Kielce")


@pytest.fixture
def foreign_customer(other_org):
    return Customer.objects.create(organization=other_org, name="Szpital Miejski w Radomiu", city="Radom")


@pytest.fixture
def other_salesperson(org):
    return User.objects.create_user(username="ewa", password="haslo-testowe-123", organization=org,
                                    role=Role.HANDLOWIEC)


def make_note(org, author, status=VoiceStatus.NOWA, **kwargs):
    return VoiceNote.objects.create(organization=org, author=author, status=status, **kwargs)


def ready_note(org, author, customer=None, **extra):
    """Notatka po przetworzeniu (DO_POTWIERDZENIA) z wynikiem ekstrakcji próbki."""
    result = extraction.heuristic_extract(org, SAMPLE, TODAY)
    return make_note(org, author, status=VoiceStatus.DO_POTWIERDZENIA, transcript=SAMPLE, extraction=result,
                     customer=customer, **extra)


# =============================================================================================
# Heurystyka
# =============================================================================================

class TestHeuristic:
    def test_sample_from_brief(self, org, radom):
        result = extraction.extract(org, SAMPLE, today=TODAY)
        assert result["engine"] == "heurystyka"
        assert result["customer_id"] == radom.pk
        assert result["contact_person"] == "dr Nowak"
        by_kind = {t["kind"]: t for t in result["tasks"]}
        assert by_kind["OFERTA"]["due_date"] == "2026-11-20"  # piątek
        assert by_kind["OFERTA"]["assignee_role"] == "BIURO"
        assert "kardiomonitory" in by_kind["OFERTA"]["title"]
        assert by_kind["KONTAKT"]["due_date"] == "2026-11-23"  # poniedziałek
        assert by_kind["KONTAKT"]["assignee_role"] == "HANDLOWIEC"
        assert len(result["service"]) == 1
        item = result["service"][0]
        assert item["parts"] == [{"name": "głowica konweksowa", "quantity": 1}]
        assert "USG" in item["device_hint"] and "ginekologii" in item["device_hint"]
        assert result["follow_up_date"] == "2026-11-23"
        assert "Radomiu" in result["customer_guess"]
        # Zdanie kontekstowe „Byłem w…” nie jest zadaniem.
        assert not any(t["title"].startswith("Byłem") for t in result["tasks"])

    @pytest.mark.parametrize("text,expected", [
        ("zrobić to dziś", date(2026, 11, 18)),
        ("dzisiaj wysłać", date(2026, 11, 18)),
        ("jutro wysłać", date(2026, 11, 19)),
        ("pojutrze oddzwonić", date(2026, 11, 20)),
        ("w poniedziałek", date(2026, 11, 23)),
        ("we wtorek", date(2026, 11, 24)),
        ("w środę", date(2026, 11, 25)),  # dziś środa → następna
        ("w czwartek", date(2026, 11, 19)),
        ("do piątku", date(2026, 11, 20)),
        ("w sobotę", date(2026, 11, 21)),
        ("w niedzielę", date(2026, 11, 22)),
        ("za tydzień", date(2026, 11, 25)),
        ("za 3 dni", date(2026, 11, 21)),
        ("za pięć dni", date(2026, 11, 23)),
        ("za dwa tygodnie", date(2026, 12, 2)),
        ("na koniec miesiąca", date(2026, 11, 30)),
        ("do 5.01", date(2027, 1, 5)),
        ("do 25.11", date(2026, 11, 25)),
        ("termin 12.12.2026", date(2026, 12, 12)),
        ("w przyszłym tygodniu", date(2026, 11, 23)),
        ("bez terminu", None),
        ("do 31.02", None),
        # Regresje: godzina/wersja z kropką to nie data; „w przyszłym tygodniu w środę” = środa przyszłego tygodnia.
        ("o 14.11 w czwartek", date(2026, 11, 19)),
        ("o 10.05 zadzwonić", None),
        ("godz. 14.30 jutro", date(2026, 11, 19)),
        ("wersja 2.1 do piątku", date(2026, 11, 20)),
        ("w przyszłym tygodniu w środę", date(2026, 11, 25)),
        ("w środę w przyszłym tygodniu", date(2026, 11, 25)),
        ("przyszłym tygodniu we wtorek", date(2026, 11, 24)),
    ])
    def test_relative_dates(self, text, expected):
        assert extraction.parse_relative_date(text, TODAY) == expected

    @pytest.mark.parametrize("text,kind", [
        ("Przygotować ofertę na pompy", "OFERTA"),
        ("Chcą zamówić 10 opakowań elektrod", "ZAMOWIENIE"),
        ("Zadzwonić do kierownika", "KONTAKT"),
        ("Umówić spotkanie z dyrektorem", "KONTAKT"),
        ("Ogłaszają przetarg na respiratory", "PRZETARG"),
        ("Trzeba zrobić przegląd defibrylatora", "PRZEGLAD"),
        ("Uszkodzony kabel EKG", "SERWIS"),
        ("Trzeba sprawdzić fakturę", "INNE"),
        ("Ładna pogoda", None),
    ])
    def test_classify(self, text, kind):
        assert extraction.classify(text) == kind

    def test_parts_parsing(self):
        parts = extraction.parse_parts("trzeba wymienić baterię i zamówić 2 elektrody do EKG")
        assert {"name": "bateria", "quantity": 1} in parts
        assert {"name": "elektrody", "quantity": 2} in parts
        assert extraction.parse_parts("zamówić części do USG") == []
        assert extraction.parse_parts("potrzebna część: czujnik SpO2") == [{"name": "czujnik SpO2", "quantity": 1}]

    @pytest.mark.parametrize("text,expected", [
        ("Rozmawiałem z panią Kowalską", "pani Kowalska"),
        ("byłem u ordynatora Wiśniewskiego", "ordynator Wiśniewski"),
        ("Spotkanie z dr Lis", "dr Lis"),
        ("brak osoby", ""),
    ])
    def test_contact_person(self, text, expected):
        assert extraction.find_contact_person(text) == expected

    def test_urgent_and_office_role(self, org):
        result = extraction.heuristic_extract(org, "Pilnie zadzwonić do nich, niech biuro to zrobi jutro.", TODAY)
        task = result["tasks"][0]
        assert task["priority"] == 3
        assert task["assignee_role"] == "BIURO"
        assert task["due_date"] == "2026-11-19"

    def test_unknown_customer_and_empty(self, org, radom):
        result = extraction.heuristic_extract(org, "Chcą ofertę na pompy jutro.", TODAY)
        assert result["customer_id"] is None
        empty = extraction.extract(org, "", today=TODAY)
        assert empty["tasks"] == [] and empty["service"] == [] and empty["customer_id"] is None

    def test_heuristic_only_matches_own_org(self, org, foreign_customer):
        result = extraction.extract(org, SAMPLE, today=TODAY)
        assert result["customer_id"] is None


# =============================================================================================
# Ścieżka AI
# =============================================================================================

class TestAIExtraction:
    def test_valid_ai_result_is_cleaned(self, org, radom):
        raw = {
            "customer_id": radom.pk, "customer_guess": "szpital w Radomiu", "contact_person": "dr Nowak",
            "summary": "Wizyta w szpitalu.",
            "tasks": [
                {"title": "Oferta na kardiomonitory", "kind": "OFERTA", "due_date": "2026-11-20",
                 "assignee_role": "BIURO", "priority": 2},
                {"title": "Coś dziwnego", "kind": "TELEPORTACJA", "due_date": "w piątek", "assignee_role": "KOSMOS",
                 "priority": 9},
                {"title": "", "kind": "OFERTA"},
                "śmieci",
            ],
            "service": [{"title": "Awaria USG", "description": "Padła głowica", "device_hint": "USG",
                         "parts": [{"name": "głowica konweksowa", "quantity": "2"}, {"name": ""}, "bateria"]}],
            "follow_up_date": "2026-13-45",
        }
        with mock.patch("apps.voice.extraction.ask_json", return_value=raw) as ask:
            result = extraction.extract(org, SAMPLE, today=TODAY)
        system_prompt = ask.call_args[0][0]
        assert "2026-11-18" in system_prompt and "środa" in system_prompt
        assert f"{radom.pk}: Szpital Specjalistyczny w Radomiu" in system_prompt
        assert result["engine"] == "ai"
        assert result["customer_id"] == radom.pk
        assert len(result["tasks"]) == 2
        odd = result["tasks"][1]
        assert odd["kind"] == "INNE" and odd["due_date"] is None and odd["priority"] == 3
        assert odd["assignee_role"] == "BIURO"
        assert result["service"][0]["parts"] == [{"name": "głowica konweksowa", "quantity": 2},
                                                 {"name": "bateria", "quantity": 1}]
        assert result["follow_up_date"] is None

    def test_malformed_json_falls_back_to_heuristic(self, org, radom):
        with mock.patch("apps.voice.extraction.ask_json", return_value={"_error": "invalid_json", "_raw": "xx"}):
            result = extraction.extract(org, SAMPLE, today=TODAY)
        assert result["engine"] == "heurystyka"
        assert result["customer_id"] == radom.pk
        assert any(t["kind"] == "OFERTA" for t in result["tasks"])

    def test_non_dict_and_wrong_types(self, org):
        with mock.patch("apps.voice.extraction.ask_json", return_value=["nie", "dict"]):
            assert extraction.extract(org, SAMPLE, today=TODAY)["engine"] == "heurystyka"
        with mock.patch("apps.voice.extraction.ask_json", return_value={"tasks": "x", "service": None,
                                                                          "customer_id": True}):
            result = extraction.extract(org, "Krótko.", today=TODAY)
        assert result["engine"] == "ai" and result["tasks"] == [] and result["service"] == []
        assert result["customer_id"] is None

    def test_exception_in_ai_path_falls_back(self, org):
        with mock.patch("apps.voice.extraction.ask_json", side_effect=RuntimeError("boom")):
            assert extraction.extract(org, SAMPLE, today=TODAY)["engine"] == "heurystyka"

    def test_cross_org_customer_id_rejected(self, org, radom, foreign_customer):
        raw = {"customer_id": foreign_customer.pk, "customer_guess": "szpital w Radomiu", "tasks": [], "service": []}
        with mock.patch("apps.voice.extraction.ask_json", return_value=raw):
            result = extraction.extract(org, SAMPLE, today=TODAY)
        # ID z obcej organizacji odrzucone → dopasowanie po nazwie w SWOJEJ organizacji.
        assert result["customer_id"] == radom.pk

    def test_cross_org_customer_id_without_match_is_null(self, org, foreign_customer):
        raw = {"customer_id": str(foreign_customer.pk), "customer_guess": "", "tasks": [], "service": []}
        with mock.patch("apps.voice.extraction.ask_json", return_value=raw):
            result = extraction.extract(org, "Chcą ofertę.", today=TODAY)
        assert result["customer_id"] is None

    def test_no_api_key_uses_heuristic(self, org):
        assert extraction.extract(org, SAMPLE, today=TODAY)["engine"] == "heurystyka"


# =============================================================================================
# Transkrypcja
# =============================================================================================

class TestTranscription:
    def test_get_transcriber_by_setting(self):
        with override_settings(STT_PROVIDER="elevenlabs"):
            assert isinstance(transcription.get_transcriber(), transcription.ElevenLabsTranscriber)
        with override_settings(STT_PROVIDER="openai"):
            assert isinstance(transcription.get_transcriber(), transcription.OpenAITranscriber)
        with override_settings(STT_PROVIDER="demo"):
            assert isinstance(transcription.get_transcriber(), transcription.DemoTranscriber)

    def test_manual_text_preferred_over_stt(self, org, salesperson):
        note = make_note(org, salesperson, typed_text="Tekst poprawiony ręcznie", text_source="RECZNY",
                         audio=ContentFile(b"abc", "a.webm"))
        with override_settings(STT_PROVIDER="openai"), mock.patch.object(transcription, "_http_post") as post:
            assert transcription.transcribe_note(note) == "Tekst poprawiony ręcznie"
        post.assert_not_called()

    @pytest.mark.parametrize("source", ["DYKTOWANIE", ""])
    @override_settings(STT_PROVIDER="openai", OPENAI_API_KEY="k")
    def test_partial_dictation_does_not_replace_audio(self, org, salesperson, source):
        """Regresja: urwane dyktowanie („Byłem w szpitalu…”) nie może zastąpić pełnego nagrania."""
        note = make_note(org, salesperson, typed_text="Byłem w szpitalu w Kielcach", text_source=source,
                         audio=ContentFile(b"abc", "a.webm"))
        full = "Byłem w szpitalu w Kielcach. Trzeba wysłać ofertę do piątku i zamówić głowicę."
        with mock.patch.object(transcription, "_http_post", return_value=(200, {"text": full}, "")) as post:
            assert transcription.transcribe_note(note) == full
        post.assert_called_once()

    @override_settings(STT_PROVIDER="openai", OPENAI_API_KEY="k")
    def test_dictation_is_fallback_when_stt_fails(self, org, salesperson):
        note = make_note(org, salesperson, typed_text="Zapas z dyktowania", text_source="DYKTOWANIE",
                         audio=ContentFile(b"abc", "a.webm"))
        with mock.patch.object(transcription, "_http_post", return_value=(500, None, "err")):
            assert transcription.transcribe_note(note) == "Zapas z dyktowania"
        with mock.patch.object(transcription, "_http_post", return_value=(200, {"text": "  "}, "")):
            assert transcription.transcribe_note(note) == "Zapas z dyktowania"

    def test_typed_text_used_in_demo_and_without_audio(self, org, salesperson):
        note = make_note(org, salesperson, typed_text="Tekst z dyktowania", audio=ContentFile(b"abc", "a.webm"))
        assert transcription.transcribe_note(note) == "Tekst z dyktowania"  # demo
        no_audio = make_note(org, salesperson, typed_text="Sam tekst")
        with override_settings(STT_PROVIDER="openai"), mock.patch.object(transcription, "_http_post") as post:
            assert transcription.transcribe_note(no_audio) == "Sam tekst"
        post.assert_not_called()

    def test_demo_audio_only_raises(self, org, salesperson):
        note = make_note(org, salesperson, audio=ContentFile(b"abc", "a.webm"))
        with pytest.raises(transcription.TranscriptionError, match="Brak transkrypcji w trybie demo"):
            transcription.transcribe_note(note)

    @override_settings(STT_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="test-key")
    def test_elevenlabs_request(self, org, salesperson):
        note = make_note(org, salesperson, audio=ContentFile(b"RIFFdata", "nagranie.webm"))
        with mock.patch.object(transcription, "_http_post", return_value=(200, {"text": " Dzień dobry "}, "")) as post:
            assert transcription.transcribe_note(note) == "Dzień dobry"
        url = post.call_args[0][0]
        kwargs = post.call_args[1]
        assert url == "https://api.elevenlabs.io/v1/speech-to-text"
        assert kwargs["headers"] == {"xi-api-key": "test-key"}
        assert kwargs["data"] == {"model_id": "scribe_v1", "language_code": "pol"}
        assert kwargs["files"]["file"][1] == b"RIFFdata"
        assert kwargs["files"]["file"][2] == "audio/webm"

    @override_settings(STT_PROVIDER="openai", OPENAI_API_KEY="test-key")
    def test_openai_request_and_http_error(self, org, salesperson):
        note = make_note(org, salesperson, audio=ContentFile(b"data", "n.m4a"))
        with mock.patch.object(transcription, "_http_post", return_value=(200, {"text": "Ok"}, "")) as post:
            assert transcription.transcribe_note(note) == "Ok"
        assert post.call_args[0][0] == "https://api.openai.com/v1/audio/transcriptions"
        assert post.call_args[1]["data"] == {"model": "gpt-4o-transcribe", "language": "pl"}
        assert post.call_args[1]["headers"]["Authorization"] == "Bearer test-key"
        with mock.patch.object(transcription, "_http_post", return_value=(500, None, "err")):
            with pytest.raises(transcription.TranscriptionError, match="HTTP 500"):
                transcription.transcribe_note(note)
        with mock.patch.object(transcription, "_http_post", side_effect=ConnectionError("x")):
            with pytest.raises(transcription.TranscriptionError, match="połączenia"):
                transcription.transcribe_note(note)
        with mock.patch.object(transcription, "_http_post", return_value=(200, {"bez": "tekstu"}, "")):
            with pytest.raises(transcription.TranscriptionError):
                transcription.transcribe_note(note)

    @override_settings(STT_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="")
    def test_missing_key(self, org, salesperson):
        note = make_note(org, salesperson, audio=ContentFile(b"data", "n.webm"))
        with pytest.raises(transcription.TranscriptionError, match="brak klucza"):
            transcription.transcribe_note(note)


# =============================================================================================
# Pipeline
# =============================================================================================

class TestProcess:
    def test_process_typed_note(self, org, salesperson, radom):
        note = make_note(org, salesperson, typed_text=SAMPLE)
        note = services.process_voice_note(note, today=TODAY)
        assert note.status == VoiceStatus.DO_POTWIERDZENIA
        assert note.transcript == SAMPLE
        assert note.customer == radom
        assert note.extraction["engine"] == "heurystyka"
        notification = Notification.objects.get(user=salesperson)
        assert notification.url == reverse("voice:detail", args=[note.pk])

    def test_rep_selected_customer_wins(self, org, salesperson, radom, kielce):
        note = make_note(org, salesperson, typed_text=SAMPLE, customer=kielce)
        note = services.process_voice_note(note, today=TODAY)
        assert note.customer == kielce
        assert note.extraction["customer_id"] == kielce.pk

    def test_demo_audio_only_goes_to_verification(self, org, salesperson, office_user):
        note = make_note(org, salesperson, audio=ContentFile(b"abc", "a.webm"))
        note = services.process_voice_note(note)
        assert note.status == VoiceStatus.DO_WERYFIKACJI
        assert note.error == "Brak transkrypcji w trybie demo"
        assert note.transcript == ""
        assert Notification.objects.filter(user=office_user).exists()

    def test_stt_error_never_crashes(self, org, salesperson):
        note = make_note(org, salesperson, audio=ContentFile(b"abc", "a.webm"))
        with override_settings(STT_PROVIDER="openai", OPENAI_API_KEY="k"), \
                mock.patch.object(transcription, "_http_post", side_effect=TimeoutError()):
            note = services.process_voice_note(note)
        assert note.status == VoiceStatus.DO_WERYFIKACJI
        assert "openai" in note.error

    def test_extraction_crash_sets_blad(self, org, salesperson):
        note = make_note(org, salesperson, typed_text="Tekst")
        with mock.patch("apps.voice.services.extract", side_effect=RuntimeError("x")):
            note = services.process_voice_note(note)
        assert note.status == VoiceStatus.BLAD
        assert note.transcript == "Tekst"
        # Ponowne przetworzenie z BLAD jest dozwolone.
        note = services.process_voice_note(note)
        assert note.status == VoiceStatus.DO_POTWIERDZENIA

    def test_process_is_idempotent_for_confirmed(self, org, salesperson):
        note = make_note(org, salesperson, status=VoiceStatus.POTWIERDZONA, typed_text="x")
        with mock.patch("apps.voice.services.extract") as ext:
            services.process_voice_note(note)
        ext.assert_not_called()

    def test_task_wrapper(self, org, salesperson):
        note = make_note(org, salesperson, typed_text="Zadzwonić jutro.")
        assert voice_tasks.process_voice_note_task(note.pk) == VoiceStatus.DO_POTWIERDZENIA
        assert voice_tasks.process_voice_note_task(999999) is None


# =============================================================================================
# Zatwierdzenie
# =============================================================================================

class TestConfirm:
    def test_confirm_creates_everything_exactly_once(self, org, salesperson, office_user, service_user, radom):
        note = ready_note(org, salesperson, customer=radom)
        created = services.confirm_voice_note(note, salesperson)
        note.refresh_from_db()
        assert note.status == VoiceStatus.POTWIERDZONA and note.confirmed_at is not None
        tasks = Task.objects.filter(source_ref=f"voice:{note.pk}")
        assert tasks.count() == len(note.extraction["tasks"]) == len(created["tasks"])
        assert all(t.source == "GLOS" and t.customer == radom and t.created_by == salesperson for t in tasks)
        assert tasks.get(kind="OFERTA").assignee == office_user
        assert tasks.get(kind="OFERTA").due_date == date(2026, 11, 20)
        assert tasks.get(kind="KONTAKT").assignee == salesperson
        assert tasks.get(kind="SERWIS").assignee == service_user
        ticket = ServiceTicket.objects.get(source_ref=f"voice:{note.pk}")
        assert ticket.customer == radom and ticket.reported_by == salesperson
        part = PartOrder.objects.get(ticket=ticket)
        assert part.part_name == "głowica konweksowa" and part.quantity == 1
        assert note.extraction["result"]["ticket_ids"] == [ticket.pk]

        again = services.confirm_voice_note(note, salesperson)
        assert again == {"tasks": [], "tickets": [], "part_orders": []}
        assert Task.objects.filter(source_ref=f"voice:{note.pk}").count() == tasks.count()
        assert ServiceTicket.objects.count() == 1 and PartOrder.objects.count() == 1

    def test_edited_items(self, org, salesperson, office_user, radom):
        note = ready_note(org, salesperson)
        items = {"customer": radom, "tasks": [
            {"title": "Wysłać katalog", "kind": "NIEZNANY", "due_date": "2026-12-01", "assignee_role": "BIURO",
             "priority": "3"},
            {"title": "  ", "kind": "OFERTA"},
        ], "service": []}
        created = services.confirm_voice_note(note, salesperson, items)
        assert len(created["tasks"]) == 1
        task = created["tasks"][0]
        assert task.kind == "INNE" and task.priority == 3 and task.due_date == date(2026, 12, 1)
        assert ServiceTicket.objects.count() == 0
        note.refresh_from_db()
        assert note.customer == radom

    def test_service_requires_customer(self, org, salesperson):
        note = ready_note(org, salesperson)
        with pytest.raises(ValidationError):
            services.confirm_voice_note(note, salesperson)
        note.refresh_from_db()
        assert note.status == VoiceStatus.DO_POTWIERDZENIA
        assert Task.objects.count() == 0  # transakcja wycofana

    def test_cross_org_customer_rejected(self, org, salesperson, foreign_customer):
        note = ready_note(org, salesperson)
        with pytest.raises(ValidationError):
            services.confirm_voice_note(note, salesperson, {"customer": foreign_customer, "tasks": [],
                                                            "service": []})

    def test_confirm_from_verification_and_not_from_processing(self, org, salesperson, office_user, radom):
        note = ready_note(org, salesperson, customer=radom)
        note.status = VoiceStatus.DO_WERYFIKACJI
        note.save()
        assert services.confirm_voice_note(note, office_user)["tasks"]
        processing = make_note(org, salesperson, status=VoiceStatus.PRZETWARZANIE)
        assert services.confirm_voice_note(processing, salesperson) == {"tasks": [], "tickets": [],
                                                                         "part_orders": []}

    def test_assignee_fallbacks(self, org, salesperson):
        assert services.resolve_assignee(org, "BIURO", salesperson) is None
        assert services.resolve_assignee(org, "SERWIS", salesperson) is None
        assert services.resolve_assignee(org, "HANDLOWIEC", salesperson) == salesperson

    def test_send_to_verification(self, org, salesperson, office_user, radom):
        note = ready_note(org, salesperson)
        services.send_to_verification(note, salesperson, {"customer": radom, "tasks": [
            {"title": "Nowe", "kind": "OFERTA"}], "service": []})
        note.refresh_from_db()
        assert note.status == VoiceStatus.DO_WERYFIKACJI
        assert note.customer == radom
        assert [t["title"] for t in note.extraction["tasks"]] == ["Nowe"]
        assert Notification.objects.filter(user=office_user).exists()


# =============================================================================================
# FSM i zadania okresowe
# =============================================================================================

class TestFSMAndPeriodic:
    def test_illegal_transition(self, org, salesperson):
        note = make_note(org, salesperson, status=VoiceStatus.POTWIERDZONA)
        with pytest.raises(ValidationError):
            services.set_status(note, VoiceStatus.DO_WERYFIKACJI)
        assert VOICE_LEGAL_TRANSITIONS[VoiceStatus.POTWIERDZONA] == set()
        note2 = make_note(org, salesperson, status=VoiceStatus.DO_POTWIERDZENIA)
        services.set_status(note2, VoiceStatus.DO_WERYFIKACJI)
        assert note2.status == VoiceStatus.DO_WERYFIKACJI

    def test_escalate_unconfirmed(self, org, salesperson, office_user):
        old = make_note(org, salesperson, status=VoiceStatus.DO_POTWIERDZENIA)
        fresh = make_note(org, salesperson, status=VoiceStatus.DO_POTWIERDZENIA)
        VoiceNote.objects.filter(pk=old.pk).update(updated_at=timezone.now() - timedelta(hours=3))
        assert voice_tasks.escalate_unconfirmed() == 1
        old.refresh_from_db()
        fresh.refresh_from_db()
        assert old.status == VoiceStatus.DO_WERYFIKACJI
        assert fresh.status == VoiceStatus.DO_POTWIERDZENIA
        assert Notification.objects.filter(user=office_user, url=reverse("voice:detail", args=[old.pk])).exists()
        assert voice_tasks.escalate_unconfirmed() == 0

    def test_escalation_does_not_overwrite_confirmed_note(self, org, salesperson, office_user, radom):
        """Regresja wyścigu: eskalacja ze starą kopią nie nadpisuje POTWIERDZONA i nie pozwala zatwierdzić 2×."""
        note = ready_note(org, salesperson, customer=radom)
        stale_copy = VoiceNote.objects.get(pk=note.pk)  # kopia „z SELECT-a” eskalacji
        services.confirm_voice_note(note, salesperson)
        tasks_after_confirm = Task.objects.filter(source_ref=f"voice:{note.pk}").count()
        with pytest.raises(services.StaleStatus):
            services.set_status(stale_copy, VoiceStatus.DO_WERYFIKACJI, error="Niepotwierdzona")
        note.refresh_from_db()
        assert note.status == VoiceStatus.POTWIERDZONA
        assert services.confirm_voice_note(note, office_user) == {"tasks": [], "tickets": [], "part_orders": []}
        assert Task.objects.filter(source_ref=f"voice:{note.pk}").count() == tasks_after_confirm

    def test_escalate_unconfirmed_skips_note_confirmed_after_select(self, org, salesperson, office_user, radom,
                                                                   monkeypatch):
        note = ready_note(org, salesperson, customer=radom)
        VoiceNote.objects.filter(pk=note.pk).update(updated_at=timezone.now() - timedelta(hours=3))
        real_filter = VoiceNote.objects.filter
        calls = {"n": 0}

        def racing_filter(*args, **kwargs):
            # Pierwsze zapytanie (lista kandydatów) — po nim handlowiec zatwierdza notatkę.
            qs = real_filter(*args, **kwargs)
            if calls["n"] == 0:
                calls["n"] += 1
                rows = list(qs)
                services.confirm_voice_note(VoiceNote.objects.get(pk=note.pk), salesperson)
                return rows
            return qs

        class RowsWithSelectRelated(list):
            def select_related(self, *a):
                return self

        monkeypatch.setattr(VoiceNote.objects, "filter",
                            lambda *a, **k: (lambda r: RowsWithSelectRelated(r) if isinstance(r, list) else r)(
                                racing_filter(*a, **k)))
        assert voice_tasks.escalate_unconfirmed() == 0
        monkeypatch.undo()
        note.refresh_from_db()
        assert note.status == VoiceStatus.POTWIERDZONA
        assert not Notification.objects.filter(user=office_user, title="Niepotwierdzona notatka głosowa").exists()

    def test_retry_stuck_notes(self, org, salesperson, office_user):
        now = timezone.now()
        fresh_new = make_note(org, salesperson, typed_text="Zadzwonić jutro.")
        stuck_new = make_note(org, salesperson, typed_text="Zadzwonić jutro.")
        dead_new = make_note(org, salesperson, typed_text="x")
        stuck_processing = make_note(org, salesperson, status=VoiceStatus.PRZETWARZANIE, typed_text="x")
        VoiceNote.objects.filter(pk=stuck_new.pk).update(created_at=now - timedelta(minutes=10))
        VoiceNote.objects.filter(pk=dead_new.pk).update(created_at=now - timedelta(hours=3))
        VoiceNote.objects.filter(pk=stuck_processing.pk).update(updated_at=now - timedelta(hours=1))
        result = voice_tasks.retry_stuck_notes()
        assert result == {"requeued": 1, "escalated": 2}
        for n in (fresh_new, stuck_new, dead_new, stuck_processing):
            n.refresh_from_db()
        assert fresh_new.status == VoiceStatus.NOWA
        assert stuck_new.status == VoiceStatus.DO_POTWIERDZENIA  # tryb eager: przetworzona od razu
        assert dead_new.status == VoiceStatus.DO_WERYFIKACJI
        assert stuck_processing.status == VoiceStatus.DO_WERYFIKACJI
        assert Notification.objects.filter(user=office_user, url=reverse("voice:detail", args=[dead_new.pk])).exists()
        assert voice_tasks.retry_stuck_notes() == {"requeued": 0, "escalated": 0}

    def test_retry_stuck_notes_tolerates_broker_down(self, org, salesperson):
        note = make_note(org, salesperson, typed_text="x")
        VoiceNote.objects.filter(pk=note.pk).update(created_at=timezone.now() - timedelta(minutes=10))
        with mock.patch.object(voice_tasks.process_voice_note_task, "delay", side_effect=ConnectionError):
            assert voice_tasks.retry_stuck_notes() == {"requeued": 0, "escalated": 0}
        note.refresh_from_db()
        assert note.status == VoiceStatus.NOWA

    def test_processing_result_does_not_overwrite_escalated_note(self, org, salesperson, office_user):
        note = make_note(org, salesperson, typed_text="Zadzwonić jutro.")

        def escalate_meanwhile(n):
            VoiceNote.objects.filter(pk=n.pk).update(status=VoiceStatus.DO_WERYFIKACJI)
            return "Zadzwonić jutro."

        with mock.patch.object(services, "transcribe_note", side_effect=escalate_meanwhile):
            services.process_voice_note(note)
        note.refresh_from_db()
        assert note.status == VoiceStatus.DO_WERYFIKACJI

    def test_relative_dates_use_recording_day(self, org, salesperson):
        """Regresja: „jutro” nagrane w czwartek i wysłane w sobotę = piątek, nie niedziela."""
        thursday = timezone.make_aware(timezone.datetime(2026, 11, 19, 15, 0))
        note = make_note(org, salesperson, typed_text="Zadzwonić do nich jutro. Ofertę wysłać do piątku.",
                         recorded_at=thursday)
        services.process_voice_note(note)
        note.refresh_from_db()
        dues = sorted(t["due_date"] for t in note.extraction["tasks"] if t.get("due_date"))
        assert dues and dues[0] == "2026-11-20"
        assert all(d <= "2026-11-20" for d in dues)

    @override_settings(VOICE_AUDIO_RETENTION_DAYS=30)
    def test_purge_keeps_audio_of_unconfirmed_notes(self, org, salesperson):
        old = timezone.now() - timedelta(days=31)
        keep = [make_note(org, salesperson, status=st, audio=ContentFile(b"abc", f"{st}.webm"))
                for st in (VoiceStatus.NOWA, VoiceStatus.PRZETWARZANIE, VoiceStatus.BLAD, VoiceStatus.DO_WERYFIKACJI,
                           VoiceStatus.DO_POTWIERDZENIA)]
        VoiceNote.objects.filter(pk__in=[n.pk for n in keep]).update(created_at=old)
        assert voice_tasks.purge_old_audio() == 0
        assert all(VoiceNote.objects.get(pk=n.pk).audio for n in keep)

    @override_settings(VOICE_AUDIO_RETENTION_DAYS=30)
    def test_purge_old_audio(self, org, salesperson):
        old = make_note(org, salesperson, status=VoiceStatus.POTWIERDZONA, transcript="zostaje",
                        audio=ContentFile(b"abc", "a.webm"))
        fresh = make_note(org, salesperson, status=VoiceStatus.POTWIERDZONA, audio=ContentFile(b"abc", "b.webm"))
        VoiceNote.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))
        assert voice_tasks.purge_old_audio() == 1
        old.refresh_from_db()
        fresh.refresh_from_db()
        assert not old.audio and old.audio_deleted_at is not None and old.transcript == "zostaje"
        assert fresh.audio and fresh.audio_deleted_at is None
        assert voice_tasks.purge_old_audio() == 0


# =============================================================================================
# Rejestracje
# =============================================================================================

class TestRegistrations:
    def test_nav_periodic_timeline(self, org, salesperson, radom):
        assert registrations.NAV[0]["url_name"] == "voice:list" and registrations.NAV[0]["order"] == 15
        assert registrations.NAV[0]["roles"] == []
        names = {p["name"]: p for p in registrations.PERIODIC}
        assert names["voice-escalate-unconfirmed"]["crontab"] == {"minute": "*/15"}
        assert names["voice-purge-audio"]["task"] == "apps.voice.tasks.purge_old_audio"
        assert names["voice-retry-stuck"]["task"] == "apps.voice.tasks.retry_stuck_notes"
        note = ready_note(org, salesperson, customer=radom)
        entries = registrations.TIMELINE[0](radom)
        assert len(entries) == 1 and entries[0]["url"] == reverse("voice:detail", args=[note.pk])
        assert set(entries[0]) == {"date", "icon", "title", "text", "url"}


# =============================================================================================
# Widoki
# =============================================================================================

class TestUploadView:
    url = reverse("voice:upload")

    def test_login_required(self, client):
        response = client.post(self.url, {"typed_text": "x"})
        assert response.status_code == 302 and "next=" in response["Location"]

    def test_json_typed_text_and_idempotency(self, client, salesperson, radom):
        client.force_login(salesperson)
        payload = {"typed_text": SAMPLE, "client_uuid": "abc-123", "recorded_at": "2026-11-18T10:15:00"}
        response = client.post(self.url, json.dumps(payload), content_type="application/json",
                               HTTP_ACCEPT="application/json")
        assert response.status_code == 201
        data = response.json()
        note = VoiceNote.objects.get(pk=data["id"])
        assert data["status"] == VoiceStatus.DO_POTWIERDZENIA == note.status  # eager
        assert data["url"] == reverse("voice:detail", args=[note.pk])
        assert note.recorded_at is not None and note.author == salesperson
        again = client.post(self.url, json.dumps(payload), content_type="application/json",
                            HTTP_ACCEPT="application/json")
        assert again.status_code == 200 and again.json()["id"] == note.pk
        assert VoiceNote.objects.count() == 1

    def test_multipart_audio(self, client, salesperson, radom):
        client.force_login(salesperson)
        audio = SimpleUploadedFile("../../evil.sh", b"\x1aE\xdf\xa3audio", content_type="audio/webm;codecs=opus")
        response = client.post(self.url, {"audio": audio, "typed_text": SAMPLE, "customer_id": radom.pk,
                                          "client_uuid": "u-1"}, HTTP_ACCEPT="application/json")
        assert response.status_code == 201
        note = VoiceNote.objects.get()
        assert note.audio.name.endswith(".webm") and "evil" not in note.audio.name
        assert note.customer == radom

    def test_audio_only_demo_goes_to_verification(self, client, salesperson):
        client.force_login(salesperson)
        audio = SimpleUploadedFile("a.ogg", b"OggS....", content_type="audio/ogg")
        response = client.post(self.url, {"audio": audio}, HTTP_ACCEPT="application/json")
        assert response.status_code == 201
        assert response.json()["status"] == VoiceStatus.DO_WERYFIKACJI

    def test_rejects_bad_content_type(self, client, salesperson):
        client.force_login(salesperson)
        bad = SimpleUploadedFile("a.html", b"<script>", content_type="text/html")
        response = client.post(self.url, {"audio": bad}, HTTP_ACCEPT="application/json")
        assert response.status_code == 400
        assert VoiceNote.objects.count() == 0

    def test_rejects_too_large(self, client, salesperson, monkeypatch):
        monkeypatch.setattr(views, "MAX_AUDIO_BYTES", 10)
        client.force_login(salesperson)
        big = SimpleUploadedFile("a.webm", b"x" * 11, content_type="audio/webm")
        response = client.post(self.url, {"audio": big}, HTTP_ACCEPT="application/json")
        assert response.status_code == 413
        assert VoiceNote.objects.count() == 0

    def test_requires_audio_or_text(self, client, salesperson):
        client.force_login(salesperson)
        response = client.post(self.url, {"typed_text": "  "}, HTTP_ACCEPT="application/json")
        assert response.status_code == 400

    def test_unknown_or_cross_org_customer_falls_back_to_ai(self, client, salesperson, foreign_customer, radom):
        """Klient usunięty/obcy: notatka z kolejki nie może utknąć na 400 — klienta dopasuje AI (bez wycieku)."""
        client.force_login(salesperson)
        for i, raw in enumerate((foreign_customer.pk, "١٢", 999999)):
            response = client.post(self.url, {"typed_text": "Byłem w szpitalu w Radomiu.", "customer_id": raw,
                                              "client_uuid": f"u-{i}"}, HTTP_ACCEPT="application/json")
            assert response.status_code == 201
            assert "warning" in response.json()
            note = VoiceNote.objects.get(pk=response.json()["id"])
            assert note.customer_id != foreign_customer.pk
            assert note.customer in (None, radom)

    def test_text_source_stored(self, client, salesperson):
        client.force_login(salesperson)
        client.post(self.url, {"typed_text": "x", "text_source": "dyktowanie", "client_uuid": "a"},
                    HTTP_ACCEPT="application/json")
        client.post(self.url, {"typed_text": "x", "text_source": "zlo", "client_uuid": "b"},
                    HTTP_ACCEPT="application/json")
        assert VoiceNote.objects.get(client_uuid="a").text_source == "DYKTOWANIE"
        assert VoiceNote.objects.get(client_uuid="b").text_source == ""

    def test_form_post_redirects_to_detail(self, client, salesperson):
        client.force_login(salesperson)
        response = client.post(self.url, {"typed_text": "Zadzwonić jutro."})
        note = VoiceNote.objects.get()
        assert response.status_code == 302 and response["Location"] == reverse("voice:detail", args=[note.pk])

    def test_uuid_is_scoped_per_org(self, client, salesperson, other_org_user):
        VoiceNote.objects.create(organization=other_org_user.organization, author=other_org_user,
                                 client_uuid="same", typed_text="obce")
        client.force_login(salesperson)
        response = client.post(self.url, {"typed_text": "moje", "client_uuid": "same"},
                               HTTP_ACCEPT="application/json")
        assert response.status_code == 201
        assert VoiceNote.objects.get(pk=response.json()["id"]).organization == salesperson.organization

    def test_get_not_allowed(self, client, salesperson):
        client.force_login(salesperson)
        assert client.get(self.url).status_code == 405


class TestDetailAndConfirmViews:
    def test_detail_access(self, client, org, salesperson, other_salesperson, office_user, boss, service_user,
                           other_org_user, radom):
        note = ready_note(org, salesperson, customer=radom)
        url = reverse("voice:detail", args=[note.pk])
        for user, code in [(salesperson, 200), (office_user, 200), (boss, 200), (other_salesperson, 404),
                           (service_user, 404), (other_org_user, 404)]:
            client.force_login(user)
            assert client.get(url).status_code == code, user.username
        client.force_login(salesperson)
        content = client.get(url).content.decode()
        assert "✅ Zatwierdź" in content and "📨 Przekaż biuru do weryfikacji" in content
        assert "+ dodaj zadanie" in content and "głowica konweksowa" in content
        assert "heurystyka" in content

    def test_processing_polling(self, client, org, salesperson):
        note = make_note(org, salesperson, status=VoiceStatus.PRZETWARZANIE)
        client.force_login(salesperson)
        url = reverse("voice:detail", args=[note.pk])
        content = client.get(url).content.decode()
        assert 'hx-trigger="every 2s"' in content and "✅ Zatwierdź" not in content
        partial = client.get(url + "?partial=status", HTTP_HX_REQUEST="true")
        assert "every 2s" in partial.content.decode()
        VoiceNote.objects.filter(pk=note.pk).update(status=VoiceStatus.DO_POTWIERDZENIA)
        done = client.get(url + "?partial=status", HTTP_HX_REQUEST="true")
        assert done["HX-Refresh"] == "true"

    def test_processing_stale_shows_timeout_message(self, client, org, salesperson):
        note = make_note(org, salesperson, status=VoiceStatus.NOWA)
        VoiceNote.objects.filter(pk=note.pk).update(updated_at=timezone.now() - timedelta(minutes=10))
        client.force_login(salesperson)
        url = reverse("voice:detail", args=[note.pk])
        partial = client.get(url + "?partial=status", HTTP_HX_REQUEST="true").content.decode()
        assert "dłużej niż zwykle" in partial and "every 15s" in partial

    def test_detail_keeps_matched_customer_beyond_list_and_inactive(self, client, org, salesperson):
        """Regresja: klient spoza pierwszych 1000 / nieaktywny nie znika z ekranu potwierdzenia."""
        Customer.objects.bulk_create([Customer(organization=org, name=f"A{i:04d}") for i in range(1001)])
        far = Customer.objects.create(organization=org, name="Żywiec Szpital", city="Żywiec", is_active=False)
        note = ready_note(org, salesperson, customer=far)
        client.force_login(salesperson)
        content = client.get(reverse("voice:detail", args=[note.pk])).content.decode()
        assert f'name="customer" :value="id" value="{far.pk}"' in content
        assert "Żywiec Szpital" in content and "nieaktywny" in content
        assert "<select name=\"customer\"" not in content
        data = self._form(note, far)
        client.post(reverse("voice:confirm", args=[note.pk]), data)
        note.refresh_from_db()
        assert note.status == VoiceStatus.POTWIERDZONA and note.customer_id == far.pk
        assert set(Task.objects.filter(source_ref=f"voice:{note.pk}").values_list("customer_id", flat=True)) == {far.pk}

    def test_detail_empty_states(self, client, org, salesperson):
        empty = make_note(org, salesperson, status=VoiceStatus.DO_POTWIERDZENIA, transcript="x",
                          extraction={"tasks": [], "service": []})
        client.force_login(salesperson)
        content = client.get(reverse("voice:detail", args=[empty.pk])).content.decode()
        assert "Nie rozpoznano zadań" in content
        assert 'x-show="extraService === 0">Brak spraw serwisowych.' in content
        full = ready_note(org, salesperson)
        content = client.get(reverse("voice:detail", args=[full.pk])).content.decode()
        assert "Nie rozpoznano zadań" not in content and "Brak spraw serwisowych" not in content

    def _form(self, note, customer):
        data = {"customer": customer.pk if customer else "", "task_total": len(note.extraction["tasks"]) + 3,
                "service_total": len(note.extraction["service"]) + 1, "action": "confirm"}
        for i, task in enumerate(note.extraction["tasks"]):
            data.update({f"task-{i}-title": task["title"], f"task-{i}-kind": task["kind"],
                         f"task-{i}-due": task["due_date"] or "", f"task-{i}-role": task["assignee_role"],
                         f"task-{i}-priority": task["priority"]})
        for i, item in enumerate(note.extraction["service"]):
            data.update({f"service-{i}-title": item["title"], f"service-{i}-description": item["description"],
                         f"service-{i}-device": item["device_hint"]})
            for j, part in enumerate(item["parts"]):
                data.update({f"service-{i}-part-{j}-name": part["name"], f"service-{i}-part-{j}-qty": part["quantity"]})
        return data

    def test_confirm_view_with_edits(self, client, org, salesperson, office_user, radom):
        note = ready_note(org, salesperson)
        client.force_login(salesperson)
        data = self._form(note, radom)
        n = len(note.extraction["tasks"])
        data["task-0-remove"] = "1"
        data.update({f"task-{n}-title": "Dodatkowe zadanie", f"task-{n}-kind": "INNE", f"task-{n}-due": "",
                     f"task-{n}-role": "BIURO", f"task-{n}-priority": "2"})
        data["service-0-part-1-name"] = "kabel zasilający"
        data["service-0-part-1-qty"] = "2"
        response = client.post(reverse("voice:confirm", args=[note.pk]), data)
        assert response.status_code == 302
        note.refresh_from_db()
        assert note.status == VoiceStatus.POTWIERDZONA
        titles = set(Task.objects.filter(source_ref=f"voice:{note.pk}").values_list("title", flat=True))
        assert "Dodatkowe zadanie" in titles and note.extraction["tasks"][0]["title"] in titles
        assert len(titles) == n  # jedno usunięte, jedno dodane
        assert set(PartOrder.objects.values_list("part_name", "quantity")) == {
            ("głowica konweksowa", 1), ("kabel zasilający", 2)}
        # Drugi POST nic nie tworzy.
        client.post(reverse("voice:confirm", args=[note.pk]), data)
        assert Task.objects.filter(source_ref=f"voice:{note.pk}").count() == n
        assert "Zatwierdzono" in client.get(reverse("voice:detail", args=[note.pk])).content.decode()

    def test_confirm_view_cross_org_customer(self, client, org, salesperson, foreign_customer):
        note = ready_note(org, salesperson)
        client.force_login(salesperson)
        data = self._form(note, foreign_customer)
        response = client.post(reverse("voice:confirm", args=[note.pk]), data, follow=True)
        assert "Nieznany klient" in response.content.decode()
        note.refresh_from_db()
        assert note.status == VoiceStatus.DO_POTWIERDZENIA and Task.objects.count() == 0

    def test_confirm_view_service_without_customer(self, client, org, salesperson):
        note = ready_note(org, salesperson)
        client.force_login(salesperson)
        response = client.post(reverse("voice:confirm", args=[note.pk]), self._form(note, None), follow=True)
        assert "wymagają klienta" in response.content.decode()
        assert Task.objects.count() == 0

    def test_verify_action(self, client, org, salesperson, office_user, radom):
        note = ready_note(org, salesperson)
        client.force_login(salesperson)
        data = self._form(note, radom)
        data["action"] = "verify"
        client.post(reverse("voice:confirm", args=[note.pk]), data)
        note.refresh_from_db()
        assert note.status == VoiceStatus.DO_WERYFIKACJI and note.customer == radom
        assert Task.objects.count() == 0
        # Biuro zatwierdza po weryfikacji.
        client.force_login(office_user)
        data["action"] = "confirm"
        client.post(reverse("voice:confirm", args=[note.pk]), data)
        note.refresh_from_db()
        assert note.status == VoiceStatus.POTWIERDZONA
        assert Task.objects.filter(source_ref=f"voice:{note.pk}").exists()

    def test_confirm_permissions(self, client, org, salesperson, other_salesperson, other_org_user, radom):
        note = ready_note(org, salesperson, customer=radom)
        url = reverse("voice:confirm", args=[note.pk])
        for user in (other_salesperson, other_org_user):
            client.force_login(user)
            assert client.post(url, self._form(note, radom)).status_code == 404
        client.logout()
        assert client.post(url, {}).status_code == 302
        note.refresh_from_db()
        assert note.status == VoiceStatus.DO_POTWIERDZENIA

    def test_confirm_not_ready(self, client, org, salesperson):
        note = make_note(org, salesperson, status=VoiceStatus.PRZETWARZANIE)
        client.force_login(salesperson)
        client.post(reverse("voice:confirm", args=[note.pk]), {"task_total": "0", "service_total": "0"})
        note.refresh_from_db()
        assert note.status == VoiceStatus.PRZETWARZANIE


class TestListAndAudioViews:
    def test_list_scoping(self, client, org, salesperson, other_salesperson, office_user, other_org_user):
        mine = ready_note(org, salesperson, typed_text="moja")
        theirs = make_note(org, other_salesperson, status=VoiceStatus.DO_WERYFIKACJI, typed_text="cudza")
        foreign = make_note(other_org_user.organization, other_org_user, typed_text="obca")
        url = reverse("voice:list")
        client.force_login(salesperson)
        notes = list(client.get(url).context["notes"])
        assert notes == [mine]
        client.force_login(office_user)
        notes = list(client.get(url).context["notes"])
        assert set(notes) == {mine, theirs} and foreign not in notes
        filtered = client.get(url + "?f=do_weryfikacji")
        assert list(filtered.context["notes"]) == [theirs]
        assert filtered.context["counts"]["do_weryfikacji"] == 1
        assert list(client.get(url + "?f=nonsense").context["notes"]) != []

    def test_list_login_required(self, client):
        assert client.get(reverse("voice:list")).status_code == 302

    def test_audio_gate(self, client, org, salesperson, other_salesperson, office_user, other_org_user):
        note = make_note(org, salesperson, audio=ContentFile(b"OggS-audio", "x.ogg"))
        url = reverse("voice:audio", args=[note.pk])
        client.force_login(salesperson)
        response = client.get(url)
        assert response.status_code == 200
        assert response["Content-Type"] == "audio/ogg"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert b"".join(response.streaming_content) == b"OggS-audio"
        client.force_login(office_user)
        assert client.get(url).status_code == 200
        for user in (other_salesperson, other_org_user):
            client.force_login(user)
            assert client.get(url).status_code == 404
        client.force_login(salesperson)
        no_audio = make_note(org, salesperson, typed_text="x")
        assert client.get(reverse("voice:audio", args=[no_audio.pk])).status_code == 404
        client.logout()
        assert client.get(url).status_code == 302
