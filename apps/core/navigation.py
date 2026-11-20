"""Nawigacja składana z modułów: każda app może mieć `registrations.py` z listą `NAV`.

Wpis: {"label": "Zadania", "url_name": "tasks:list", "roles": [...], "order": 20, "icon": "✅"}
Puste `roles` = widoczne dla wszystkich zalogowanych.
"""
from importlib import import_module

from django.conf import settings
from django.urls import NoReverseMatch, reverse


def collect_nav():
    items = []
    for app in settings.INSTALLED_APPS:
        if not app.startswith("apps."):
            continue
        try:
            module = import_module(f"{app}.registrations")
        except ModuleNotFoundError:
            continue
        items.extend(getattr(module, "NAV", []))
    return sorted(items, key=lambda i: i.get("order", 100))


def nav_for_user(user):
    if not user.is_authenticated:
        return []
    out = []
    for item in collect_nav():
        roles = item.get("roles") or []
        if roles and user.role not in roles:
            continue
        try:
            url = reverse(item["url_name"])
        except NoReverseMatch:
            continue
        out.append({**item, "url": url})
    return out
