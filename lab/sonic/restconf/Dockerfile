# syntax=docker/dockerfile:1
# Tests du serveur RESTCONF des routeurs (il s'exécute dans les routeurs, pas dans une image à lui).
# Python 3.11 : celui de l'image SONiC. frr_config.py et parsers.py viennent de l'interface web :
#   docker build --target test --build-context webapp=web/backend/app lab/sonic/restconf

FROM python:3.11-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RESTCONF_SHARED=/shared
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /src/sonic/restconf
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY --from=webapp frr_config.py parsers.py /shared/
COPY . .
RUN python -m pytest -q -p no:cacheprovider tests
