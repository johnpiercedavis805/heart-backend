# HeArt v1 API — invented by John Davis.
# Production image. Local dev stays on SQLite; this image adds only the
# Postgres driver (psycopg[binary]) needed for the prod DATABASE_URL.
# No secrets baked in — DATABASE_URL, ADMIN_TOKEN, etc. come from the
# platform's env manager at deploy time (Render/Fly), never from a .env file.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]"

COPY api/ ./api/

# Canonical migration runner (idempotent: tracks applied files in
# heart_schema_migrations) plus the migrations it applies. Run in prod with:
#   DATABASE_URL='<postgres>' python scripts/apply_migrations.py --yes
COPY scripts/ ./scripts/
COPY supabase/ ./supabase/

# Writable upload dir for LocalStorage. /data is a VOLUME so uploads survive
# redeploys; a managed volume or object storage replaces this when the app
# outgrows one container.
ENV UPLOAD_DIR=/data/uploads

# Never run as root in production.
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /data/uploads \
    && chown -R app:app /data /app
VOLUME /data
USER app

EXPOSE 8000

# Mirrors the documented local run: ./.venv/bin/uvicorn app.main:app --app-dir api
# ${PORT} lets Render/Fly inject their port; defaults to 8000.
# exec-form: uvicorn becomes PID 1 so SIGTERM on deploy shuts it down cleanly.
CMD ["sh", "-c", "exec uvicorn app.main:app --app-dir /app/api --host 0.0.0.0 --port ${PORT:-8000}"]
