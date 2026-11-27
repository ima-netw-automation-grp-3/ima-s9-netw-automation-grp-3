# syntax=docker/dockerfile:1
# Synchronisation des dépôts GitLab avec gitlab/repos/ (voir gitlab_sync.py) :
# Python (bibliothèque standard seulement) + git.

FROM python:3.12-alpine AS base
RUN apk add --no-cache git
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY gitlab_sync.py .

# --- tests : docker build --target test gitlab/sync ------------------------------------------
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests/ tests/
RUN python -m pytest -q -p no:cacheprovider tests

# --- image finale -----------------------------------------------------------------------------
FROM base
ENTRYPOINT ["python3", "/app/gitlab_sync.py"]
CMD ["run"]
