"""Transkrypcja notatek głosowych (STT). Dostawca wybierany przez `settings.STT_PROVIDER`.

- `demo`       — bez zewnętrznego API: używa `note.typed_text` (dyktowanie przeglądarki / wpisany tekst),
  (przy prawdziwym dostawcy nagranie ma pierwszeństwo przed dyktowaniem — patrz `transcribe_note`),
- `elevenlabs` — ElevenLabs Scribe (`scribe_v1`, język polski),
- `openai`     — OpenAI `gpt-4o-transcribe` (język polski).

Każdy błąd dostawcy to `TranscriptionError` — pipeline łapie go i przekazuje notatkę do weryfikacji,
nigdy nie wywraca żądania ani workera.
"""
import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
HTTP_TIMEOUT = 120

DEMO_NO_TEXT_ERROR = "Brak transkrypcji w trybie demo"

# Rozszerzenie pliku → typ MIME audio (mimetypes zgaduje .webm jako video/webm).
AUDIO_EXT_MIME = {
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "mp3": "audio/mpeg",
    "mpeg": "audio/mpeg",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "amr": "audio/amr",
    "3gp": "audio/3gpp",
}


class TranscriptionError(Exception):
    """Błąd transkrypcji (brak klucza, błąd sieci/HTTP, pusta odpowiedź, tryb demo bez tekstu)."""


def audio_mime_for_name(name):
    ext = os.path.splitext(name or "")[1].lower().lstrip(".")
    return AUDIO_EXT_MIME.get(ext, "application/octet-stream")


def _read_audio(note):
    if not note.audio:
        raise TranscriptionError("Brak nagrania do transkrypcji")
    try:
        note.audio.open("rb")
        try:
            data = note.audio.read()
        finally:
            note.audio.close()
    except Exception as exc:  # noqa: BLE001 — storage może rzucić różne wyjątki
        raise TranscriptionError(f"Nie można odczytać nagrania: {exc.__class__.__name__}") from exc
    if not data:
        raise TranscriptionError("Puste nagranie")
    filename = os.path.basename(note.audio.name) or "notatka.webm"
    return filename, data, audio_mime_for_name(filename)


def _http_post(url, *, headers, data, files, timeout=HTTP_TIMEOUT):
    """POST multipart; zwraca (status_code, json|None, text). httpx, a gdy niedostępny — requests."""
    try:
        import httpx

        response = httpx.post(url, headers=headers, data=data, files=files, timeout=timeout)
    except ImportError:
        import requests

        response = requests.post(url, headers=headers, data=data, files=files, timeout=timeout)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload, (response.text or "")[:300]


def _post_and_get_text(provider, url, *, headers, data, files):
    try:
        status, payload, text = _http_post(url, headers=headers, data=data, files=files)
    except TranscriptionError:
        raise
    except Exception as exc:  # noqa: BLE001 — sieć/timeouty dowolnej biblioteki HTTP
        logger.warning("STT %s: błąd połączenia %s", provider, exc.__class__.__name__)
        raise TranscriptionError(f"{provider}: błąd połączenia ({exc.__class__.__name__})") from exc
    if status >= 400:
        logger.warning("STT %s: HTTP %s", provider, status)
        raise TranscriptionError(f"{provider}: HTTP {status}")
    transcript = (payload or {}).get("text") if isinstance(payload, dict) else None
    if not isinstance(transcript, str):
        raise TranscriptionError(f"{provider}: nieprawidłowa odpowiedź")
    return transcript.strip()


class DemoTranscriber:
    name = "demo"

    def transcribe(self, note):
        text = (note.typed_text or "").strip()
        if text:
            return text
        if note.audio:
            raise TranscriptionError(DEMO_NO_TEXT_ERROR)
        return ""


class ElevenLabsTranscriber:
    name = "elevenlabs"

    def transcribe(self, note):
        if not settings.ELEVENLABS_API_KEY:
            raise TranscriptionError("elevenlabs: brak klucza API")
        filename, data, mime = _read_audio(note)
        return _post_and_get_text(
            "elevenlabs", ELEVENLABS_URL,
            headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
            data={"model_id": "scribe_v1", "language_code": "pol"},
            files={"file": (filename, data, mime)},
        )


class OpenAITranscriber:
    name = "openai"

    def transcribe(self, note):
        if not settings.OPENAI_API_KEY:
            raise TranscriptionError("openai: brak klucza API")
        filename, data, mime = _read_audio(note)
        return _post_and_get_text(
            "openai", OPENAI_URL,
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            data={"model": "gpt-4o-transcribe", "language": "pl"},
            files={"file": (filename, data, mime)},
        )


TRANSCRIBERS = {
    "demo": DemoTranscriber,
    "elevenlabs": ElevenLabsTranscriber,
    "openai": OpenAITranscriber,
}


def get_transcriber():
    provider = (getattr(settings, "STT_PROVIDER", "demo") or "demo").lower()
    return TRANSCRIBERS.get(provider, DemoTranscriber)()


def transcribe_note(note):
    """Tekst notatki.

    - bez nagrania: `typed_text`;
    - nagranie + tekst wpisany/poprawiony ręcznie (`text_source=RECZNY`): `typed_text` (świadoma decyzja handlowca);
    - nagranie + dyktowanie przeglądarki / źródło nieznane: STT pełnego nagrania — dyktowanie na żywo bywa
      urwane (błąd sieci, koniec sesji), więc jest tylko zapasem, gdy STT zawiedzie albo zwróci pusty tekst.
    Tryb `demo` nie ma STT, więc zawsze używa `typed_text`.
    """
    text = (note.typed_text or "").strip()
    if not note.audio:
        return text or get_transcriber().transcribe(note)
    if text and getattr(note, "text_source", "") == "RECZNY":
        return text
    transcriber = get_transcriber()
    if isinstance(transcriber, DemoTranscriber):
        return transcriber.transcribe(note)
    try:
        audio_text = transcriber.transcribe(note)
    except TranscriptionError as exc:
        if text:
            logger.warning("STT notatki %s nieudane (%s) — używam tekstu z dyktowania", note.pk, exc)
            return text
        raise
    return audio_text if audio_text.strip() else text
