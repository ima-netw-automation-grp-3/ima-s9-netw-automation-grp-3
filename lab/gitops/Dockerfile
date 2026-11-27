# syntax=docker/dockerfile:1
# Tests de l'agent GitOps (il s'exécute dans le service clab, pas dans une image à lui). Contexte : lab/,
# pour valider aussi la vraie topologie et ses confs de démarrage :
#   docker build --target test -f lab/gitops/Dockerfile lab

FROM python:3.12-alpine AS test
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src/lab
COPY gitops/requirements-dev.txt gitops/
RUN pip install -r gitops/requirements-dev.txt
COPY sonic-lab.clab.yml ./
COPY configs/ configs/
COPY gitops/ gitops/
RUN python -m pytest -q -p no:cacheprovider gitops/tests
