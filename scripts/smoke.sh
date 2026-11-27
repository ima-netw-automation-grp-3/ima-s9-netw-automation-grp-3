#!/usr/bin/env bash
# Test de bout en bout du lab déployé, à travers l'API de l'interface web, le RESTCONF des routeurs,
# InfluxDB, Grafana, GitLab et son runner.
#   make smoke            (le lab doit être démarré : make up)
#   API=http://hote:8080 SMOKE_AUTH=user:motdepasse make smoke
#   INFLUX=http://hote:8086 GRAFANA=http://hote:3000 INFLUXDB_TOKEN=… make smoke
#   GITLAB=http://172.20.30.5 GITLAB_API_TOKEN=… make smoke
#   RESTCONF_USER=… RESTCONF_PASSWORD=… make smoke
# Les valeurs absentes de l'environnement sont lues dans .env (dont WEB_AUTH_* pour SMOKE_AUTH).
# Ne laisse aucune trace : sonic1 est remis dans l'état de son snapshot de départ (sa running, même
# non sauvegardée), le snapshot de test est supprimé et les fichiers de lab/configs/ sont vérifiés
# inchangés ; les projets GitLab de test sont supprimés définitivement, leurs fichiers de
# gitlab/repos/ aussi.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
# valeur d'une variable du fichier .env (lu sans l'exécuter), sinon la valeur par défaut
envval() { local v; v=$(grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2-); echo "${v:-$2}"; }
API=${API:-http://127.0.0.1:${WEB_PORT:-$(envval WEB_PORT 8080)}}
INFLUX=${INFLUX:-http://127.0.0.1:$(envval INFLUXDB_PORT 8086)}
GRAFANA=${GRAFANA:-http://127.0.0.1:$(envval GRAFANA_PORT 3000)}
TOKEN=${INFLUXDB_TOKEN:-$(envval INFLUXDB_TOKEN "")}
GITLAB=${GITLAB:-http://$(envval GITLAB_IP 172.20.30.5)}
GITLAB_TOKEN=${GITLAB_API_TOKEN:-$(envval GITLAB_API_TOKEN "")}
GITLAB_GROUP=${GITLAB_GROUP:-$(envval GITLAB_GROUP ima-netw-automation-grp-3)}
GITLAB_USERS=${GITLAB_USERS:-$(envval GITLAB_USERS "")}
GITOPS_PROJECT=${GITOPS_PROJECT:-$(envval GITOPS_PROJECT "$GITLAB_GROUP/lab-infra")}
RESTCONF=${RESTCONF:-https://172.20.30.11/restconf} # sonic1
RC_AUTH=(-u "${RESTCONF_USER:-$(envval RESTCONF_USER admin)}:${RESTCONF_PASSWORD:-$(envval RESTCONF_PASSWORD "")}")
if [ -z "${SMOKE_AUTH:-}" ] && [ -n "$(envval WEB_AUTH_USER "")" ] && [ -n "$(envval WEB_AUTH_PASSWORD "")" ]; then
    SMOKE_AUTH="$(envval WEB_AUTH_USER ""):$(envval WEB_AUTH_PASSWORD "")"
fi
AUTH=()
[ -n "${SMOKE_AUTH:-}" ] && AUTH=(-u "$SMOKE_AUTH")
pass=0
fail=0

ok() { echo "  ✔ $*"; pass=$((pass + 1)); }
ko() { echo "  ✘ $*"; fail=$((fail + 1)); }
step() { printf '\n== %s\n' "$*"; }
get() { curl -fsS "${AUTH[@]}" "$API$1"; }
post() {
    local body='{}'
    [ $# -ge 2 ] && body=$2
    curl -fsS "${AUTH[@]}" -X POST -H 'content-type: application/json' -d "$body" "$API$1"
}
status_of() { # code HTTP d'un POST
    curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X POST -H 'content-type: application/json' -d "$2" "$API$1"
}
wait_until() { # délai description commande...
    local timeout=$1 desc=$2 t=0
    shift 2
    until "$@" >/dev/null 2>&1; do
        sleep 2
        t=$((t + 2))
        if [ "$t" -ge "$timeout" ]; then
            ko "$desc (délai de ${timeout}s dépassé)"
            return 1
        fi
    done
    ok "$desc (${t}s)"
}

# requête InfluxQL ; la première valeur de la première série, ou null
influx_q() {
    curl -fsS -G "$INFLUX/query" -H "Authorization: Token $TOKEN" --data-urlencode "db=lab" --data-urlencode "q=$1"
}
first_value() { influx_q "$1" | jq '.results[0].series[0].values[0][1] // null'; }

lab_ready() { get /api/lab | jq -e '[.nodes[].phase] | length > 0 and all(. == "ready")'; }
metrics_nodes() { # tous les nœuds du lab relevés dans la dernière minute
    influx_q 'SELECT last("collected") FROM "lab_node" WHERE time > now() - 1m GROUP BY "node"' |
        jq -e '[.results[0].series[]? | select(.values[0][1] == 1)] | length >= 4'
}
metrics_probe_ok() {
    [ "$(first_value "SELECT last(\"reachable\") FROM \"lab_probe\" WHERE \"src\" = 'client1' AND \"dst_ip\" = '192.168.2.10' AND time > now() - 30s")" = 1 ]
}
metrics_bgp() { # $1 = 1 (établie) ou 0, vu par sonic1 dans les 20 dernières secondes
    [ "$(first_value "SELECT last(\"established\") FROM \"lab_bgp_peer\" WHERE \"node\" = 'sonic1' AND time > now() - 20s")" = "$1" ]
}
metrics_bgp_event() {
    [ "$(first_value "SELECT count(\"message\") FROM \"lab_event\" WHERE \"type\" = 'bgp' AND \"node\" = 'sonic1' AND time > now() - 3m")" != null ]
}
compose() { (cd "$ROOT" && PWD="$ROOT" docker compose "$@"); }
uri() { jq -rn --arg v "$1" '$v | @uri'; }
glapi() { # chemin de l'API GitLab, puis options de curl
    local path=$1
    shift
    curl -fsS -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$@" "$GITLAB/api/v4$path"
}
gitlab_ready() { glapi /user | jq -e .is_admin; }
smoke_files_gone() { [ ! -e "$SMOKE_FILE.json" ] && [ ! -e "$SMOKE_FILE.bundle" ]; }
delete_project() { # suppression définitive d'un projet GitLab (id) : DELETE, puis purge du projet renommé
    local id=$1 renamed
    glapi "/projects/$id" -X DELETE >/dev/null || return 1
    renamed=$(glapi "/projects/$id" | jq -r .path_with_namespace) # suppression différée : renommé
    glapi "/projects/$id" -X DELETE -G --data-urlencode permanently_remove=true \
        --data-urlencode "full_path=$renamed" >/dev/null
}

# RESTCONF de sonic1 : rc MÉTHODE CHEMIN [CORPS] (corps de la réponse sur stdout), rc_code idem (code HTTP)
rc() {
    local data=()
    [ $# -ge 3 ] && data=(-H 'Content-Type: application/yang-data+json' --data-binary "$3")
    curl -sk "${RC_AUTH[@]}" -X "$1" -H 'Accept: application/yang-data+json' "${data[@]}" "$RESTCONF$2"
}
rc_code() {
    local data=()
    [ $# -ge 3 ] && data=(-H 'Content-Type: application/yang-data+json' --data-binary "$3")
    curl -sk -o /dev/null -w '%{http_code}' "${RC_AUTH[@]}" -X "$1" "${data[@]}" "$RESTCONF$2"
}
rc_bgp_up() { rc GET "/data/sonic-lab:frr/state/bgp-neighbor=10.0.12.2/state" | jq -e '."sonic-lab:state" == "Established"'; }
runner_online() {
    glapi "/runners/all?type=instance_type&per_page=100" | jq -e 'any(.[]; .description == "sonic-lab" and .status == "online")'
}

bgp_up() { get /api/lab | jq -e '.bgp_sessions | length > 0 and all(.state == "Established")'; }
bgp_down() { get /api/lab | jq -e '.bgp_sessions | all(.state != "Established")'; }
ping_ok() { post /api/nodes/client1/ping '{"target":"192.168.2.10","count":2}' | jq -e '.summary.ok'; }
ping_ko() { post /api/nodes/client1/ping '{"target":"192.168.2.10","count":2}' | jq -e '.summary.ok | not'; }

command -v jq >/dev/null || { echo "jq est requis"; exit 2; }

step "API et état du lab"
if get /api/health >/dev/null; then ok "API joignable ($API)"; else ko "API injoignable ($API)"; exit 1; fi
wait_until 300 "les 4 nœuds sont prêts" lab_ready
wait_until 120 "session eBGP sonic1 ↔ sonic2 établie" bgp_up
wait_until 30 "ping client1 → client2 (192.168.2.10)" ping_ok

step "Métrologie : InfluxDB, Grafana, collecte"
if curl -fsS "$INFLUX/health" | jq -e '.status == "pass"' >/dev/null; then ok "InfluxDB répond ($INFLUX)"; else ko "InfluxDB injoignable ($INFLUX)"; fi
if curl -fsS "$GRAFANA/api/health" | jq -e '.database == "ok"' >/dev/null; then ok "Grafana répond ($GRAFANA)"; else ko "Grafana injoignable ($GRAFANA)"; fi
dashboards=$(curl -fsS "$GRAFANA/api/search?type=dash-db&folderUIDs=sonic-lab" | jq -r '[.[].uid] | sort | join(" ")')
if [ "$dashboards" = "lab-client lab-flows lab-infra lab-interfaces lab-overview lab-probes lab-router" ]; then
    ok "7 tableaux de bord provisionnés"
else
    ko "tableaux de bord provisionnés : « $dashboards »"
fi
if get /api/monitoring | jq -e '.grafana.available' >/dev/null; then ok "l'interface voit Grafana (/api/monitoring)"; else ko "/api/monitoring : Grafana indisponible"; fi
wait_until 90 "les 4 nœuds sont relevés dans InfluxDB" metrics_nodes
wait_until 60 "sonde client1 → 192.168.2.10 joignable dans InfluxDB" metrics_probe_ok
wait_until 60 "session BGP de sonic1 établie dans InfluxDB" metrics_bgp 1

step "Snapshot de sonic1"
SNAP=$(post /api/nodes/sonic1/snapshots '{"label":"smoke"}' | jq -r '.id // empty')
if [ -n "$SNAP" ]; then ok "snapshot $SNAP créé"; else ko "création du snapshot"; fi

step "Push CONFIG_DB (merge) : shutdown d'Ethernet4 sur sonic1"
if post /api/nodes/sonic1/config/push \
    '{"domain":"configdb","mode":"merge","content":"{\"PORT\":{\"Ethernet4\":{\"admin_status\":\"down\"}}}"}' |
    jq -e '.ok and .applied == 1' >/dev/null; then
    ok "push appliqué"
else
    ko "push CONFIG_DB"
fi
wait_until 60 "la session BGP tombe" bgp_down
wait_until 30 "le ping client1 → client2 échoue" ping_ko
wait_until 60 "la métrologie voit la session BGP tomber" metrics_bgp 0
wait_until 30 "événement BGP enregistré (lab_event)" metrics_bgp_event
if get /api/nodes/sonic1/config/diff | jq -e '.dirty and .configdb.changes[0].key == "Ethernet4"' >/dev/null; then
    ok "diff running/startup : modification détectée"
else
    ko "diff running/startup"
fi

step "Restauration du snapshot de départ (replace)"
# le snapshot pris avant le test, et non la conf de démarrage : une running non sauvegardée (CD
# Ansible, essais) est remise telle quelle
RESTORE_SRC=startup
[ -n "${SNAP:-}" ] && RESTORE_SRC="snapshot:$SNAP"
if post /api/nodes/sonic1/config/restore "{\"source\":\"$RESTORE_SRC\"}" | jq -e .ok >/dev/null; then
    ok "restauration ($RESTORE_SRC)"
else
    ko "restauration ($RESTORE_SRC)"
fi
wait_until 120 "la session BGP est rétablie" bgp_up
wait_until 30 "le ping client1 → client2 est rétabli" ping_ok
wait_until 60 "la métrologie voit la session BGP rétablie" metrics_bgp 1
if get "/api/nodes/sonic1/config/diff?against=$RESTORE_SRC" | jq -e '.dirty | not' >/dev/null; then
    ok "running = $RESTORE_SRC"
else
    ko "différences avec $RESTORE_SRC après restauration"
fi

step "FRR : aperçu replace et validation"
BODY=$(jq -n --rawfile c "$ROOT/lab/configs/sonic1/frr.conf" \
    '{domain: "frr", mode: "replace", content: ($c | sub("  network 192.168.1.0/24\n"; ""))}')
if post /api/nodes/sonic1/config/preview "$BODY" | jq -e '.valid and .replace.resets == ["router bgp 65001"]' >/dev/null; then
    ok "aperçu replace : le bloc router bgp serait recréé (rien n'est poussé)"
else
    ko "aperçu replace FRR"
fi
code=$(status_of /api/nodes/sonic1/config/push '{"domain":"frr","mode":"merge","content":"router bgp 65001\n neighbor foo bar\n"}')
if [ "$code" = "400" ]; then ok "conf FRR invalide refusée (HTTP 400)"; else ko "conf FRR invalide : HTTP $code au lieu de 400"; fi

step "Sauvegarde running → startup (les fichiers doivent rester identiques)"
if get /api/nodes/sonic1/config/diff | jq -e '.dirty' >/dev/null; then
    # sauvegarder écrirait la running dans la conf de démarrage (ou ouvrirait une merge request en GitOps)
    echo "  – la running de sonic1 diffère de sa conf de démarrage : sauvegarde non testée"
else
    BACKUP=$(mktemp -d)
    cp "$ROOT"/lab/configs/sonic1/* "$BACKUP"/
    SAVE=$(post /api/nodes/sonic1/config/save)
    if jq -e '.ok and ((.gitops // false) == false or .changed == false)' <<<"$SAVE" >/dev/null; then
        ok "sauvegarde$(jq -r 'if .gitops then " (GitOps : Git déjà à jour, aucune merge request)" else "" end' <<<"$SAVE")"
    else
        ko "sauvegarde : $(jq -c '{ok, gitops, changed, merge_request}' <<<"$SAVE" 2>/dev/null)"
    fi
    if diff -r "$BACKUP" "$ROOT/lab/configs/sonic1" >/dev/null; then
        ok "lab/configs/sonic1 inchangé"
    else
        ko "la sauvegarde a modifié lab/configs/sonic1 (différences ci-dessous, originaux restaurés)"
        diff -r "$BACKUP" "$ROOT/lab/configs/sonic1" | head -40
        cp "$BACKUP"/* "$ROOT/lab/configs/sonic1/"
    fi
    rm -r -- "$BACKUP"
fi

step "Terminal web (WebSocket → docker exec)"
if (cd "$ROOT" && PWD="$ROOT" docker compose exec -T -e SMOKE_AUTH="${SMOKE_AUTH:-}" web python - <<'PY'
import asyncio, base64, json, os, sys
import websockets

async def main():
    headers = {}
    if os.environ.get("SMOKE_AUTH"):
        headers["Authorization"] = "Basic " + base64.b64encode(os.environ["SMOKE_AUTH"].encode()).decode()
    url = "ws://127.0.0.1:8080/ws/nodes/sonic1/terminal?shell=bash&cols=120&rows=30"
    async with websockets.connect(url, additional_headers=headers) as ws:
        await asyncio.sleep(0.5)
        await ws.send(json.dumps({"type": "input", "data": "show version | head -3\r"}))
        buf = b""
        while b"SONiC Software Version" not in buf:
            msg = await asyncio.wait_for(ws.recv(), 10)
            buf += msg if isinstance(msg, bytes) else msg.encode()
        await ws.send(json.dumps({"type": "input", "data": "exit\r"}))

try:
    asyncio.run(main())
except Exception as exc:
    print(f"erreur : {exc!r}", file=sys.stderr)
    sys.exit(1)
PY
); then ok "bash SONiC via le terminal web : show version"; else ko "terminal web"; fi

step "Matrice de connectivité"
# Cases « na » : cible non routée par le lab depuis ce client (VRF, plateforme), rien n'est pingué.
if post /api/lab/connectivity | jq -e '.totals.ok > 0 and .totals.fail == 0' >/dev/null; then
    ok "tous les pings routés de la matrice réussissent"
else
    ko "matrice de connectivité"
fi

step "RESTCONF des routeurs (modèles sonic-*, module sonic-lab)"
E0=/data/sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0
for ip in 172.20.30.11 172.20.30.12; do
    if curl -sk "https://$ip/.well-known/host-meta" | grep -q "href='/restconf'"; then
        ok "RESTCONF de $ip : découverte (/.well-known/host-meta)"
    else
        ko "RESTCONF de $ip injoignable (docker exec … supervisorctl status restconf)"
    fi
done
code=$(curl -sk -o /dev/null -w '%{http_code}' "$RESTCONF/data/sonic-port:sonic-port")
if [ "$code" = 401 ]; then ok "sans identifiants : 401"; else ko "sans identifiants : HTTP $code au lieu de 401"; fi
if rc GET /data/ietf-yang-library:modules-state |
    jq -e '[."ietf-yang-library:modules-state".module[].name] | index("sonic-port") and index("sonic-lab")' >/dev/null; then
    ok "yang-library : modules sonic-port et sonic-lab"
else
    ko "yang-library (modules-state)"
fi
if rc GET "$E0/admin_status" | jq -e '."sonic-port:admin_status" == "up"' >/dev/null; then ok "GET $E0/admin_status"; else ko "GET $E0/admin_status"; fi
desc=$(rc GET "$E0" | jq -r '."sonic-port:PORT_LIST"[0].description // empty')
code=$(rc_code PATCH "$E0" '{"sonic-port:PORT_LIST":[{"name":"Ethernet0","description":"smoke-restconf"}]}')
if [ "$code" = 204 ] && rc GET "$E0/description" | jq -e '."sonic-port:description" == "smoke-restconf"' >/dev/null; then
    ok "PATCH de la description d'Ethernet0, appliqué par GCU"
else
    ko "PATCH de la description d'Ethernet0 : HTTP $code"
fi
if [ -n "$desc" ]; then
    code=$(rc_code PUT "$E0/description" "$(jq -n --arg d "$desc" '{"sonic-port:description": $d}')")
else
    code=$(rc_code DELETE "$E0/description")
fi
if [ "$code" = 204 ]; then ok "description d'origine remise"; else ko "description d'origine : HTTP $code"; fi
if rc PUT "$E0/mtu" '{"sonic-port:mtu":"abc"}' |
    jq -e '."ietf-restconf:errors".error[0] | .["error-tag"] == "invalid-value" and (.["error-path"] | endswith("/mtu"))' >/dev/null; then
    ok "valeur invalide refusée par les modèles YANG (400, error-path)"
else
    ko "valeur invalide : pas d'erreur RESTCONF attendue"
fi
if rc POST /operations/sonic-lab:preview \
    '{"sonic-lab:input":{"path":"sonic-lab:frr/running-config","method":"PUT","data":{"sonic-lab:running-config":"router bgp 65001\n neighbor foo bar\n"}}}' |
    jq -e '."sonic-lab:output".valid == false' >/dev/null; then
    ok "preview : conf FRR invalide détectée (vtysh -C), rien d'appliqué"
else
    ko "preview d'une conf FRR invalide"
fi
wait_until 60 "état BGP par RESTCONF : sonic1 → 10.0.12.2 Established" rc_bgp_up
if rc POST /operations/sonic-lab:ping '{"sonic-lab:input":{"destination":"192.168.2.10","source":"192.168.1.1","count":2}}' |
    jq -e '."sonic-lab:output".success' >/dev/null; then
    ok "opération ping : sonic1 → client2 (192.168.2.10)"
else
    ko "opération ping sonic1 → 192.168.2.10"
fi

step "Flux sFlow : sonic1 → collecteur → InfluxDB"
# Le bootstrap prépare ce que hsflowd attend dans STATE_DB (absent de l'image SONiC-VS)
if [ "$(docker exec clab-sonic-lab-sonic1 sonic-db-cli STATE_DB HGET 'PORT_INDEX_TABLE|Ethernet0' index 2>/dev/null)" = 1 ] &&
    [ "$(docker exec clab-sonic-lab-sonic1 sonic-db-cli STATE_DB HGET 'SYSTEM_READY|SYSTEM_STATE' Status 2>/dev/null)" = UP ]; then
    ok "STATE_DB préparée par le bootstrap (PORT_INDEX_TABLE, SYSTEM_READY)"
else
    ko "STATE_DB de sonic1 sans PORT_INDEX_TABLE ou SYSTEM_READY (bootstrap à jour ? make redeploy)"
fi
# sFlow déjà déployé par la CD Ansible : on s'en sert ; sinon conf de test, retirée ensuite
SFLOW=/data/sonic-sflow:sonic-sflow
sflow_set_here=0
if ! rc GET "$SFLOW" | jq -e '."sonic-sflow:sonic-sflow".SFLOW.global.admin_state == "up"' >/dev/null; then
    collector=$(envval FLOW_COLLECTOR_IP 172.20.30.6)
    # sessions d'abord (tous les ports à down, sauf deux) : sinon sflowmgrd active d'abord tous les ports
    sessions='{"sonic-sflow:sonic-sflow":{"SFLOW_SESSION":{"SFLOW_SESSION_LIST":[{"port":"all","admin_state":"down"},{"port":"Ethernet0","admin_state":"up","sample_rate":256},{"port":"Ethernet4","admin_state":"up","sample_rate":256}]}}}'
    full=$(jq -c --arg ip "$collector" '."sonic-sflow:sonic-sflow" += {"SFLOW":{"global":{"admin_state":"up","polling_interval":20,"agent_id":"Loopback0"}},"SFLOW_COLLECTOR":{"SFLOW_COLLECTOR_LIST":[{"name":"smoke","collector_ip":$ip,"collector_port":6343,"collector_vrf":"default"}]}}' <<<"$sessions")
    if [[ "$(rc_code PATCH "$SFLOW" "$sessions")" == 20[14] && "$(rc_code PUT "$SFLOW" "$full")" == 20[14] ]]; then
        sflow_set_here=1
        ok "conf sFlow de test sur sonic1 (collecteur $collector), retirée à la fin de l'étape"
    else
        ko "conf sFlow de test refusée par le RESTCONF de sonic1"
    fi
else
    ok "sFlow déjà actif sur sonic1 (conf de démarrage ou CD Ansible)"
fi
flows_since=$(date -u +%Y-%m-%dT%H:%M:%SZ) # seulement les échantillons de ce test
flows_icmp() {
    [ "$(first_value "SELECT sum(\"samples\") FROM \"lab_flow\" WHERE \"node\" = 'sonic1' AND \"app\" = 'icmp' AND \"src\" = '192.168.1.10' AND time > '$flows_since'")" != null ]
}
docker exec -d clab-sonic-lab-client1 ping -q -i 0.002 -s 1200 -w 40 192.168.2.10
wait_until 120 "lab_flow : ICMP de client1 échantillonné par sonic1, classé et nommé" flows_icmp
if [ "$(first_value "SELECT last(\"listening\") FROM \"lab_collector\" WHERE \"task\" = 'flows' AND time > now() - 1m")" = 1 ]; then
    ok "collecteur sFlow en écoute (lab_collector,task=flows)"
else
    ko "collecteur sFlow : pas en écoute (docker compose logs telegraf)"
fi
if [ "$sflow_set_here" = 1 ]; then
    if [ "$(rc_code DELETE "$SFLOW")" = 204 ]; then ok "conf sFlow de test retirée"; else ko "retrait de la conf sFlow de test"; fi
fi

step "Tableaux de bord Grafana (chaque requête de chaque panneau)"
if (cd "$ROOT" && PWD="$ROOT" docker compose exec -T web python - <"$ROOT/scripts/check_dashboards.py"); then
    ok "toutes les requêtes des tableaux de bord aboutissent"
else
    ko "requêtes de tableaux de bord en erreur (détail ci-dessus)"
fi

step "GitLab : comptes, groupe, dépôts exportés dans gitlab/repos/"
# premier démarrage de GitLab : 5 minutes environ
if wait_until 600 "GitLab répond avec le jeton d'API ($GITLAB)" gitlab_ready; then
    owners=" $(glapi "/groups/$(uri "$GITLAB_GROUP")/members?per_page=100" |
        jq -r '[.[] | select(.access_level == 50) | .username] | join(" ")') "
    missing=""
    for entry in ${GITLAB_USERS//,/ }; do
        [[ "$owners" == *" ${entry%%:*} "* ]] || missing+=" ${entry%%:*}"
    done
    if [ -z "$GITLAB_USERS" ]; then
        echo "  – GITLAB_USERS vide : pas de compte à vérifier (root seulement)"
    elif [ -z "$missing" ]; then
        ok "comptes du .env Owner de $GITLAB_GROUP"
    else
        ko "pas Owner de $GITLAB_GROUP :$missing"
    fi
    if docker exec clab-sonic-lab-client1 wget -q -O /dev/null "$GITLAB/users/sign_in"; then
        ok "GitLab joignable depuis client1 (réseau de management)"
    else
        ko "GitLab injoignable depuis client1"
    fi
    if timeout 5 bash -c "exec 3<>/dev/tcp/${GITLAB#http://}/22" 2>/dev/null; then ok "SSH de GitLab ouvert (port 22)"; else ko "SSH de GitLab fermé"; fi

    # aller-retour : push en tant que premier compte du .env, export, suppression
    SMOKE_PROJECT="smoke-$(date +%s)"
    SMOKE_FILE="$ROOT/gitlab/repos/$GITLAB_GROUP/$SMOKE_PROJECT"
    group_id=$(glapi "/groups/$(uri "$GITLAB_GROUP")" | jq -r .id)
    if glapi /projects -X POST --data-urlencode "name=$SMOKE_PROJECT" --data-urlencode "namespace_id=$group_id" >/dev/null; then
        # premier compte du .env ; sans compte, root avec le jeton d'API comme mot de passe
        first=${GITLAB_USERS%%,*}
        first=${first:-root:$GITLAB_TOKEN}
        WORK=$(mktemp -d)
        git -c init.defaultBranch=main init -q "$WORK"
        echo smoke >"$WORK/README.md"
        git -C "$WORK" add README.md
        git -C "$WORK" -c user.name=smoke -c user.email=smoke@sonic-lab.local commit -qm smoke
        sha=$(git -C "$WORK" rev-parse HEAD)
        if GIT_TERMINAL_PROMPT=0 git -C "$WORK" push -q "${GITLAB/:\/\//://$first@}/$GITLAB_GROUP/$SMOKE_PROJECT.git" main 2>/dev/null; then
            ok "push de ${first%%:*} (identifiants du .env) dans $GITLAB_GROUP/$SMOKE_PROJECT"
        else
            ko "push de ${first%%:*} dans $GITLAB_GROUP/$SMOKE_PROJECT"
        fi
        rm -r -- "$WORK"
        # « trigger » : un cycle du service lui-même (make gitlab-sync), qui voit le projet puis sa suppression
        if compose exec -T gitlab-sync python3 /app/gitlab_sync.py trigger >/dev/null &&
            jq -e --arg s "$sha" '.refs["refs/heads/main"] == $s' "$SMOKE_FILE.json" >/dev/null &&
            git ls-remote "$SMOKE_FILE.bundle" | grep -q "$sha"; then
            ok "exporté dans gitlab/repos/ (JSON et bundle)"
        else
            ko "export dans gitlab/repos/"
        fi
        project_id=$(glapi "/projects/$(uri "$GITLAB_GROUP/$SMOKE_PROJECT")" | jq -r .id)
        delete_project "$project_id"
        if compose exec -T gitlab-sync python3 /app/gitlab_sync.py trigger >/dev/null && smoke_files_gone; then
            ok "projet supprimé de GitLab : fichiers retirés de gitlab/repos/"
        else
            ko "projet supprimé de GitLab : fichiers toujours dans gitlab/repos/"
        fi
        rm -f -- "$SMOKE_FILE.json" "$SMOKE_FILE.bundle" # aucune trace, même après un échec
        rmdir -- "$ROOT/gitlab/repos/$GITLAB_GROUP" 2>/dev/null
    else
        ko "création du projet $GITLAB_GROUP/$SMOKE_PROJECT"
    fi

    step "Runner GitLab et CI/CD réseau (pipeline du projet modèle, en lecture seule)"
    wait_until 180 "runner « sonic-lab » en ligne" runner_online
    # projet temporaire créé comme le ferait make ci-project ; main est poussée sans pipeline, puis
    # une branche lance lint, preview et verify, qui ne modifient rien (pas de deploy hors main)
    CI_PROJECT="smoke-ci-$(date +%s)"
    SMOKE_FILE="$ROOT/gitlab/repos/$GITLAB_GROUP/$CI_PROJECT"
    if PROJECT=$CI_PROJECT GITLAB=$GITLAB GITLAB_API_TOKEN=$GITLAB_TOKEN GITLAB_GROUP=$GITLAB_GROUP \
        "$ROOT/scripts/ci_project.sh" >/dev/null; then
        ok "projet $GITLAB_GROUP/$CI_PROJECT créé depuis gitlab/network-config (script de make ci-project)"
        ci_id=$(glapi "/projects/$(uri "$GITLAB_GROUP/$CI_PROJECT")" | jq -r .id)
        glapi "/projects/$ci_id/repository/branches" -X POST --data-urlencode branch=smoke --data-urlencode ref=main >/dev/null
        sleep 5
        pipeline=$(glapi "/projects/$ci_id/pipelines?ref=smoke" | jq -r '.[0].id // empty')
        [ -n "$pipeline" ] || pipeline=$(glapi "/projects/$ci_id/pipeline" -X POST --data-urlencode ref=smoke | jq -r '.id // empty')
        pipeline_done() {
            status=$(glapi "/projects/$ci_id/pipelines/$pipeline" | jq -r .status)
            case $status in success | failed | canceled | skipped) return 0 ;; *) return 1 ;; esac
        }
        status=unknown
        # premier pipeline : téléchargement des images (python, assistant du runner)
        if [ -n "$pipeline" ] && wait_until 900 "pipeline $pipeline terminé" pipeline_done && [ "$status" = success ]; then
            jobs=$(glapi "/projects/$ci_id/pipelines/$pipeline/jobs" | jq -r '[.[] | "\(.name)=\(.status)"] | sort | join(" ")')
            ok "pipeline réussi : $jobs"
        else
            ko "pipeline ${pipeline:-absent} : $status"
            for job in $(glapi "/projects/$ci_id/pipelines/${pipeline:-0}/jobs" 2>/dev/null | jq -r '.[] | select(.status == "failed") | .id'); do
                echo "    --- journal du job $job (fin) ---"
                glapi "/projects/$ci_id/jobs/$job/trace" | tail -25 | sed 's/^/    /'
            done
        fi
        if delete_project "$ci_id"; then ok "projet $CI_PROJECT supprimé"; else ko "suppression du projet $CI_PROJECT"; fi
        compose exec -T gitlab-sync python3 /app/gitlab_sync.py trigger >/dev/null
        rm -f -- "$SMOKE_FILE.json" "$SMOKE_FILE.bundle"
        rmdir -- "$ROOT/gitlab/repos/$GITLAB_GROUP" 2>/dev/null
    else
        ko "création du projet $GITLAB_GROUP/$CI_PROJECT (scripts/ci_project.sh)"
    fi
fi

step "Infra en GitOps (agent du lab, plan d'une merge request sans fusion)"
gitops_on() { get /api/lab | jq -e '.gitops.enabled' >/dev/null; }
if ! gitlab_ready >/dev/null 2>&1 || ! glapi "/projects/$(uri "$GITOPS_PROJECT")" >/dev/null 2>&1; then
    echo "  - projet $GITOPS_PROJECT absent (make infra-project) : étape ignorée"
else
    infra_id=$(glapi "/projects/$(uri "$GITOPS_PROJECT")" | jq -r .id)
    infra_head=$(glapi "/projects/$infra_id/repository/branches/main" | jq -r .commit.id)
    applied_ok() { get /api/lab | jq -e --arg h "$infra_head" '.gitops.enabled and .gitops.status == "deployed" and .gitops.applied_sha == $h'; }
    wait_until 120 "agent GitOps : commit ${infra_head:0:8} de main appliqué" applied_ok
    branch="smoke-gitops-$(date +%s)"
    topo=$(glapi "/projects/$infra_id/repository/files/sonic-lab.clab.yml/raw?ref=main")
    new_topo=$(sed '/^  nodes:$/a\    smoke-client:\n      kind: linux\n      mgmt-ipv4: 172.20.30.99' <<<"$topo")
    glapi "/projects/$infra_id/repository/branches" -X POST --data-urlencode "branch=$branch" --data-urlencode ref=main >/dev/null
    sha=$(glapi "/projects/$infra_id/repository/commits" -X POST -H 'Content-Type: application/json' \
        -d "$(jq -n --arg b "$branch" --arg c "$new_topo" '{branch: $b, commit_message: "smoke : client de test", actions: [{action: "update", file_path: "sonic-lab.clab.yml", content: $c}]}')" | jq -r .id)
    mr=$(glapi "/projects/$infra_id/merge_requests" -X POST --data-urlencode "source_branch=$branch" \
        --data-urlencode target_branch=main --data-urlencode "title=smoke : plan sans fusion" | jq -r .iid)
    plan_done() {
        glapi "/projects/$infra_id/repository/commits/$sha/statuses?all=true" |
            jq -e 'any(.[]; .name == "lab/plan" and (.status == "success" or .status == "failed"))'
    }
    if wait_until 180 "statut lab/plan publié par l'agent (MR !$mr)" plan_done; then
        plan_state=$(glapi "/projects/$infra_id/repository/commits/$sha/statuses?all=true" | jq -r '[.[] | select(.name == "lab/plan")][0] | "\(.status) : \(.description)"')
        if [[ "$plan_state" == success* ]]; then ok "plan : $plan_state"; else ko "plan : $plan_state"; fi
        if glapi "/projects/$infra_id/merge_requests/$mr/notes?per_page=100" | jq -e 'any(.[]; .body | contains("smoke-client") and contains("mermaid"))' >/dev/null; then
            ok "commentaire du plan sur la MR (nœud ajouté, schéma)"
        else
            ko "commentaire du plan absent de la MR !$mr"
        fi
    fi
    glapi "/projects/$infra_id/merge_requests/$mr" -X DELETE >/dev/null # aucune trace : MR supprimée
    glapi "/projects/$infra_id/repository/branches/$(uri "$branch")" -X DELETE >/dev/null
    if [ "$(glapi "/projects/$infra_id/repository/branches/main" | jq -r .commit.id)" = "$infra_head" ]; then
        ok "MR supprimée sans fusion, branche supprimée : le lab n'est pas modifié"
    else
        ko "la branche main a bougé pendant le test"
    fi
fi

step "Nettoyage"
if [ -n "${SNAP:-}" ] && curl -fsS "${AUTH[@]}" -X DELETE "$API/api/nodes/sonic1/snapshots/$SNAP" >/dev/null; then
    ok "snapshot $SNAP supprimé"
fi

printf '\n%d vérification(s) OK, %d échec(s)\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
