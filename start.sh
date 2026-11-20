#!/bin/sh
# Start kontenera (Railway): migracje, opcjonalny seed demo, gunicorn.
set -e
python manage.py migrate --noinput
if [ -n "$DEMO_PASSWORD" ]; then
  python manage.py seed_demo --password "$DEMO_PASSWORD"
fi
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers 1 --threads "${GUNICORN_THREADS:-8}" --worker-class gthread \
  --timeout 120 --access-logfile -
