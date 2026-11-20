"""Rejestracje modułu integracji: nawigacja i zadanie okresowe (import z enova365)."""

NAV = [
    {"label": "Integracje", "url_name": "erp_sync:status", "roles": ["SZEF", "ADMIN", "BIURO"], "order": 80,
     "icon": "🔄"},
]

PERIODIC = [
    {"name": "erp-enova-daily", "task": "apps.erp_sync.tasks.daily_enova_sync", "crontab": {"hour": 5, "minute": 0}},
]
