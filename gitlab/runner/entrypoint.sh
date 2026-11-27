#!/bin/bash
# Runner GitLab du lab : s'enregistre tout seul, puis lance « gitlab-runner run ».
#
#  1. attend l'API GitLab avec GITLAB_API_TOKEN (jeton de root, créé en dernier par le provisioning) ;
#  2. réutilise le jeton du runner gardé dans le volume s'il est encore valide (POST /runners/verify) ;
#     s'il est refusé (GitLab neuf), crée un runner d'instance (POST /user/runners) ;
#  3. aligne description et tags sur le .env, supprime les runners homonymes orphelins ;
#  4. régénère config.toml depuis l'environnement (executor docker, réseau de management) ;
#  5. passe la main à l'entrypoint de l'image officielle.
#
# Idempotent. Aucun jeton n'apparaît dans les logs ni dans les arguments des commandes.
set -u

GITLAB_URL=${GITLAB_URL:-http://gitlab}
CLONE_URL=${RUNNER_CLONE_URL:-$GITLAB_URL}
API="$GITLAB_URL/api/v4"
NAME=${RUNNER_NAME:-sonic-lab}
TAGS=${RUNNER_TAGS:-sonic-lab}
CONCURRENT=${RUNNER_CONCURRENT:-2}
IMAGE=${RUNNER_IMAGE:-python:3.12-alpine}
NETWORK=${RUNNER_NETWORK:-sonic-lab-mgmt}
# variables d'environnement du conteneur transmises à chaque job
JOB_ENV=${RUNNER_JOB_ENV:-RESTCONF_USER RESTCONF_PASSWORD FLOW_COLLECTOR_IP}

DATA_DIR=/etc/gitlab-runner
STATE=$DATA_DIR/sonic-lab-runner.json # id et jeton du runner (glrt-…)
CONFIG=$DATA_DIR/config.toml

log() { echo "[sonic-lab-runner] $*"; }

if [ -z "${GITLAB_API_TOKEN:-}" ]; then
    log "GITLAB_API_TOKEN est vide : enregistrement impossible"
    exit 1
fi
case $CONCURRENT in '' | *[!0-9]* | 0) CONCURRENT=2 ;; esac
# requêtes simultanées vers GitLab (long polling) : 1 retarde les jobs quand plusieurs sont en file
REQUEST_CONCURRENCY=$((CONCURRENT > 4 ? 4 : CONCURRENT))

umask 077
mkdir -p "$DATA_DIR"
WORK=$(mktemp -d)
HEADERS=$WORK/headers # PRIVATE-TOKEN, lu par curl -H @fichier
BODY=$WORK/body
printf 'PRIVATE-TOKEN: %s\n' "$GITLAB_API_TOKEN" > "$HEADERS"
HTTP_CODE=000

# api MÉTHODE CHEMIN [options curl…] : code dans $HTTP_CODE, corps dans le fichier $BODY
api() {
    local method=$1 path=$2
    shift 2
    HTTP_CODE=$(curl -sS -o "$BODY" -w '%{http_code}' --max-time 30 -X "$method" -H "@$HEADERS" "$@" "$API$path" 2>/dev/null) ||
        HTTP_CODE=000
}

wait_api() {
    local n=0
    until api GET /user && [ "$HTTP_CODE" = 200 ] && jq -e '.is_admin == true' "$BODY" >/dev/null 2>&1; do
        if [ $((n % 6)) -eq 0 ]; then
            log "en attente de l'API GitLab ($GITLAB_URL, HTTP $HTTP_CODE) : premier démarrage ou provisioning en cours"
        fi
        n=$((n + 1))
        sleep 10
    done
    log "API GitLab joignable"
}

# 0 : jeton valide ; 1 : refusé par GitLab ; 2 : réponse inattendue (on réessaie, sans réenregistrer)
token_status() {
    local token_file=$WORK/token
    jq -j .token "$STATE" > "$token_file"
    HTTP_CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -X POST \
        --data-urlencode "token@$token_file" "$API/runners/verify" 2>/dev/null) || HTTP_CODE=000
    rm -f "$token_file"
    case $HTTP_CODE in
        200) return 0 ;;
        401 | 403 | 404 | 410) return 1 ;;
        *) return 2 ;;
    esac
}

