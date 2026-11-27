#!/usr/bin/env bash
# Suit un pipeline du projet de CI/CD réseau (network-config) depuis le terminal : le journal de chaque
# job (la sortie d'Ansible : preview, diff, PLAY RECAP…) s'affiche au fil de l'eau, comme sur la page
# du job dans GitLab, puis le bilan des jobs.
#   make ci-watch                  dernier pipeline du projet (en cours : suivi en direct ; terminé : rejoué)
#   make ci-watch NEXT=1           attend le prochain pipeline (après un git push), puis le suit en direct
#   make ci-watch REF=ma-branche   dernier pipeline de cette branche
#   make ci-watch PIPELINE=42      ce pipeline
#   make ci-watch LOGS=0           états des jobs seulement, sans leur journal
#   make ci-watch PROJECT=autre    autre projet du groupe GITLAB_GROUP
# Ctrl+C arrête le suivi, pas le pipeline. Code de sortie : 0 si le pipeline réussit, 1 sinon.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
# valeur d'une variable du fichier .env (lu sans l'exécuter), sinon la valeur par défaut
envval() { local v; v=$(grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2-); echo "${v:-$2}"; }
GITLAB=${GITLAB:-http://$(envval GITLAB_IP 172.20.30.5)}
TOKEN=${GITLAB_API_TOKEN:-$(envval GITLAB_API_TOKEN "")}
[ -n "$TOKEN" ] || { echo "GITLAB_API_TOKEN manquant : définissez-le dans .env (make init)"; exit 2; }
GROUP=${GITLAB_GROUP:-$(envval GITLAB_GROUP ima-netw-automation-grp-3)}
PROJECT=${PROJECT:-network-config}
REF=${REF:-}
PIPELINE=${PIPELINE:-}
NEXT=${NEXT:-0}
LOGS=${LOGS:-1}
POLL=${POLL:-2}

for tool in curl jq; do
    command -v "$tool" >/dev/null || { echo "$tool est requis"; exit 2; }
done
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
printf 'PRIVATE-TOKEN: %s\n' "$TOKEN" > "$WORK/headers" # le jeton ne passe pas en argument
uri() { jq -rn --arg v "$1" '$v | @uri'; }
glapi() { # chemin de l'API, puis options de curl
    local path=$1
    shift
    curl -fsS -H "@$WORK/headers" "$@" "$GITLAB/api/v4$path"
}
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

pid=$(glapi "/projects/$(uri "$GROUP/$PROJECT")" 2>/dev/null | jq -r '.id // empty')
[ -n "$pid" ] || { echo "projet $GROUP/$PROJECT introuvable sur $GITLAB (make ci-project ?)"; exit 1; }
filter=""
[ -n "$REF" ] && filter="&ref=$(uri "$REF")"
latest() { glapi "/projects/$pid/pipelines?per_page=1&order_by=id&sort=desc$filter" | jq -r '.[0].id // empty'; }

if [ -z "$PIPELINE" ]; then
    if [ "$NEXT" = 1 ]; then
        last=$(latest)
        last=${last:-0}
        echo "en attente du prochain pipeline de $GROUP/$PROJECT${REF:+ (branche $REF)} : poussez maintenant (Ctrl+C pour arrêter)"
        until PIPELINE=$(latest) && [ -n "$PIPELINE" ] && [ "$PIPELINE" -gt "$last" ]; do
            sleep "$POLL"
        done
    else
        PIPELINE=$(latest)
        [ -n "$PIPELINE" ] || { echo "aucun pipeline pour $GROUP/$PROJECT${REF:+ (branche $REF)}"; exit 1; }
    fi
fi

info=$(glapi "/projects/$pid/pipelines/$PIPELINE") || { echo "pipeline $PIPELINE introuvable"; exit 1; }
sha=$(jq -r .sha <<<"$info")
title=$(glapi "/projects/$pid/repository/commits/$sha" 2>/dev/null | jq -r '.title // ""')
bold "pipeline $PIPELINE : branche $(jq -r .ref <<<"$info"), commit ${sha:0:8} « $title »"
jq -r .web_url <<<"$info"

declare -A offset=() finished=()
final() { case $1 in success | failed | canceled | skipped) return 0 ;; *) return 1 ;; esac; }
waiting() { case $1 in created | pending | waiting_for_resource | preparing | scheduled | manual) return 0 ;; *) return 1 ;; esac; }
# Mise en forme du journal brut : horodatage et flux que GitLab met en tête de chaque ligne
# (« 2026-11-25T00:05:16.574226Z 01O »), marqueurs de sections (section_start:…:nom[options]\r\e[0K).
# L'interface de GitLab les masque aussi.
clean() {
    sed -E -e 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z [0-9a-f]{2}[OE](\+| )?//' \
        -e $'s/section_(start|end):[0-9]+:[A-Za-z0-9_.-]+(\\[[^]]*\\])?\r?\x1b\\[0K//g'
}

# Un passage : journaux des jobs dans l'ordre (id croissant = ordre des étapes) ; on s'arrête sur le
# premier job en cours (suivi jusqu'à sa fin) ou pas encore lancé.
pass() {
    local rows jid st stage name size
    rows=$(glapi "/projects/$pid/pipelines/$PIPELINE/jobs?per_page=100" | jq -r 'sort_by(.id)[] | "\(.id) \(.status) \(.stage) \(.name)"') || return 0
    while read -r jid st stage name; do
        [ -n "$jid" ] || continue
        [ -n "${finished[$jid]:-}" ] && continue
        if [ "$st" = skipped ]; then
            finished[$jid]=1
            continue
        fi
        waiting "$st" && return 0
        if [ -z "${offset[$jid]:-}" ]; then
            echo
            bold "=== job $name (étape $stage)"
            offset[$jid]=0
        fi
        if [ "$LOGS" = 1 ] && glapi "/projects/$pid/jobs/$jid/trace" >"$WORK/trace" 2>/dev/null; then
            size=$(wc -c <"$WORK/trace")
            if [ "$size" -gt "${offset[$jid]}" ]; then
                tail -c "+$((offset[$jid] + 1))" "$WORK/trace" | clean
                offset[$jid]=$size
            fi
        fi
        final "$st" || return 0
        bold "=== job $name : $st"
        finished[$jid]=1
    done <<<"$rows"
}

while :; do
    status=$(glapi "/projects/$pid/pipelines/$PIPELINE" | jq -r .status) || status=unknown
    pass
    final "$status" && break # état lu avant le passage : tous les jobs étaient déjà finis
    sleep "$POLL"
done

echo
bold "pipeline $PIPELINE : $status"
glapi "/projects/$pid/pipelines/$PIPELINE/jobs?per_page=100" |
    jq -r 'sort_by(.id)[] | "  \(.stage)\t\(.name)\t\(.status)\t\(.duration // 0 | floor) s"'
[ "$status" = success ]
