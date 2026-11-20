"""Zapisuje zadania okresowe z `apps/<app>/registrations.py` (lista `PERIODIC`) do django-celery-beat.

Wpis: {"name": "notify-morning-digest", "task": "apps.notify.tasks.morning_digest",
       "crontab": {"hour": 7, "minute": 0}}  — pola crontab: minute, hour, day_of_week, day_of_month,
month_of_year (domyślnie „*”, minute domyślnie 0). Strefa: Europe/Warsaw (settings.TIME_ZONE).

Idempotentne (upsert po `name`). `--dry-run` tylko wypisuje plan. `--prune` wyłącza zadania beat z prefiksem
nazwy modułu, których nie ma już w rejestrach.
"""
from importlib import import_module

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

CRONTAB_FIELDS = ("minute", "hour", "day_of_week", "day_of_month", "month_of_year")


def collect_periodic():
    """Wszystkie wpisy PERIODIC z zainstalowanych modułów `apps.*` (z nazwą modułu źródłowego)."""
    entries = []
    for app in settings.INSTALLED_APPS:
        if not app.startswith("apps."):
            continue
        try:
            module = import_module(f"{app}.registrations")
        except ModuleNotFoundError as exc:
            if exc.name == f"{app}.registrations":
                continue
            raise
        for entry in getattr(module, "PERIODIC", []):
            entries.append({**entry, "app": app})
    return entries


def normalize_crontab(crontab):
    unknown = set(crontab) - set(CRONTAB_FIELDS)
    if unknown:
        raise CommandError(f"Nieznane pola crontab: {', '.join(sorted(unknown))}")
    values = {name: str(crontab.get(name, "*")) for name in CRONTAB_FIELDS}
    if "minute" not in crontab:
        values["minute"] = "0"
    return values


def validate(entries):
    names = set()
    for entry in entries:
        for key in ("name", "task", "crontab"):
            if not entry.get(key):
                raise CommandError(f"Wpis PERIODIC w {entry.get('app')} bez pola „{key}”: {entry}")
        if entry["name"] in names:
            raise CommandError(f"Zduplikowana nazwa zadania okresowego: {entry['name']}")
        names.add(entry["name"])
        module_path, _, func = entry["task"].rpartition(".")
        try:
            module = import_module(module_path)
        except ImportError as exc:
            raise CommandError(f"Nie można zaimportować {module_path} ({entry['name']}): {exc}") from exc
        if not hasattr(module, func):
            raise CommandError(f"Brak zadania {entry['task']} ({entry['name']})")


class Command(BaseCommand):
    help = "Synchronizuje PERIODIC z registrations.py do django-celery-beat (CrontabSchedule + PeriodicTask)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Tylko wypisz, nic nie zapisuj")
        parser.add_argument("--prune", action="store_true",
                            help="Wyłącz zadania beat zarejestrowanych modułów, których nie ma już w PERIODIC")

    def handle(self, *args, **options):
        entries = collect_periodic()
        validate(entries)
        tz = settings.TIME_ZONE
        for entry in entries:
            cron = normalize_crontab(entry["crontab"])
            cron_txt = " ".join(cron[f] for f in CRONTAB_FIELDS)
            self.stdout.write(f"{entry['name']:<32} {cron_txt:<16} {tz}  {entry['task']}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"[dry-run] {len(entries)} zadań — nic nie zapisano."))
            return

        from django_celery_beat.models import CrontabSchedule, PeriodicTask

        created = updated = 0
        with transaction.atomic():
            for entry in entries:
                cron = normalize_crontab(entry["crontab"])
                schedule, _ = CrontabSchedule.objects.get_or_create(timezone=tz, **cron)
                task, was_created = PeriodicTask.objects.update_or_create(
                    name=entry["name"],
                    defaults={"task": entry["task"], "crontab": schedule, "interval": None, "solar": None,
                              "clocked": None, "enabled": True,
                              "description": f"Z {entry['app']}.registrations (setup_periodic)"},
                )
                created += was_created
                updated += not was_created
            pruned = 0
            if options["prune"]:
                known = {e["name"] for e in entries}
                stale = PeriodicTask.objects.filter(description__endswith="(setup_periodic)", enabled=True) \
                    .exclude(name__in=known)
                pruned = stale.update(enabled=False)
        self.stdout.write(self.style.SUCCESS(
            f"Beat: utworzono {created}, zaktualizowano {updated}" + (f", wyłączono {pruned}" if options["prune"] else "")
        ))
