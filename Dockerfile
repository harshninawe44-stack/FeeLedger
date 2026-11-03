# ---- Stage 1: build the React app into plain files ----
FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: the Django app ----
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /build/dist ./frontend/dist

# collectstatic needs the settings to load, but not a real secret.
RUN SECRET_KEY=build-only-not-a-real-secret python manage.py collectstatic --noinput

# Run as a normal user, not as root.
RUN useradd --system --no-create-home app && chown -R app /app
USER app

EXPOSE 8000

# On every start: update the tables, create the owner if none exists, then
# start gunicorn. $PORT is set by most hosts; 8000 is the fallback.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py create_owner && exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-3} --access-logfile - --error-logfile -"]