#!/usr/bin/env bash
# Crée dans GitLab le projet de CI/CD réseau, à partir du modèle gitlab/network-config/.
#   make ci-project                      projet <GITLAB_GROUP>/network-config
#   make ci-project PROJECT=mon-projet   autre nom
# Refuse d'écraser un projet existant. Le contenu est poussé avec « ci.skip » : rien n'est déployé à
# la création ; le premier pipeline part au premier commit ou depuis CI/CD > Pipelines > Run pipeline.
# gitlab-sync exporte ensuite le projet dans gitlab/repos/ (voir gitlab/repos/README.md).
# Sert aussi à scripts/infra_project.sh : SRC (dossier à pousser), DESCRIPTION, NEXT (message final),
# MERGE_REQUIRES_PIPELINE=1 (fusion seulement si le pipeline, statuts externes compris, réussit).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
# valeur d'une variable du fichier .env (lu sans l'exécuter), sinon la valeur par défaut
envval() { local v; v=$(grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2-); echo "${v:-$2}"; }
GITLAB=${GITLAB:-http://$(envval GITLAB_IP 172.20.30.5)}
TOKEN=${GITLAB_API_TOKEN:-$(envval GITLAB_API_TOKEN "")}
[ -n "$TOKEN" ] || { echo "GITLAB_API_TOKEN manquant : définissez-le dans .env (make init)"; exit 2; }
GROUP=${GITLAB_GROUP:-$(envval GITLAB_GROUP ima-netw-automation-grp-3)}
PROJECT=${PROJECT:-network-config}
SRC=${SRC:-$ROOT/gitlab/network-config}
DESCRIPTION=${DESCRIPTION:-CI/CD réseau du lab SONiC avec Ansible, par RESTCONF (créé depuis gitlab/network-config)}
NEXT=${NEXT:-créez une branche, modifiez inventory/host_vars/<routeur>.yml, ouvrez une merge request.}

for tool in curl jq git; do
    command -v "$tool" >/dev/null || { echo "$tool est requis"; exit 2; }
done
[ -d "$SRC" ] || { echo "modèle introuvable : $SRC"; exit 2; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
printf 'PRIVATE-TOKEN: %s\n' "$TOKEN" > "$WORK/headers" # le jeton ne passe pas en argument
uri() { jq -rn --arg v "$1" '$v | @uri'; }
glapi() { # chemin de l'API, puis options de curl
    local path=$1
    shift
    curl -fsS -H "@$WORK/headers" "$@" "$GITLAB/api/v4$path"
}

if ! glapi /user | jq -e '.is_admin' >/dev/null 2>&1; then
    echo "GitLab ne répond pas avec GITLAB_API_TOKEN ($GITLAB) : make ps, docker compose logs gitlab"
    exit 1
fi
group_id=$(glapi "/groups/$(uri "$GROUP")" | jq -r '.id // empty') || true
[ -n "$group_id" ] || { echo "groupe $GROUP introuvable"; exit 1; }
if glapi "/projects/$(uri "$GROUP/$PROJECT")" >/dev/null 2>&1; then
    echo "le projet $GROUP/$PROJECT existe déjà : rien n'est écrasé (PROJECT=autre-nom make ci-project)"
    exit 1
fi

created=$(glapi /projects -X POST --data-urlencode "name=$PROJECT" --data-urlencode "path=$PROJECT" \
    --data-urlencode "namespace_id=$group_id" --data-urlencode "visibility=private" \
    --data-urlencode "description=$DESCRIPTION")
web_url=$(jq -r .web_url <<<"$created")
echo "projet créé : $GROUP/$PROJECT"
if [ "${MERGE_REQUIRES_PIPELINE:-0}" = 1 ]; then
    glapi "/projects/$(jq -r .id <<<"$created")" -X PUT --data-urlencode only_allow_merge_if_pipeline_succeeds=true \
        --data-urlencode remove_source_branch_after_merge=true >/dev/null
    echo "fusion permise seulement si le pipeline réussit"
fi

mkdir -p "$WORK/repo"
cp -a "$SRC/." "$WORK/repo/"
# produits locaux (make ansible, tests) : ignorés aussi par le .gitignore du modèle
find "$WORK/repo" \( -name __pycache__ -o -name .pytest_cache -o -name build -o -name rollback -o -name .ansible -o -name .cache \) \
    -prune -exec rm -rf {} +
git -C "$WORK/repo" init -q -b main
git -C "$WORK/repo" add -A
git -C "$WORK/repo" -c user.name="sonic-lab" -c user.email="root@sonic-lab.local" \
    commit -q -m "${COMMIT_MESSAGE:-Projet de CI/CD réseau (modèle gitlab/network-config)}"
# authentification comme gitlab-sync : en-tête HTTP passé par l'environnement, jamais dans l'URL
basic=$(printf 'root:%s' "$TOKEN" | base64 | tr -d '\n')
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="Authorization: Basic $basic" \
    GIT_TERMINAL_PROMPT=0 git -C "$WORK/repo" push -q -o ci.skip "$GITLAB/$GROUP/$PROJECT.git" main
echo "contenu poussé (branche main, sans pipeline)"
echo
echo "Projet : $web_url"
echo "Prochaine étape : $NEXT"
