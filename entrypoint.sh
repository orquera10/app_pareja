#!/bin/sh
set -e

echo "==> Ensuring PostgreSQL database exists (if DATABASE_URL is set)..."
python - <<'EOF'
import os
from urllib.parse import urlparse
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

db_url = os.environ.get("DATABASE_URL", "")
if db_url.startswith("postgres"):
    parsed = urlparse(db_url)
    target_db = (parsed.path or "/postgres").lstrip("/") or "postgres"
    if target_db != "postgres":
        try:
            conn = psycopg2.connect(
                dbname="postgres",
                user=parsed.username,
                password=parsed.password,
                host=parsed.hostname,
                port=parsed.port or 5432,
                connect_timeout=5,
            )
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{target_db}"')
                print(f"Created PostgreSQL database: {target_db}")
            cur.close()
            conn.close()
        except Exception as exc:
            print(f"Database check note: {exc}")
EOF

echo "==> Running database migrations..."
python manage.py migrate --noinput

echo "==> Seeding initial Nosotros couple agenda data..."
python manage.py seed_nosotros

echo "==> Collecting static files..."
python manage.py collectstatic --noinput

echo "==> Starting Gunicorn server on port ${PORT:-8000}..."
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers 3 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
