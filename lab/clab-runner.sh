#!/usr/bin/env bash
# Point d'entrée du service compose « clab » (image lab/clab : containerlab + Python).
#  1. charge l'image SONiC fournie (docker-sonic-vs.gz) si elle n'est pas encore dans Docker ;
#  2. vérifie la topologie (lisible, lab nommé LAB_NAME), détruit ce qui reste d'un déploiement
#     précédent, puis déploie le lab (--reconfigure : repart toujours d'un état propre) ;
#  3. lance l'agent GitOps (lab/gitops/) : il applique au lab le projet GitLab de l'infra, s'il existe ;
#  4. reste actif ; à l'arrêt du conteneur (docker compose down/stop/restart, make stop, boutons
#     « Arrêter le lab » et « Redéployer le lab » de l'interface), arrête l'agent puis détruit le lab.
#
# Démarrer ou arrêter le lab, c'est démarrer ou arrêter ce conteneur : on peut arrêter le lab, changer
# d'infra (sonic-lab.clab.yml, configs/), puis le redémarrer. La destruction ne dépend pas du fichier :
# containerlab ne retrouve que les nœuds du fichier actuel, les autres conteneurs du lab (label
# containerlab=<LAB_NAME>, ceux d'une version précédente du fichier) sont supprimés ensuite.
#
# L'état courant est écrit dans lab/.runner-status (lu par l'interface web et le healthcheck) :
#   loading-image | deploying | deployed | failed | destroying | stopped
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")" || exit 1

TOPO=${CLAB_TOPO:-sonic-lab.clab.yml}
LAB_NAME=${LAB_NAME:-sonic-lab}
SONIC_IMAGE=${SONIC_IMAGE:-docker-sonic-vs:latest}
SONIC_ARCHIVE=${SONIC_ARCHIVE:-../docker-sonic-vs.gz}
# Empreinte attendue de l'archive (GUIDE.md, « Image SONiC »), vérifiée avant « docker load » ; vide :
# pas de vérification (autre build de l'image)
SONIC_ARCHIVE_SHA256=${SONIC_ARCHIVE_SHA256-}
STATUS_FILE=.runner-status
AGENT_PID=""
CHILD=""   # groupe de processus de la commande longue en cours (voir run)

set_status() {
    echo "$1" > "$STATUS_FILE"
    echo "[runner] $(date '+%F %T') état : $1${2:+ ($2)}"
}

# Commande longue (docker load, containerlab deploy, attente) en arrière-plan, dans son propre groupe
# de processus : un signal interrompt `wait` tout de suite (bash diffère un trap tant qu'une commande
# tourne au premier plan, jusqu'à 900 s de bootstrap par nœud pour containerlab deploy), et le trap
# peut l'arrêter avec ses enfants. Commandes externes seulement (pas de fonction shell).
run() {
    setsid "$@" &
    CHILD=$!
    wait "$CHILD"
    local rc=$?
    CHILD=""
    return "$rc"
}

# TERM à tout un groupe de processus, puis KILL s'il en reste après $2 secondes.
kill_group() {
    local pgid=$1 delay=$2
    kill -TERM -- "-$pgid" 2>/dev/null || return 0
    for _ in $(seq 1 "$delay"); do
        kill -0 -- "-$pgid" 2>/dev/null || return 0
        sleep 1
    done
    kill -KILL -- "-$pgid" 2>/dev/null
    return 0
}

stop_child() {
    local pgid=$CHILD
    [ -n "$pgid" ] || return 0
    CHILD=""
    kill_group "$pgid" 30
    wait "$pgid" 2>/dev/null
    return 0
}

# Agent GitOps dans son propre groupe de processus : son arrêt emporte un containerlab en cours.
start_agent() {
    if [ "${GITOPS_ENABLED:-1}" = 0 ] || ! command -v python3 >/dev/null; then
        echo "[runner] agent GitOps désactivé"
        return
    fi
    LAB_DIR=$PWD PYTHONPATH=$PWD setsid python3 -m gitops &
    AGENT_PID=$!
}

# Attend tout le groupe, pas seulement Python : un containerlab lancé par l'agent lui survivrait.
stop_agent() {
    [ -n "$AGENT_PID" ] || return 0
    kill_group "$AGENT_PID" 30
    AGENT_PID=""
}

