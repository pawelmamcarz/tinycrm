"""Logika modułu klientów: wyszukiwanie, oś czasu (agregowana z modułów), statystyki."""
import datetime
import inspect
import logging
from importlib import import_module

from django.conf import settings
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from .models import Customer

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 15


def search_queryset(organization, q="", user=None):
    """Aktywni klienci organizacji pasujący do `q` (nazwa, skrót, miasto, NIP); klienci `user` najpierw."""
    qs = Customer.objects.for_org(organization).filter(is_active=True)
    q = (q or "").strip()
    if q:
        cond = Q(name__icontains=q) | Q(short_name__icontains=q) | Q(city__icontains=q) | Q(nip__icontains=q)
        digits = "".join(ch for ch in q if ch.isascii() and ch.isdigit())
        if len(digits) >= 3:
            cond |= Q(nip__icontains=digits)
        qs = qs.filter(cond)
    if user is not None and getattr(user, "pk", None):
        qs = qs.annotate(
            _mine=Case(When(owner_id=user.pk, then=Value(0)), default=Value(1), output_field=IntegerField())
        ).order_by("_mine", "name")
    else:
        qs = qs.order_by("name")
    return qs


def open_tasks_count(customer):
    """Liczba otwartych zadań klienta; 0, gdy moduł zadań niedostępny."""
    try:
        from apps.tasks.models import OPEN_STATUSES
    except Exception:  # noqa: BLE001 — moduł opcjonalny
        return 0
    related = getattr(customer, "tasks", None)
    if related is None:
        return 0
    return related.filter(status__in=OPEN_STATUSES).count()


def _as_datetime(value):
    if isinstance(value, datetime.datetime):
        if timezone.is_naive(value):
            return timezone.make_aware(value)
        return value
    if isinstance(value, datetime.date):
        return timezone.make_aware(datetime.datetime.combine(value, datetime.time.min))
    return None


def _accepts_user(fn):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "user" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def collect_timeline_sources():
    sources = []
    for app in settings.INSTALLED_APPS:
        if not app.startswith("apps."):
            continue
        try:
            module = import_module(f"{app}.registrations")
        except ModuleNotFoundError as exc:
            if exc.name != f"{app}.registrations":
                logger.warning("Błąd importu %s.registrations: %s", app, exc)
            continue
        except Exception:  # noqa: BLE001 — uszkodzony moduł nie może wyłożyć karty klienta
            logger.exception("Nie udało się załadować %s.registrations", app)
            continue
        sources.extend(getattr(module, "TIMELINE", []) or [])
    return sources


def build_timeline(customer, user=None, limit=100):
    """Oś czasu klienta: wpisy ze wszystkich modułów (`TIMELINE`), malejąco po dacie.

    `user` przekazujemy każdemu źródłu (`fn(customer, user=user)`), żeby filtrowało wpisy regułami widoczności
    swojego modułu. Źródło bez parametru `user` przy podanym użytkowniku jest pomijane (fail-closed).
    """
    entries = []
    for fn in collect_timeline_sources():
        try:
            if user is not None and not _accepts_user(fn):
                logger.warning("Źródło osi czasu %r nie przyjmuje `user` — pomijam", fn)
                continue
            items = (fn(customer, user=user) if _accepts_user(fn) else fn(customer)) or []
        except Exception:  # noqa: BLE001
            logger.exception("Błąd źródła osi czasu %r", fn)
            continue
        for item in items:
            when = _as_datetime(item.get("date"))
            if when is None:
                continue
            entries.append({
                "date": when,
                "icon": item.get("icon", "•"),
                "title": item.get("title", ""),
                "text": item.get("text", ""),
                "url": item.get("url", ""),
            })
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries[:limit]


def customer_timeline(customer, user=None):
    """Źródło osi czasu tego modułu: utworzenie klienta i dodanie osób kontaktowych."""
    out = [{"date": customer.created_at, "icon": "🏥", "title": "Dodano klienta", "text": customer.name, "url": ""}]
    for contact in customer.contacts.all():
        text = contact.name + (f" — {contact.position}" if contact.position else "")
        out.append({"date": contact.created_at, "icon": "👤", "title": "Nowa osoba kontaktowa", "text": text,
                    "url": ""})
    return out


# ---------------------------------------------------------------------------------------------
# Zapis — publiczne API dla innych modułów (np. erp_sync, seed_demo)
# ---------------------------------------------------------------------------------------------

# Pola, które inne moduły mogą ustawiać przez `create_customer` / `update_customer`.
WRITABLE_FIELDS = ("name", "short_name", "nip", "city", "address", "phone", "email", "email_domain", "owner",
                   "enova_id", "notes", "is_active")


def create_customer(organization, **values):
    """Utwórz klienta w organizacji. Opiekun (`owner`) musi należeć do tej samej organizacji."""
    unknown = set(values) - set(WRITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Niedozwolone pola klienta: {', '.join(sorted(unknown))}")
    owner = values.get("owner")
    if owner is not None and owner.organization_id != organization.pk:
        raise ValueError("Opiekun spoza organizacji.")
    return Customer.objects.create(organization=organization, **values)


def update_customer(customer, values):
    """Zaktualizuj wskazane pola klienta; zapisuje tylko faktyczne zmiany. Zwraca listę zmienionych pól."""
    unknown = set(values) - set(WRITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Niedozwolone pola klienta: {', '.join(sorted(unknown))}")
    owner = values.get("owner")
    if owner is not None and owner.organization_id != customer.organization_id:
        raise ValueError("Opiekun spoza organizacji.")
    changed = []
    for name, value in values.items():
        if getattr(customer, name) != value:
            setattr(customer, name, value)
            changed.append(name)
    if changed:
        customer.save(update_fields=[*changed, "updated_at"])
    return changed
