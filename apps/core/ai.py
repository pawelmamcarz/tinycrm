"""Kontrakt AI (wzorzec z Pyrlandii): strict JSON, fallback, `{"_error": ...}` zamiast wyjątku.

Caller ZAWSZE sprawdza klucz `_error`. Bez ANTHROPIC_API_KEY zwracamy `{"_error": "no_api_key"}`,
a moduły mają własny fallback (np. heurystyka w trybie demo).
"""
import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_response(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    match = _JSON_BLOCK.search(text or "")
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            pass
    return {"_error": "invalid_json", "_raw": (text or "")[:500]}


def ask_json(system, prompt, max_tokens=2000):
    """Wyślij prompt do Claude i zwróć dict z JSON. Nigdy nie rzuca wyjątku."""
    if not settings.ANTHROPIC_API_KEY:
        return {"_error": "no_api_key"}
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system + "\n\nOdpowiadaj WYŁĄCZNIE poprawnym JSON, bez komentarzy.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(block, "text", "") for block in message.content)
        return parse_json_response(text)
    except Exception as exc:  # noqa: BLE001 — kontrakt: błąd jako dane, nie wyjątek
        logger.warning("AI error: %s", exc)
        return {"_error": f"api_error: {exc.__class__.__name__}"}
