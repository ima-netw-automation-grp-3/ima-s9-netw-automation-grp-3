#!/bin/sh
# Exécuté une seule fois, à l'initialisation d'InfluxDB (volume vide), après « influx setup ».
# Crée le mapping InfluxQL « <bucket>/autogen » -> bucket, utilisé par la datasource InfluxQL de
# Grafana (InfluxDB 2.x n'accepte les requêtes InfluxQL que sur un bucket mappé).
if influx v1 dbrp create \
    --bucket-id "${DOCKER_INFLUXDB_INIT_BUCKET_ID}" \
    --db "${DOCKER_INFLUXDB_INIT_BUCKET}" \
    --rp autogen \
    --default \
    --org "${DOCKER_INFLUXDB_INIT_ORG}"; then
    echo "[init] mapping InfluxQL ${DOCKER_INFLUXDB_INIT_BUCKET}/autogen créé"
else
    echo "[init] AVERTISSEMENT : création du mapping InfluxQL impossible" >&2
fi
