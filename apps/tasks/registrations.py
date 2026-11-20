"""Rejestracje modułu zadań: nawigacja i oś czasu klienta."""
from apps.users.models import Role

from .services import customer_timeline

NAV = [
    {"label": "Moje zadania", "url_name": "tasks:mine", "roles": [], "order": 10, "icon": "✅"},
    {"label": "Kolejka biura", "url_name": "tasks:office", "order": 20, "icon": "📋",
     "roles": [Role.BIURO, Role.SZEF, Role.ADMIN, Role.SERWIS]},
]

TIMELINE = [customer_timeline]
