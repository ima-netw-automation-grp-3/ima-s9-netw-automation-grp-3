# syntax=docker/dockerfile:1
# Interface web du lab SONiC : SPA React (build Node) servie par FastAPI (Python).

# --- 1. build de l'interface React -------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- 2. dépendances Python -----------------------------------------------------------
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt

# --- tests du backend : docker build --target test web/ -----------------------------
FROM base AS test
COPY backend/requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY backend/ ./
RUN python -m pytest -q

# --- 3. image finale -----------------------------------------------------------------
FROM base
COPY backend/app ./app
COPY --from=ui /ui/dist ./static
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