create_runner() {
    api POST /user/runners --data-urlencode "runner_type=instance_type" --data-urlencode "description=$NAME" \
        --data-urlencode "tag_list=$TAGS" --data-urlencode "run_untagged=true"
    if [ "$HTTP_CODE" != 201 ]; then
        log "ÉCHEC de la création du runner (HTTP $HTTP_CODE) : $(head -c 300 "$BODY")"
        return 1
    fi
    jq '{id, token, token_expires_at}' "$BODY" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
    log "runner d'instance « $NAME » créé (id $(jq -r .id "$STATE"), tags : $TAGS)"
}

sync_runner() {
    local id=$1 old
    api PUT "/runners/$id" --data-urlencode "description=$NAME" --data-urlencode "tag_list=$TAGS" \
        --data-urlencode "run_untagged=true" --data-urlencode "paused=false"
    [ "$HTTP_CODE" = 200 ] || log "avertissement : mise à jour du runner impossible (HTTP $HTTP_CODE)"
    api GET "/runners/all?type=instance_type&per_page=100"
    [ "$HTTP_CODE" = 200 ] || return 0
    for old in $(jq -r --arg name "$NAME" --argjson id "$id" '.[] | select(.description == $name and .id != $id) | .id' "$BODY"); do
        api DELETE "/runners/$old"
        [ "$HTTP_CODE" = 204 ] && log "ancien runner « $NAME » supprimé (id $old)"
    done
}

toml() { jq -Rn --arg v "$1" '$v'; } # chaîne TOML (échappements compatibles avec JSON)

write_config() {
    local envs="" var
    for var in $JOB_ENV; do
        [ -n "${!var+x}" ] && envs="$envs${envs:+, }$(toml "$var=${!var}")"
    done
    cat > "$CONFIG.tmp" <<EOF
# Généré à chaque démarrage par gitlab/runner/entrypoint.sh : ne pas modifier, réglez plutôt les
# variables GITLAB_RUNNER_* du .env (puis docker compose up -d gitlab-runner).
concurrent = $CONCURRENT
check_interval = 0
shutdown_timeout = 0

[session_server]
  session_timeout = 1800

[[runners]]
  name = $(toml "$NAME")
  url = $(toml "$GITLAB_URL")
  id = $(jq -r .id "$STATE")
  token = $(jq .token "$STATE")
  request_concurrency = $REQUEST_CONCURRENCY
  executor = "docker"
  clone_url = $(toml "$CLONE_URL")
  environment = [$envs]
  [runners.docker]
    image = $(toml "$IMAGE")
    network_mode = $(toml "$NETWORK")
    pull_policy = ["if-not-present"]
    privileged = false
    disable_cache = false
    volumes = ["/cache"]
    shm_size = 0
EOF
    mv "$CONFIG.tmp" "$CONFIG"
}

wait_api
while :; do
    if [ -s "$STATE" ]; then
        token_status
        case $? in
            0)
                log "runner « $NAME » déjà enregistré (id $(jq -r .id "$STATE"))"
                break
                ;;
            1)
                log "jeton du runner refusé (HTTP $HTTP_CODE) : GitLab a été réinitialisé, nouvel enregistrement"
                rm -f "$STATE"
                ;;
            *)
                log "vérification du jeton impossible (HTTP $HTTP_CODE), nouvel essai dans 10 s"
                sleep 10
                continue
                ;;
        esac
    fi
    create_runner && break
    sleep 30
done
sync_runner "$(jq -r .id "$STATE")"
write_config
rm -rf "$WORK"
log "gitlab-runner : $CONCURRENT job(s) en parallèle, image par défaut $IMAGE, réseau $NETWORK, clone par $CLONE_URL"
exec /entrypoint "$@"
