# HeArt v1 API — invented by John Davis.
# Production image. Local dev stays on SQLite; this image adds only the
# Postgres driver (psycopg[binary]) needed for the prod DATABASE_URL.
# No secrets baked in — DATABASE_URL, ADMIN_TOKEN, etc. come from the
# platform's env manager at deploy time (Render/Fly), never from a .env file.
# Digest-pinned (T2 red-pen item 8, Nov 8 2026): the 3.12-slim manifest
# digest as of 2026-11-08, so a rebuild months from now cannot silently
# pull a different base layer. Refresh with:
#   curl -s https://hub.docker.com/v2/repositories/library/python/tags/3.12-slim \
#     | python3 -c "import json,sys; print(json.load(sys.stdin)['digest'])"
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

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

# Portable backstop (T2 red-pen item 7): the container runtime can
# restart an unhealthy container on its own. Plain python + urllib
# against the database-aware /health endpoint — no extra package.
# Reads $PORT (Render/Fly inject it) and falls back to 8000.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:%s/health' % os.environ.get('PORT', '8000'))"

# Mirrors the documented local run: ./.venv/bin/uvicorn app.main:app --app-dir api
# ${PORT} lets Render/Fly inject their port; defaults to 8000.
# exec-form: uvicorn becomes PID 1 so SIGTERM on deploy shuts it down cleanly.
# --proxy-headers --forwarded-allow-ips='*' (T2 red-pen item 4): behind
# Render/Fly, request.client.host is the proxy's IP unless uvicorn trusts
# X-Forwarded-For — without this, the /signup per-IP limit (10/hour)
# becomes effectively global. Render is the only ingress.
CMD ["sh", "-c", "exec uvicorn app.main:app --app-dir /app/api --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
