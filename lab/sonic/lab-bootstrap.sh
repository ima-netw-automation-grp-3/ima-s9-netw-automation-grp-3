#!/usr/bin/env bash
# lab-bootstrap : exécuté DANS chaque routeur SONiC.
#  - au déploiement, par containerlab (directive `exec` du kind sonic-vs) ;
#  - à la demande, depuis l'interface web (bouton « Relancer le bootstrap »).
#
# Attend que SONiC soit opérationnel, rend la CONFIG_DB conforme aux modèles YANG, applique la conf
# FRR de démarrage (merge via vtysh -f), synchronise /etc/sonic/config_db.json, (re)lance le serveur
# RESTCONF, prépare sFlow (STATE_DB attendue par hsflowd) puis crée le marqueur /var/run/lab-ready.
# Idempotent : peut être relancé sans effet de bord.
set -u

FRR_STARTUP=${FRR_STARTUP:-/etc/sonic/lab/frr.conf}
READY_FLAG=/var/run/lab-ready
TIMEOUT=${LAB_BOOT_TIMEOUT:-300}
# Serveur RESTCONF : code monté par containerlab (lab/sonic/restconf), lancé par supervisord.
RESTCONF_LIB=${RESTCONF_LIB:-/usr/local/lib/sonic-lab}
RESTCONF_PROGRAM=/etc/supervisor/conf.d/restconf.conf

log() {
    echo "[lab-bootstrap] $*"
    logger -t lab-bootstrap -- "$*" 2>/dev/null || true
}

rm -f "$READY_FLAG"
waited=0

# wait_for <description> <commande...> : réessaie chaque seconde jusqu'au succès ou au délai global.
wait_for() {
    local what=$1
    shift
    until "$@" >/dev/null 2>&1; do
        if [ "$waited" -ge "$TIMEOUT" ]; then
            log "ÉCHEC : délai de ${TIMEOUT}s dépassé en attendant : $what"
            exit 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    log "ok : $what (${waited}s)"
}

# CONFIG_DB conforme aux modèles YANG de SONiC, condition de GCU (config apply-patch) et donc du
# serveur RESTCONF. L'image écrit DEVICE_METADATA|localhost switch_type = "switch", valeur absente du
# modèle sonic-device_metadata ; sans ce champ, orchagent a le même comportement (« switch »).
normalize_configdb() {
    local key="DEVICE_METADATA|localhost"
    if [ "$(sonic-db-cli CONFIG_DB HGET "$key" switch_type)" = "switch" ]; then
        sonic-db-cli CONFIG_DB HDEL "$key" switch_type >/dev/null &&
            log "CONFIG_DB : switch_type « switch » retiré (refusé par le modèle YANG)"
    fi
}

# (Re)lance le serveur RESTCONF sous supervisord : relancer le bootstrap recharge donc son code.
start_restconf() {
    if [ ! -f "$RESTCONF_LIB/restconf/__main__.py" ]; then
        log "RESTCONF : code absent ($RESTCONF_LIB/restconf), serveur non lancé"
        return 0
    fi
    cat > "$RESTCONF_PROGRAM" <<EOF
; Généré par lab-bootstrap.sh : serveur RESTCONF du lab (voir lab/sonic/restconf/).
[program:restconf]
command=/usr/bin/python3 -m restconf
directory=/
environment=PYTHONPATH="$RESTCONF_LIB",PYTHONDONTWRITEBYTECODE="1"
priority=999
autostart=true
autorestart=true
startsecs=3
startretries=20
stopasgroup=true
killasgroup=true
stdout_logfile=NONE
stdout_syslog=true
stderr_logfile=NONE
stderr_syslog=true
EOF
    supervisorctl reread >/dev/null 2>&1
    supervisorctl update >/dev/null 2>&1
    local out
    out=$(supervisorctl restart restconf 2>&1)
    if supervisorctl status restconf 2>/dev/null | grep -q RUNNING; then
        log "RESTCONF : serveur démarré (supervisorctl status restconf)"
    else
        log "avertissement : RESTCONF ne démarre pas : $out (voir le syslog du routeur)"
    fi
}

# sFlow : hsflowd est lancé et arrêté par sflowmgrd quand SFLOW|global passe à up ou à down (conf
# déployée par la CD Ansible du projet network-config). L'image SONiC-VS n'écrit pas dans STATE_DB
# deux choses qu'il attend d'un vrai SONiC :
#  - PORT_INDEX_TABLE (écrite par port_index_mapper dans le conteneur sflow) : sans elle, hsflowd écarte
#    tous les échantillons (« non-sonic port »). index = N + 1 pour EthernetN (convention SONiC, c'est
#    l'ifIndex rapporté dans les échantillons), ifindex = index de l'interface dans le noyau ;
#  - SYSTEM_READY|SYSTEM_STATE (system-health) : sans lui, hsflowd attend 5 minutes avant d'émettre.
# L'échantillonnage passe par « tc … action sample » vers psample : une action jetable sur une interface
# factice fait charger les modules psample et act_sample par le noyau de l'hôte, avant que hsflowd
# cherche la famille netlink psample.
prepare_sflow() {
    local dev port idx count=0
    if ip link add sflowprobe0 type dummy 2>/dev/null; then
        tc qdisc add dev sflowprobe0 ingress 2>/dev/null &&
            tc filter add dev sflowprobe0 parent ffff: matchall action sample rate 1000 group 1 2>/dev/null
        ip link del sflowprobe0 2>/dev/null
    fi
    for dev in /sys/class/net/Ethernet*; do
        [ -e "$dev/ifindex" ] || continue
        port=${dev##*/}
        idx=${port#Ethernet}
        case $idx in '' | *[!0-9]*) continue ;; esac
        sonic-db-cli STATE_DB HSET "PORT_INDEX_TABLE|$port" index $((idx + 1)) ifindex "$(cat "$dev/ifindex")" >/dev/null &&
            count=$((count + 1))
    done
    sonic-db-cli STATE_DB HSET "SYSTEM_READY|SYSTEM_STATE" Status UP >/dev/null
    log "sFlow : $count port(s) dans PORT_INDEX_TABLE, SYSTEM_READY posé (hsflowd)"
}

sysctl -qw net.ipv4.ip_forward=1 net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1 || true

wait_for "CONFIG_DB initialisée" bash -c '[ "$(sonic-db-cli CONFIG_DB GET CONFIG_DB_INITIALIZED)" = "1" ]'
normalize_configdb
wait_for "ports initialisés (PortInitDone)" bash -c '[ "$(sonic-db-cli APPL_DB EXISTS PORT_TABLE:PortInitDone)" = "1" ]'
wait_for "zebra joignable" vtysh -d zebra -c "show version"
wait_for "bgpd joignable" vtysh -d bgpd -c "show version"

if [ -s "$FRR_STARTUP" ]; then
    if ! check=$(vtysh -C -f "$FRR_STARTUP" 2>&1); then
        log "ERREUR : $FRR_STARTUP invalide : $check"
        exit 1
    fi
    if vtysh -f "$FRR_STARTUP" >/dev/null 2>&1; then
        log "conf FRR de démarrage appliquée ($FRR_STARTUP)"
    else
        log "ERREUR : échec de l'application de $FRR_STARTUP"
        exit 1
    fi
else
    log "pas de conf FRR de démarrage ($FRR_STARTUP absent ou vide)"
fi

config save -y >/dev/null 2>&1 || log "avertissement : 'config save -y' a échoué"

start_restconf
prepare_sflow

touch "$READY_FLAG"
log "routeur $(hostname) prêt"
