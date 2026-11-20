"""Rejestracje modułu powiadomień: nawigacja i zadania okresowe."""

NAV = [
    {"label": "Powiadomienia", "url_name": "notify:list", "order": 90, "icon": "🔔", "roles": []},
]

PERIODIC = [
    {"name": "notify-morning-digest", "task": "apps.notify.tasks.morning_digest",
     "crontab": {"hour": 7, "minute": 0}},
    {"name": "notify-overdue-escalation", "task": "apps.notify.tasks.overdue_escalation",
     "crontab": {"hour": 9, "minute": 0}},
]
