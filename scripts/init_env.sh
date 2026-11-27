#!/usr/bin/env bash
# make init : crée .env à partir de .env.example et remplit les secrets laissés vides (mots de passe,
# jetons) avec des valeurs aléatoires. Ne modifie jamais un .env existant.
#
# Les valeurs générées restent dans .env (droits 600) : elles ne s'affichent pas ici.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE=$ROOT/.env
EXAMPLE=$ROOT/.env.example

if [ -e "$ENV_FILE" ]; then
    echo ".env existe déjà : rien n'est modifié (supprimez-le pour en générer un nouveau)."
    exit 0
fi
command -v openssl >/dev/null || { echo "openssl est requis pour générer les secrets" >&2; exit 1; }

# $1 octets aléatoires en hexadécimal (2 × $1 caractères : ni « root », ni « admin », ni « gitlab »)
rand() { openssl rand -hex "$1"; }

tmp=$(mktemp "$ROOT/.env.XXXXXX")
trap 'rm -f "$tmp"' EXIT
sed -e "s|^GRAFANA_ADMIN_PASSWORD=\$|GRAFANA_ADMIN_PASSWORD=$(rand 12)|" \
    -e "s|^INFLUXDB_ADMIN_PASSWORD=\$|INFLUXDB_ADMIN_PASSWORD=$(rand 12)|" \
    -e "s|^INFLUXDB_TOKEN=\$|INFLUXDB_TOKEN=$(rand 32)|" \
    -e "s|^GITLAB_ROOT_PASSWORD=\$|GITLAB_ROOT_PASSWORD=$(rand 12)|" \
    -e "s|^GITLAB_USERS=\$|GITLAB_USERS=lab:$(rand 8)|" \
    -e "s|^GITLAB_API_TOKEN=\$|GITLAB_API_TOKEN=$(rand 10)|" \
    -e "s|^RESTCONF_PASSWORD=\$|RESTCONF_PASSWORD=$(rand 12)|" \
    "$EXAMPLE" > "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"
trap - EXIT

echo ".env créé : mots de passe et jetons générés (Grafana, InfluxDB, GitLab, RESTCONF)."
echo "Compte GitLab « lab » (Owner du groupe) : son mot de passe est dans GITLAB_USERS du .env."
echo "Relisez .env (adresses, ports, comptes), puis : make up"
