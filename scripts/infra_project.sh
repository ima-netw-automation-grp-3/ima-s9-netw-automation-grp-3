#!/usr/bin/env bash
# Crée dans GitLab le projet de l'infra en GitOps, à partir de l'état actuel du lab :
#   lab/sonic-lab.clab.yml, lab/configs/<nœud>/{config_db.json,frr.conf}, le modèle gitlab/lab-infra/
#   et une copie du validateur (lab/gitops/validate.py → ci/validate.py).
#   make infra-project                       projet GITOPS_PROJECT, ou <GITLAB_GROUP>/lab-infra
# Le contenu étant celui du lab, l'agent GitOps l'adopte sans redéployer. Refuse d'écraser un projet.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
envval() { local v; v=$(grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2-); echo "${v:-$2}"; }
GROUP=${GITLAB_GROUP:-$(envval GITLAB_GROUP ima-netw-automation-grp-3)}
TARGET=${GITOPS_PROJECT:-$(envval GITOPS_PROJECT "$GROUP/lab-infra")}

SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT
cp "$ROOT/lab/sonic-lab.clab.yml" "$SRC/"
for dir in "$ROOT"/lab/configs/*/; do
    node=$(basename "$dir")
    for f in config_db.json frr.conf; do
        if [ -f "$dir/$f" ]; then
            mkdir -p "$SRC/configs/$node"
            cp "$dir/$f" "$SRC/configs/$node/"
        fi
    done
done
cp -a "$ROOT/gitlab/lab-infra/." "$SRC/"
mkdir -p "$SRC/ci"
cp "$ROOT/lab/gitops/validate.py" "$SRC/ci/validate.py"

GITLAB_GROUP=${TARGET%/*} PROJECT=${TARGET##*/} SRC=$SRC MERGE_REQUIRES_PIPELINE=1 \
    DESCRIPTION="Infra du lab SONiC en GitOps : topologie containerlab et confs de démarrage (make infra-project)" \
    COMMIT_MESSAGE="Infra du lab : état actuel (topologie et confs de démarrage)" \
    NEXT="l'agent GitOps adopte ce commit (statut lab/deploy) ; modifiez l'infra par merge request." \
    "$ROOT/scripts/ci_project.sh"
