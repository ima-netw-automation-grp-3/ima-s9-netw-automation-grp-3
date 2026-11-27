# syntax=docker/dockerfile:1
# Image des jobs CI du projet network-config (CD réseau avec Ansible) : ansible-core, ansible-lint,
# pytest. Construite par docker compose (service ponctuel « ansible ») et utilisée telle quelle par le
# runner du lab (pull_policy if-not-present) : image sonic-lab-ansible:latest du .gitlab-ci.yml.
# Pas d'ENTRYPOINT : l'executor docker de GitLab lance un shell dans le conteneur du job.

FROM python:3.12-alpine AS base
# git : ansible-lint liste les fichiers du projet par git
RUN apk add --no-cache git
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
# dossiers de travail d'Ansible hors du projet, inscriptibles quel que soit l'utilisateur (make ansible)
ENV ANSIBLE_HOME=/tmp/.ansible ANSIBLE_LOCAL_TEMP=/tmp/.ansible/tmp ANSIBLE_FORCE_COLOR=1 PY_COLORS=1

# --- tests du projet modèle : docker build --target test --build-context project=gitlab/network-config gitlab/ansible
FROM base AS test
COPY --from=project . /project
WORKDIR /project
ENV ANSIBLE_CONFIG=/project/ansible.cfg
RUN ansible-lint && python3 -m pytest -q -p no:cacheprovider tests

# --- image finale -------------------------------------------------------------------------------
FROM base
