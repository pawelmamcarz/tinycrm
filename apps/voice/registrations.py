"""Rejestracje modułu notatek głosowych: nawigacja, oś czasu klienta, zadania okresowe."""
from .services import customer_timeline

NAV = [
    {"label": "Notatki", "url_name": "voice:list", "roles": [], "order": 15, "icon": "🎙️"},
]

TIMELINE = [customer_timeline]

PERIODIC = [
    {"name": "voice-escalate-unconfirmed", "task": "apps.voice.tasks.escalate_unconfirmed",
     "crontab": {"minute": "*/15"}},
    {"name": "voice-retry-stuck", "task": "apps.voice.tasks.retry_stuck_notes", "crontab": {"minute": "*/5"}},
    {"name": "voice-purge-audio", "task": "apps.voice.tasks.purge_old_audio", "crontab": {"hour": 3, "minute": 30}},
]
