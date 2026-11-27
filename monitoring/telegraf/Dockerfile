# syntax=docker/dockerfile:1
# Telegraf du lab SONiC : image officielle + Python (collecteur lab_collector) + CLI docker.

FROM telegraf:1.40.3-alpine AS base
RUN apk add --no-cache python3 py3-yaml docker-cli
COPY collector/ /opt/lab-collector/
COPY telegraf.conf /etc/telegraf/telegraf.conf

# --- tests du collecteur : docker build --target test monitoring/telegraf --------------------
FROM base AS test
RUN apk add --no-cache py3-pytest
WORKDIR /opt/lab-collector
RUN python3 -m pytest -q -p no:cacheprovider tests

# --- image finale -----------------------------------------------------------------------------
FROM base
# Telegraf tourne en root, sans la bascule vers l'utilisateur « telegraf » de l'entrypoint officiel :
# le collecteur et l'entrée docker ont besoin de la socket Docker (comme le service web).
ENTRYPOINT ["telegraf"]
CMD ["--config", "/etc/telegraf/telegraf.conf"]