# Conteneurs du lab encore présents (label posé par containerlab) : nœuds d'une autre version de la
# topologie, que containerlab ne retrouve plus dans le fichier, ou restes d'un arrêt brutal.
purge_lab() {
    local ids names n
    ids=$(docker ps -aq --filter "label=containerlab=$LAB_NAME")
    [ -n "$ids" ] || return 0
    # shellcheck disable=SC2086  # une liste d'identifiants
    names=$(docker inspect -f '{{.Name}}' $ids 2>/dev/null | sed 's#^/##' | tr '\n' ' ')
    echo "[runner] conteneurs du lab hors de $TOPO, supprimés : $names"
    # shellcheck disable=SC2086
    docker rm -f $ids >/dev/null
    for n in $names; do
        rm -f "/var/run/netns/$n"
    done
    rm -rf "clab-$LAB_NAME"
    [ -z "$(docker ps -aq --filter "label=containerlab=$LAB_NAME")" ]
}

# containerlab détruit les nœuds du fichier actuel (liens, dossier clab-<lab>, /etc/hosts), la purge
# le reste. Le réseau de management appartient à docker compose (GitLab y est attaché) : gardé.
destroy_lab() {
    containerlab destroy -t "$TOPO" --cleanup --keep-mgmt-net || true
    purge_lab
}

on_term() {
    # Trap sans effet, et non « trap - » : bash n'est pas le PID 1 du conteneur (pid: host), un second
    # TERM (make down pendant un arrêt depuis l'interface) le tuerait au milieu du destroy.
    trap 'echo "[runner] arrêt déjà en cours"' TERM INT
    set_status destroying
    stop_child
    stop_agent
    echo destroying > "$STATUS_FILE"   # l'agent a pu réécrire l'état avant de s'arrêter
    destroy_lab || echo "[runner] des conteneurs du lab n'ont pas pu être supprimés"
    set_status stopped
    exit 0
}
trap on_term TERM INT

# Attente interruptible : le trap s'exécute dès la réception du signal.
idle() {
    while true; do
        run sleep 3600
    done
}

# Topologie lisible, et lab au nom attendu : l'interface, la métrologie et l'agent GitOps cherchent le
# lab sous LAB_NAME ; sous un autre nom, il serait invisible et jamais détruit par purge_lab.
check_topology() {
    command -v python3 >/dev/null || return 0
    python3 - "$TOPO" "$LAB_NAME" <<'EOF'
import sys

import yaml

path, lab = sys.argv[1:3]
try:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
    sys.exit(f"{path} illisible : {exc}")
topology = data.get("topology") if isinstance(data, dict) else None
if not isinstance(topology, dict) or not isinstance(topology.get("nodes"), dict):
    sys.exit(f"{path} : aucun nœud (topology.nodes)")
if data.get("name") != lab:
    sys.exit(f"{path} : « name: {data.get('name')} », le lab doit s'appeler « {lab} » (LAB_NAME)")
EOF
}

if ! docker image inspect "$SONIC_IMAGE" >/dev/null 2>&1; then
    set_status loading-image "$SONIC_ARCHIVE"
    if [ ! -f "$SONIC_ARCHIVE" ]; then
        set_status failed "archive $SONIC_ARCHIVE introuvable"
        idle
    fi
    if [ -n "$SONIC_ARCHIVE_SHA256" ]; then
        sum=$(sha256sum "$SONIC_ARCHIVE" | cut -d' ' -f1)
        if [ "$sum" != "$SONIC_ARCHIVE_SHA256" ]; then
            set_status failed "empreinte de $SONIC_ARCHIVE inattendue ($sum) : autre build ? SONIC_ARCHIVE_SHA256 dans .env"
            idle
        fi
    fi
    if ! run docker load -i "$SONIC_ARCHIVE"; then
        set_status failed "docker load a échoué"
        idle
    fi
fi

set_status deploying "$TOPO"
if ! problem=$(check_topology 2>&1); then
    set_status failed "topologie refusée : $problem"
elif ! destroy_lab; then
    set_status failed "conteneurs d'un déploiement précédent impossibles à supprimer"
elif run containerlab deploy -t "$TOPO" --reconfigure; then
    set_status deployed
else
    set_status failed "containerlab deploy a échoué (voir les logs ci-dessus)"
fi

start_agent
idle
