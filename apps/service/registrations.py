"""Rejestracje modułu serwisu: nawigacja, oś czasu klienta, zadania okresowe."""
from apps.users.models import Role

from .services import customer_timeline

NAV = [
    {"label": "Serwis", "url_name": "service:dashboard", "order": 40, "icon": "🔧",
     "roles": [Role.SERWIS, Role.BIURO, Role.SZEF, Role.ADMIN]},
]

TIMELINE = [customer_timeline]

PERIODIC = [
    {"name": "service-inspection-planning", "task": "apps.service.tasks.daily_inspection_planning",
     "crontab": {"hour": 6, "minute": 15}},
    {"name": "service-warranty-alerts", "task": "apps.service.tasks.warranty_expiring_alerts",
     "crontab": {"hour": 6, "minute": 30}},
]
