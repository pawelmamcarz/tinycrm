"""Rejestracje modułu klientów: nawigacja i źródło osi czasu."""
from .services import customer_timeline

NAV = [
    {"label": "Klienci", "url_name": "customers:list", "roles": [], "order": 30, "icon": "🏥"},
]

TIMELINE = [customer_timeline]
