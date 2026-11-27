#!/bin/bash
# Configuration de base de GitLab (utilisateurs, groupe, jeton d'API), lancée par l'image après
# chaque « gitlab-ctl reconfigure » : variable GITLAB_POST_RECONFIGURE_SCRIPT de docker-compose.yml.
# Idempotente : ne crée que ce qui manque (voir provision.rb).
#
# Ne doit jamais échouer : le script d'entrée de l'image tourne en « set -e » et arrêterait GitLab.

if timeout 900 gitlab-rails runner /opt/sonic-lab/provision.rb; then
    echo "[sonic-lab] configuration de base à jour"
else
    echo "[sonic-lab] ÉCHEC de la configuration de base (code $?) : voir les messages ci-dessus"
fi
exit 0
