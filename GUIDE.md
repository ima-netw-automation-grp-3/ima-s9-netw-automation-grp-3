# Guide complet

Ce guide explique le fonctionnement du lab et rassemble les commandes utiles. Toutes ont été testées
sur ce lab (image `docker-sonic-vs` master du 18/11/2026, FRR 10.7.1, containerlab 0.83.0, Telegraf
1.40.3, InfluxDB 2.10.1, Grafana 13.3.1, GitLab CE 19.6.1). Les commandes absentes ou cassées dans
cette image sont signalées.

Pour une présentation rapide, voir `README.md`.

## Sommaire

1. [Vue d'ensemble](#1-vue-densemble)
2. [Démarrer, arrêter, redéployer](#2-démarrer-arrêter-redéployer)
3. [Se connecter aux nœuds](#3-se-connecter-aux-nœuds)
4. [Routeurs SONiC : comprendre et commander](#4-routeurs-sonic--comprendre-et-commander)
5. [FRR (vtysh) : le routage](#5-frr-vtysh--le-routage)
6. [Clients Alpine](#6-clients-alpine)
7. [L'interface web, page par page](#7-linterface-web-page-par-page)
8. [Configurations : démarrage, running, snapshots](#8-configurations--démarrage-running-snapshots)
9. [API REST et WebSocket](#9-api-rest-et-websocket)
10. [Métrologie (Telegraf, InfluxDB, Grafana)](#10-métrologie-telegraf-influxdb-grafana)
11. [GitLab](#11-gitlab)
12. [Scénarios pas à pas](#12-scénarios-pas-à-pas)
13. [Tests](#13-tests)
14. [Dépannage](#14-dépannage)
15. [Sécurité](#15-sécurité)
16. [Développement](#16-développement)
17. [Aide-mémoire](#17-aide-mémoire)

---

## 1. Vue d'ensemble

Schémas de l'écosystème : [vue simplifiée](docs/ecosysteme-simplifie.png) et
[vue détaillée](docs/ecosysteme.png) (sources Excalidraw dans `docs/`).

### Topologie

```
client1 ─eth1──eth1/Ethernet0─ sonic1 ─Ethernet4/eth2────eth2/Ethernet4─ sonic2 ─Ethernet0/eth1──eth1─ client2
 .10        192.168.1.0/24        .1    10.0.12.1/30      10.0.12.2/30      .1     192.168.2.0/24       .10
                                  AS 65001  ◄────────── eBGP ──────────►  AS 65002
```

| Nœud | Conteneur | mgmt (eth0) | Adresses du lab |
|---|---|---|---|
| sonic1 | `clab-sonic-lab-sonic1` | 172.20.30.11 | Ethernet0 192.168.1.1/24 · Ethernet4 10.0.12.1/30 · Loopback0 10.255.0.1/32 |
| sonic2 | `clab-sonic-lab-sonic2` | 172.20.30.12 | Ethernet0 192.168.2.1/24 · Ethernet4 10.0.12.2/30 · Loopback0 10.255.0.2/32 |
| client1 | `clab-sonic-lab-client1` | 172.20.30.21 | eth1 192.168.1.10/24 (MTU 9100) |
| client2 | `clab-sonic-lab-client2` | 172.20.30.22 | eth1 192.168.2.10/24 (MTU 9100) |

- **eBGP** : sonic1 (AS 65001) et sonic2 (AS 65002) sont voisins sur 10.0.12.0/30. Chacun annonce sa
  Loopback0 et le LAN de son client.
- **Clients** : routes `10.0.0.0/8` et `192.168.0.0/16` via leur routeur. La route par défaut reste sur
  le réseau de management, ce qui donne accès à Internet pour `apk add`.
- **Management** : réseau Docker `sonic-lab-mgmt` (172.20.30.0/24), créé par docker compose et
  réutilisé par containerlab. GitLab y a l'IP 172.20.30.5 (§ 11), et Telegraf l'IP 172.20.30.6, où les
  routeurs envoient leurs flux sFlow (§ 10.8). Le RESTCONF des routeurs y écoute
  (`https://172.20.30.11/restconf`, `https://172.20.30.12/restconf`, § 9), et les jobs du runner GitLab
  y tournent.
- **Correspondance des ports** : `ethN` côté containerlab = port SONiC `Ethernet{4×(N-1)}`. Donc
  eth1 = Ethernet0, eth2 = Ethernet4, eth3 = Ethernet8, etc.

### Les briques

```
make up ──► docker compose (projet sonic-lab)
            ├─ service clab : containerlab 0.83 + Python (image lab/clab/Dockerfile)
            │    lab/clab-runner.sh :
            │      1. docker load de docker-sonic-vs.gz si l'image n'est pas dans Docker
            │      2. vérification de la topologie (lisible, name: sonic-lab), suppression des
            │         conteneurs du lab restés, containerlab deploy -t sonic-lab.clab.yml --reconfigure
            │      3. agent GitOps (lab/gitops/) : le projet GitLab de l'infra pilote le lab (§ 11)
            │      4. attend ; à l'arrêt (SIGTERM : make stop/down, bouton « Arrêter le lab ») :
            │         arrêt de l'agent, containerlab destroy --cleanup, puis suppression des
            │         conteneurs du lab absents du fichier (label containerlab=sonic-lab)
            ├─ service web : FastAPI + SPA React (http://127.0.0.1:8080)
            │    pilote les nœuds par la socket Docker (docker exec), lit et écrit dans lab/
            ├─ métrologie (§ 10)
            │    telegraf : relève tous les nœuds toutes les 10 s (docker exec), sondes ping,
            │               flux sFlow des routeurs (172.20.30.6, UDP 6343)
            │    influxdb : séries temporelles (http://127.0.0.1:8086)
            │    grafana  : tableaux de bord (http://127.0.0.1:3000), intégrés à l'interface
            └─ GitLab (§ 11)
                 gitlab        : http://172.20.30.5, sur le réseau de management du lab
                 gitlab-sync   : dépôts GitLab ⇄ gitlab/repos/, versionné avec le projet
                 gitlab-runner : CI/CD ; jobs Ansible en conteneurs (image sonic-lab-ansible)
                                 sur le réseau de management, qui pilotent les routeurs par RESTCONF
```

Démarrage d'un routeur SONiC (kind `sonic-vs` de containerlab), une vingtaine de secondes au total :

1. containerlab crée le conteneur avec `/bin/bash` comme entrypoint, branche les liens `ethN`, puis
   lance `supervisord`.
2. supervisord démarre en cascade `start.sh`, redis, syncd (SAI virtuel), orchagent, portmgrd,
   intfmgrd, vlanmgrd… puis FRR (zebra, staticd, bgpd), fpmsyncd et bgpcfgd.
3. `start.sh` copie `/var/sonic/config_db.json` (monté depuis `lab/configs/<routeur>/config_db.json`)
   vers `/etc/sonic/config_db.json`, puis le charge dans CONFIG_DB : c'est la config de démarrage.
4. containerlab lance `lab/sonic/lab-bootstrap.sh` dans le routeur. Le script attend CONFIG_DB et
   retire `switch_type: switch`, que l'image écrit mais que les modèles YANG refusent. Il attend
   ensuite les ports, zebra et bgpd, applique `lab/configs/<routeur>/frr.conf` avec `vtysh -f` et fait
   `config save -y`. Il lance enfin le serveur RESTCONF (programme `restconf` de supervisord, § 9) et
   crée `/var/run/lab-ready`.

Les clients Alpine reçoivent leur MTU, leur IP et leurs routes par les commandes `exec` de la
topologie.

### Fichiers du projet

| Fichier | Rôle |
|---|---|
| `docker-compose.yml` | services `clab`, `web`, `influxdb`, `telegraf`, `grafana`, `gitlab`, `gitlab-sync`, `gitlab-runner`, plus `ansible` (image des jobs CI) et `wireshark-image` (téléchargement de l'image des captures) |
| `Makefile` | raccourcis (`make help`) |
| `docs/ecosysteme*.png`, `*.excalidraw` | schémas de l'écosystème (vue simplifiée, vue détaillée) |
| `docker-sonic-vs.gz` | image SONiC, à placer à la racine (non versionnée, voir [Image SONiC](#image-sonic)) ; chargée automatiquement |
| `lab/sonic-lab.clab.yml` | topologie containerlab |
| `lab/clab-runner.sh` | point d'entrée du service `clab` : déploie le lab au démarrage, le détruit à l'arrêt |
| `lab/clab/Dockerfile` | image du service `clab` : containerlab + Python (agent GitOps) |
| `lab/gitops/` | agent GitOps : validateur de topologie, plan, déploiement, rollback ; tests |
| `lab/.gitops/` | état de l'agent (`state.json`) et sauvegardes avant déploiement (ignoré par git) |
| `lab/sonic/lab-bootstrap.sh` | bootstrap exécuté dans chaque routeur |
| `lab/sonic/restconf/` | serveur RESTCONF des routeurs (monté dans chaque routeur), module YANG `sonic-lab`, tests |
| `lab/configs/<routeur>/config_db.json` | config CONFIG_DB de démarrage |
| `lab/configs/<routeur>/frr.conf` | config FRR de démarrage |
| `lab/backups/<routeur>/…` | snapshots créés depuis l'interface (ignorés par git) |
| `lab/.runner-status` | état du runner : `loading-image`, `deploying`, `deployed`, `failed`, `destroying`, `stopped` |
| `lab/clab-sonic-lab/` | dossier de travail de containerlab (créé au déploiement, supprimé par `make down`) |
| `scripts/smoke.sh` | test de bout en bout |
| `scripts/ci_project.sh` | `make ci-project` : crée dans GitLab le projet de CI/CD réseau (Ansible) |
| `scripts/infra_project.sh` | `make infra-project` : crée dans GitLab le projet de l'infra en GitOps |
| `scripts/check_dashboards.py` | exécute toutes les requêtes des tableaux de bord Grafana |
| `web/` | interface : `backend/` (FastAPI), `frontend/` (React), `Dockerfile` |
| `monitoring/telegraf/` | image et configuration de Telegraf, collecteur du lab (`collector/`) et ses tests |
| `monitoring/influxdb/init/` | initialisation d'InfluxDB (mapping InfluxQL) |
| `monitoring/grafana/` | datasources, provisioning et tableaux de bord (`dashboards/*.json`) |
| `gitlab/provision/` | configuration de base de GitLab, à chaque démarrage : utilisateurs, groupe, jeton d'API |
| `gitlab/sync/` | service `gitlab-sync` : `gitlab_sync.py`, son `Dockerfile` et ses tests |
| `gitlab/repos/` | dépôts GitLab exportés (`.json` + `.bundle` par projet), versionnés avec le projet (§ 11) |
| `gitlab/runner/` | service `gitlab-runner` : image et enregistrement automatique (`entrypoint.sh`) |
| `gitlab/network-config/` | modèle du projet de CI/CD réseau avec Ansible : inventaire et `host_vars` (conf des routeurs), rôle `sonic_config`, playbooks, module `sonic_lab.restconf.config`, `.gitlab-ci.yml` |
| `gitlab/ansible/` | image des jobs CI (`sonic-lab-ansible` : ansible-core, ansible-lint, pytest) |
| `gitlab/lab-infra/` | modèle du projet de l'infra en GitOps : `README.md`, `.gitlab-ci.yml` (job `validate`) |
| `.env.example` | variables personnalisables ; `make init` en fait le `.env`, secrets aléatoires compris |
| `scripts/init_env.sh` | `make init` |

---

## 2. Démarrer, arrêter, redéployer

Prérequis : Linux x86_64, Docker avec le plugin compose, `make`, environ 8 Go de RAM libres (dont
1 Go pour la métrologie et 3 Go pour GitLab), environ 9 Go de disque pour les images Telegraf,
InfluxDB, Grafana et GitLab (5,4 Go à elle seule), et `docker-sonic-vs.gz` à la racine du projet
(`make image` l'y télécharge ; provenance et empreinte : [Image SONiC](#image-sonic)). `openssl` est requis
pour `make init`, `curl` pour `make image`, `jq` pour `make smoke`.

| Commande | Équivalent | Effet |
|---|---|---|
| `make init` | `scripts/init_env.sh` | crée `.env` depuis `.env.example`, avec des mots de passe et des jetons aléatoires (un `.env` existant est gardé) |
| `make image` | `curl` + `sha256sum` | télécharge l'image SONiC testée (build 1280150, 234 Mo) si elle manque, et vérifie son empreinte |
| `make up` | `docker compose up -d --build` | construit les images, charge l'image SONiC si besoin, déploie le lab, démarre la métrologie, GitLab et son runner |
| `make down` | `docker compose down` | détruit le lab (containerlab destroy), arrête l'interface, la métrologie et GitLab ; mesures et données de GitLab sont gardées |
| `make stop` | `docker compose stop clab` | arrête le lab seul (containerlab destroy) ; l'interface, la métrologie et GitLab continuent. Bouton « Arrêter le lab » de l'interface |
| `make start` | `docker compose start clab` | démarre le lab arrêté : déploie `lab/sonic-lab.clab.yml` tel qu'il est à ce moment. Bouton « Démarrer le lab » |
| `make redeploy` | `docker compose restart clab` | détruit et redéploie le lab depuis `lab/sonic-lab.clab.yml` et les configs de démarrage |
| `make logs` | `docker compose logs -f --tail=100` | suit les logs du déploiement, de l'interface et de la métrologie |
| `make ps` | `docker compose ps` + `docker ps --filter label=containerlab=sonic-lab` | état des services et des nœuds |
| `make test` | voir § 13 | tests du backend, du collecteur de métriques, de gitlab-sync, du RESTCONF, de l'agent GitOps et du projet Ansible |
| `make smoke` | `scripts/smoke.sh` | test de bout en bout |
| `make iperf` | voir § 10.4 | test de débit iperf3 entre deux clients, enregistré dans InfluxDB |
| `make traffic` | voir § 10.8 | trafic typé (`TYPE=mix`, `tcp`, `udp`, `http`, `dns`, `icmp`) entre deux clients, pendant `DURATION` (60 s) : tableau « Flux réseau » |
| `make collect` | `lab_collector.py once --rates` dans `telegraf` | affiche ce que relève le collecteur (line protocol) |
| `make gitlab-sync` | `gitlab_sync.py trigger` dans `gitlab-sync` | déclenche un cycle de synchronisation des dépôts GitLab avec `gitlab/repos/` et affiche ce qu'il a fait (§ 11) |
| `make ci-project` | `scripts/ci_project.sh` | crée dans GitLab le projet de CI/CD réseau à partir de `gitlab/network-config/` (§ 11) |
| `make ansible` | `docker run` de l'image `sonic-lab-ansible` | lance un playbook du modèle contre le lab (`PLAYBOOK=deploy.yml`, `ARGS="--check --diff"` par défaut) (§ 11) |
| `make ci-watch` | `scripts/ci_watch.sh` | suit en direct un pipeline de `network-config` : journal Ansible de chaque job, puis bilan (`NEXT=1` attend le prochain push, `REF=`, `PIPELINE=`, `LOGS=0`) (§ 11) |
| `make infra-project` | `scripts/infra_project.sh` | crée dans GitLab le projet de l'infra en GitOps (topologie et confs de démarrage actuelles), que l'agent adopte (§ 11) |

Les équivalents `docker compose` se lancent **depuis la racine du projet**. Le service `clab` monte
le projet au même chemin que sur l'hôte (`${PWD}:${PWD}`), parce que containerlab résout les fichiers
du lab avec des chemins d'hôte. Les cibles `make` se placent d'elles-mêmes dans le bon dossier.

```bash
make up                          # puis http://127.0.0.1:8080 ; Grafana : http://127.0.0.1:3000
make logs                        # Ctrl-C pour quitter le suivi
cat lab/.runner-status           # deployed quand containerlab a fini
docker compose logs clab         # logs complets du déploiement
make ps
docker ps --filter label=containerlab=sonic-lab --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

À savoir :

- **`make down`** détruit les nœuds, le réseau `sonic-lab-mgmt` et `lab/clab-sonic-lab/`. L'image
  SONiC, les fichiers de `lab/configs/`, l'historique des mesures (volumes InfluxDB et Grafana) et
  les données de GitLab (volumes `gitlab-*`) sont conservés.
- **Redéploiement** (`make redeploy` ou le bouton de l'interface) : tout repart de la config de
  démarrage, et les modifications non sauvegardées sont perdues.
- **Arrêter / démarrer le lab** (`make stop` / `make start`, ou « Arrêter le lab » et « Démarrer le
  lab » dans le menu « Actions du lab » du tableau de bord) : le lab tourne exactement quand le
  service `clab` tourne. À l'arrêt, son runner détruit le lab ; au démarrage, il déploie le fichier de
  topologie tel qu'il est alors. C'est le moyen de changer d'infra (ci-dessous).
- **Destruction indépendante du fichier** : containerlab ne retrouve que les nœuds du fichier actuel.
  Le runner supprime ensuite tous les autres conteneurs du lab (label `containerlab=sonic-lab`) : ceux
  d'une version précédente du fichier, ou restés après un arrêt brutal. Il le refait avant chaque
  déploiement.
- **Redémarrage de l'hôte** : le service `clab` (`restart: unless-stopped`) redéploie le lab au retour
  de Docker, sauf si le lab a été arrêté (`make stop` ou le bouton) : il reste alors arrêté. `make up`
  (ou `docker compose up web`, qui dépend de `clab`) le redémarre.
- **Ne redémarrez jamais un routeur seul** (`docker restart clab-sonic-lab-sonic1`) : SONiC ne
  repartirait pas, car containerlab ne lance supervisord qu'au déploiement. Redéployez.

### Image SONiC

L'image `docker-sonic-vs` n'est pas redistribuée dans ce dépôt. Elle provient des builds publiques du
projet SONiC (dépôt `sonic-buildimage`, plateforme `vs`, artefact `target/docker-sonic-vs.gz`). Le lab
a été construit et testé avec cette build :

| | |
|---|---|
| version | `master.1280150-71d15c250` (18/11/2026), FRR 10.7.1 |
| fichier | `docker-sonic-vs.gz`, 234 282 727 octets |
| sha256 | `e3416916c2985f98cccef9faefc9cdecb62a5b2bc5f7abe758f6420cf0d6d1b7` |

`make image` télécharge cette build depuis l'API publique des builds SONiC
(`sonic-build.azurewebsites.net`, `buildId=1280150`) et vérifie son empreinte ; vous pouvez aussi
placer le fichier vous-même à la racine du projet. Le service `clab` le charge dans Docker au premier
`make up`, après avoir vérifié son empreinte (`SONIC_ARCHIVE_SHA256` dans `.env` pour une autre build,
vide pour ne rien vérifier). Une autre build *master* peut casser les contournements décrits dans ce
guide ; son `supervisord.conf` exige par exemple la variable `BGPD_GRACEFUL_RESTART_ARG`, définie
(vide) dans `lab/sonic-lab.clab.yml`.

### Changer d'infra (autre topologie)

Le lab déployé est celui de `lab/sonic-lab.clab.yml`, avec une conf de démarrage par routeur dans
`lab/configs/<routeur>/` (`config_db.json` et `frr.conf`). Pour passer à une autre infra :

1. **Arrêter le lab** : « Actions du lab » → « Arrêter le lab » dans le tableau de bord, ou
   `make stop`. Sauvegardez d'abord ce qui doit l'être (snapshots, « Sauvegarder running →
   startup »).
2. **Remplacer les fichiers** : `lab/sonic-lab.clab.yml` et les dossiers `lab/configs/<routeur>/`.
   Par exemple, pour une topologie préparée dans un autre dossier :

   ```bash
   cp ../autre-topo/sonic-lab.clab.yml lab/
   rm -r lab/configs/* && cp -r ../autre-topo/configs/. lab/configs/
   ```

3. **Vérifier** dans le tableau de bord : lab arrêté, il dessine la topologie du fichier (« Topologie
   de lab/sonic-lab.clab.yml (non déployée) ») et signale un fichier illisible ou un mauvais `name`.
4. **Démarrer le lab** : « Actions du lab » → « Démarrer le lab », ou `make start`. Le runner
   vérifie le fichier, supprime les conteneurs restés de l'ancienne infra, puis lance containerlab
   (« Logs du déploiement » ou `make logs` pour suivre).

« Redéployer le lab » enchaîne les étapes 1 et 4 : il applique aussi un fichier modifié pendant que le
lab tournait. Le tableau de bord signale ce cas (« Le lab déployé ne correspond plus au fichier de
topologie », avec les nœuds déployés absents du fichier).

Ce que le fichier doit respecter :

- `name: sonic-lab` : l'interface, la métrologie et l'agent GitOps cherchent le lab sous ce nom
  (`LAB_NAME`), et le runner refuse de déployer un autre nom.
- `mgmt` : réseau `sonic-lab-mgmt`, sous-réseau `172.20.30.0/24`, sans `ipv4-range`. Les `mgmt-ipv4`
  des nœuds sont dans ce sous-réseau, hors de `.1` (passerelle), de `GITLAB_IP` (`.5`) et de
  `FLOW_COLLECTOR_IP` (`.6`).
- Un dossier `lab/configs/<routeur>/` par routeur SONiC monté par la topologie : containerlab refuse de
  déployer si la source d'un bind manque.
- `ethN` du conteneur = `Ethernet{4×(N-1)}` dans SONiC (`eth1` → `Ethernet0`).
- La mémoire : environ 550 Mo par routeur SONiC-VS.

En GitOps (§ 11), la topologie vient du projet `lab-infra` : un fichier modifié localement est
remplacé au prochain merge, et les merges faits pendant l'arrêt s'appliquent au démarrage. Changez
d'infra par une merge request, ou désactivez l'agent (`GITOPS_ENABLED=0` dans `.env`, puis `make up`).

### Personnaliser (`.env`)

```bash
make init        # .env depuis .env.example, mots de passe et jetons aléatoires
```

Sans `make` : `cp .env.example .env`, puis remplissez les variables marquées « obligatoire » ; docker
compose refuse de démarrer sans elles. Il n'y a aucun mot de passe par défaut.

| Variable | Défaut | Rôle |
|---|---|---|
| `WEB_BIND` | `127.0.0.1` | adresse d'écoute de l'interface |
| `WEB_PORT` | `8080` | port de l'interface (à changer si 8080 est pris) |
| `WEB_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | noms d'hôte acceptés dans l'en-tête Host |
| `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD` | vides | authentification HTTP Basic (active si les deux sont remplis) ; obligatoire dès que l'interface est exposée, sinon elle refuse de démarrer |
| `GRAFANA_BIND` / `GRAFANA_PORT` | `127.0.0.1` / `3000` | adresse et port de Grafana |
| `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` | `admin` / généré | compte qui modifie les tableaux de bord (la consultation est anonyme) ; premier démarrage seulement |
| `GRAFANA_URL` | vide | URL de Grafana vue par le navigateur, pour l'onglet Métrologie (vide : même hôte que l'interface, port `GRAFANA_PORT`) |
| `INFLUXDB_BIND` / `INFLUXDB_PORT` | `127.0.0.1` / `8086` | adresse et port d'InfluxDB |
| `INFLUXDB_ADMIN_USER` / `INFLUXDB_ADMIN_PASSWORD` | `admin` / généré | compte InfluxDB (premier démarrage seulement) |
| `INFLUXDB_TOKEN` | généré | jeton d'API, utilisé par Telegraf et Grafana (premier démarrage seulement) |
| `INFLUXDB_RETENTION` | `7d` | durée de conservation des mesures (premier démarrage seulement) |
| `TELEGRAF_INTERVAL` | `10s` | intervalle de collecte |
| `PROBE_SCOPE`, `PROBE_COUNT`, `PROBE_INTERVAL`, `PROBE_MAX_TARGETS` | `all`, `5`, `0.2`, `64` | sondes ping (§ 10.4) |
| `FLOW_COLLECTOR_IP` | `172.20.30.6` | IP de Telegraf sur le réseau de management, destination des flux sFlow des routeurs (§ 10.8) ; transmise aux jobs CI |
| `SFLOW_PORT` | `6343` | port UDP du collecteur sFlow (`0` : pas de collecte de flux) |
| `FLOW_MAX_KEYS` | `2000` | nombre maximal de séries de flux écrites par cycle |
| `CAPTURE_IMAGE`, `CAPTURE_MAX_SESSIONS`, `CAPTURE_MAX_MB` | Wireshark 4.6.9, `3`, `500` | captures de paquets (§ 7.1) |
| `GITLAB_IP` | `172.20.30.5` | IP de GitLab sur le réseau de management (libre dans 172.20.30.0/24) |
| `GITLAB_ROOT_PASSWORD` | généré | mot de passe de `root` (premier démarrage de GitLab seulement) |
| `GITLAB_GROUP` | `ima-netw-automation-grp-3` | groupe dont les comptes sont Owner |
| `GITLAB_USERS` | `lab:<généré>` | comptes `utilisateur:motdepasse` (8 caractères minimum), Owner du groupe |
| `GITLAB_API_TOKEN` | généré | jeton d'API de root, utilisé par gitlab-sync, le runner, l'agent GitOps et l'interface (20 caractères exactement) |
| `GITLAB_SYNC_INTERVAL` | `60` | intervalle de synchronisation des dépôts, en secondes |
| `GITLAB_EXTERNAL_URL` | vide | URL publique de GitLab derrière un reverse proxy HTTPS (« Héberger sur un serveur ») |
| `GITLAB_SSH_BIND` / `GITLAB_SSH_PORT` | `127.0.0.1` / `2222` | publication de git en SSH sur l'hôte ; `0.0.0.0` / `22` sur un serveur dont le sshd écoute ailleurs |
| `RESTCONF_USER` / `RESTCONF_PASSWORD` | `admin` / généré | identifiants du RESTCONF des routeurs, fournis aussi aux jobs CI (§ 9) |
| `GITLAB_RUNNER_CONCURRENT`, `GITLAB_RUNNER_TAGS`, `GITLAB_RUNNER_IMAGE` | `2`, `sonic-lab,restconf`, `python:3.12-alpine` | runner GitLab (§ 11) |
| `GITOPS_PROJECT`, `GITOPS_ENABLED`, `GITOPS_INTERVAL`, `GITOPS_ALLOWED_IMAGES` | `<groupe>/lab-infra`, `1`, `20`, `docker-sonic-vs:latest,alpine:3.22` | agent GitOps (§ 11) |
| `SONIC_ARCHIVE_SHA256` | empreinte de la build testée | vérifiée avant le chargement de l'image SONiC ; vide : pas de vérification |

Appliquez avec `make up`. Les variables marquées « premier démarrage seulement » sont lues quand
InfluxDB ou GitLab initialise son volume ; pour InfluxDB, voir § 10.7, pour GitLab, § 11.

### Héberger sur un serveur, avec un nom de domaine

Les exemples utilisent `lab.example.com` pour l'interface et `grafana.lab.example.com` pour
Grafana : remplacez-les par vos noms.

**1. Le serveur.** Linux x86_64 avec Docker (plugin compose) et `make`, 5 Go de RAM libres. Copiez
le dossier du projet entier, `docker-sonic-vs.gz` compris, puis lancez tout depuis ce dossier.

**2. Le DNS.** Chez votre registrar ou dans votre zone DNS, créez un enregistrement `A` (et `AAAA`
si le serveur a une IPv6) pour chaque nom, vers l'IP publique du serveur :

```
lab.example.com          A    203.0.113.10
grafana.lab.example.com  A    203.0.113.10
```

Vérifiez avec `dig +short lab.example.com` : la commande doit renvoyer l'IP du serveur.

**3. Le `.env`.** L'interface donne un accès root au serveur (socket Docker) : dès qu'elle est
exposée, elle refuse de démarrer sans authentification.

```bash
make init
```

Les deux variantes ci-dessous ne modifient que quelques lignes de ce fichier.

#### Variante A : HTTP, avec le port dans l'URL

Accès par `http://lab.example.com:8080`. Les services écoutent directement sur toutes les
interfaces du serveur.

```ini
WEB_BIND=0.0.0.0
WEB_ALLOWED_HOSTS=lab.example.com
WEB_AUTH_USER=admin
WEB_AUTH_PASSWORD=un-mot-de-passe-long
GRAFANA_BIND=0.0.0.0
```

Puis `make up`, et ouvrez les ports 8080 et 3000 dans le pare-feu de l'hébergeur s'il y en a un.
L'onglet Métrologie trouve Grafana tout seul sur `http://lab.example.com:3000`.

Le mot de passe circule en clair en HTTP : cette variante convient à un réseau de confiance ou à un
essai, pas à un serveur exposé durablement.

#### Variante B : HTTPS, sans port dans l'URL

Accès par `https://lab.example.com`. [Caddy](https://caddyserver.com), installé sur le serveur,
écoute sur 80 et 443, obtient et renouvelle seul les certificats Let's Encrypt, et relaie vers
l'interface et Grafana, qui restent sur 127.0.0.1.

`.env` : laissez `WEB_BIND` et `GRAFANA_BIND` à `127.0.0.1`, et renseignez :

```ini
WEB_ALLOWED_HOSTS=lab.example.com
WEB_AUTH_USER=admin
WEB_AUTH_PASSWORD=un-mot-de-passe-long
GRAFANA_URL=https://grafana.lab.example.com
```

`GRAFANA_URL` est indispensable ici : sans elle, l'onglet Métrologie chercherait Grafana en
`http://…:3000`, que le navigateur bloque dans une page HTTPS.

Installez Caddy (Debian 12, Ubuntu 24.04 et suivantes : `sudo apt install caddy` ; autres systèmes :
<https://caddyserver.com/docs/install>), puis remplacez le contenu de `/etc/caddy/Caddyfile` par :

```
lab.example.com {
	reverse_proxy 127.0.0.1:8080
}

grafana.lab.example.com {
	reverse_proxy 127.0.0.1:3000
}
```

```bash
sudo systemctl reload caddy
make up
```

Les ports 80 et 443 doivent être ouverts et joignables depuis Internet : Let's Encrypt s'en sert
pour valider le nom. Caddy relaie les WebSockets (terminal) et transmet l'en-tête Host tel quel, ce
que demandent les contrôles de l'interface. Rien d'autre à configurer.

En cas de souci :

- `sudo journalctl -u caddy -f` : erreurs de certificat (DNS pas encore propagé, port 80 fermé) ;
- `hôte non autorisé` : le nom tapé dans le navigateur n'est pas dans `WEB_ALLOWED_HOSTS` ;
- onglet Métrologie vide : vérifiez `GRAFANA_URL`, puis `make up` pour la prendre en compte.

**GitLab derrière Caddy** (facultatif). Ajoutez un vhost `gitlab.lab.example.com { reverse_proxy
172.20.30.5:80 }` et, dans `.env`, `GITLAB_EXTERNAL_URL=https://gitlab.lab.example.com` : GitLab reste
en HTTP derrière le proxy et fait confiance à l'hôte. Pour git en SSH sur le port 22 du serveur,
déplacez d'abord le sshd de l'hôte sur un autre port, puis `GITLAB_SSH_BIND=0.0.0.0` et
`GITLAB_SSH_PORT=22`. Changer ces valeurs recrée le conteneur GitLab au `make up` suivant (5 min).

À savoir : Grafana reste consultable sans compte, en lecture seule, par quiconque connaît son
adresse. Les modifications demandent le compte `admin`.

---

## 3. Se connecter aux nœuds

### Par l'interface web

http://127.0.0.1:8080, onglet **CLI** d'un nœud. Pour un routeur, deux shells : la CLI SONiC (`bash`,
avec `show …` et `config …`) ou FRR (`vtysh`). Le bouton ↗ ouvre le terminal dans une fenêtre séparée.

### Par docker exec

```bash
docker exec -it clab-sonic-lab-sonic1 bash        # CLI SONiC (root)
docker exec -it clab-sonic-lab-sonic1 vtysh       # CLI FRR
docker exec -it clab-sonic-lab-client1 sh         # client Alpine

# commandes ponctuelles, sans shell interactif
docker exec clab-sonic-lab-sonic1 show ip bgp summary
docker exec clab-sonic-lab-sonic1 vtysh -c "show ip route"
docker exec clab-sonic-lab-client1 ping -c 3 192.168.2.10
```

### Par containerlab

Le binaire containerlab est dans le conteneur du service `clab`, et son dossier de travail est `lab/` :

```bash
docker compose exec clab containerlab inspect -t sonic-lab.clab.yml
docker compose exec clab containerlab exec -t sonic-lab.clab.yml \
    --label clab-node-name=sonic1 --cmd "show ip interfaces"
```

Si containerlab est aussi installé sur l'hôte, il voit le même lab : `containerlab inspect --all`.

---

## 4. Routeurs SONiC : comprendre et commander

### 4.1 SONiC en bref

SONiC est piloté par des bases redis :

```
config … / interface web ──► CONFIG_DB (n° 4)  config voulue (= config_db.json)
                                 │  portmgrd, intfmgrd, vlanmgrd… (démons *mgrd)
                                 ▼
                             APPL_DB (n° 0)    état applicatif (ports, routes, voisins…)
                                 │  orchagent
                                 ▼
                             ASIC_DB (n° 1)    objets SAI ──► syncd + SAI virtuel (vs)
FRR : bgpd ──► zebra ──► noyau Linux + fpmsyncd ──► APPL_DB ROUTE_TABLE
STATE_DB (n° 6) : états opérationnels    COUNTERS_DB (n° 2) : compteurs
```

Dans `docker-sonic-vs`, le plan de données est simulé. Le SAI virtuel relie chaque fil `ethN` à une
interface noyau `EthernetX`, et c'est la pile Linux du conteneur qui route les paquets. Les routes
BGP apparaissent donc aussi dans `ip route`. Le routeur expose 32 ports (Ethernet0 à Ethernet124),
mais seuls ceux reliés à un fil `ethN` sont câblés : ici Ethernet0 et Ethernet4.

### 4.2 Consulter

Commandes qui fonctionnent dans cette image :

```bash
show version                       # version SONiC, plateforme, HWSKU
show interfaces status             # ports : vitesse, MTU, oper/admin
show interfaces description        # descriptions
show ip interfaces                 # adresses IPv4 par interface
show ipv6 interfaces
show ip route                      # table de routage (via FRR)
show ip route summary
show ip bgp summary                # sessions BGP
show ip bgp neighbors              # détail des voisins (sans argument)
show arp                           # voisins IPv4
show ndp                           # voisins IPv6
show vlan brief
show mac                           # table FDB
show runningconfiguration all      # CONFIG_DB complète en JSON
show feature status
show platform summary
show logging                       # syslog
show uptime / show clock / show processes summary
show interfaces counters           # compteurs SAI des ports (RX_OK, TX_OK, erreurs, rejets)
counterpoll show                   # groupes de compteurs activés (PORT_STAT)
```

`show interfaces counters` fonctionne parce que la conf de démarrage active les compteurs des ports
(`FLEX_COUNTER_TABLE` : `PORT`, l'équivalent de `counterpoll port enable`). Ses colonnes de débit
(RX_BPS, TX_BPS) restent à N/A dans cette image : les débits sont dans l'onglet État et dans la
métrologie (§ 10).

Ce qui ne fonctionne pas dans cette image :

| Commande | Pourquoi | Alternative |
|---|---|---|
| `show lldp …`, `show services` | appellent `docker` depuis le routeur | aucune (LLDP ne voit que le réseau de management) |
| `show system-health`, `show environment` | matériel absent | n/a |
| `show ip bgp neighbors <ip> advertised-routes` | cherche le voisin dans la table BGP_NEIGHBOR de CONFIG_DB, vide ici (BGP est configuré dans FRR) | `vtysh -c "show ip bgp neighbors <ip> advertised-routes"` |

### 4.3 Configurer avec la CLI SONiC

Les commandes `config …` écrivent dans CONFIG_DB, et les démons appliquent aussitôt. Les exemples
utilisent Ethernet8, un port non câblé, donc sans risque.

```bash
# interfaces
config interface startup Ethernet8                    # admin up
config interface shutdown Ethernet8                   # admin down
config interface mtu Ethernet8 9000
config interface ip add Ethernet8 192.168.99.1/24
config interface ip remove Ethernet8 192.168.99.1/24

# description : pas de « config interface description » dans cette version
sonic-db-cli CONFIG_DB HSET "PORT|Ethernet8" description "vers labo"
sonic-db-cli CONFIG_DB HDEL "PORT|Ethernet8" description

# loopback
config loopback add Loopback1
config interface ip add Loopback1 10.255.1.1/32
config interface ip remove Loopback1 10.255.1.1/32    # retirer la dernière IP supprime aussi Loopback1
config loopback del Loopback1                         # pour une loopback sans IP

# VLAN et interface de VLAN (SVI)
config vlan add 100
config vlan member add -u 100 Ethernet8               # -u : untagged
config interface ip add Vlan100 192.168.100.1/24
config interface ip remove Vlan100 192.168.100.1/24
config vlan member del 100 Ethernet8
config switchport mode routed Ethernet8               # remet le port en mode routé
config vlan del 100                                   # supprime le VLAN puis affiche une erreur
                                                      # Python (« docker » introuvable) : sans conséquence

# routes statiques (table STATIC_ROUTE, appliquées par bgpcfgd dans staticd)
config route add prefix 10.50.0.0/24 nexthop 10.0.12.2
config route del prefix 10.50.0.0/24 nexthop 10.0.12.2

# sauvegarde dans le routeur et chargement d'un fichier (fusion)
config save -y                       # CONFIG_DB → /etc/sonic/config_db.json (dans le conteneur)
config load -y /tmp/extrait.json     # = sonic-cfggen -j /tmp/extrait.json --write-to-db
```

`config save -y` ne survit pas à un redéploiement. Pour rendre une modification permanente, utilisez
« Sauvegarder running → startup » (§ 8), qui écrit dans `lab/configs/`.

GCU (Generic Config Updater) fonctionne : `config apply-patch` (JSON Patch validé par les modèles
YANG, avec `--dry-run`) et `config checkpoint` ont été testés. Le bootstrap retire en effet
`switch_type: switch`, que l'image écrit au démarrage mais que le modèle `sonic-device_metadata`
refuse. Le serveur RESTCONF (§ 9) applique ses écritures par GCU.

```bash
echo '[{"op":"replace","path":"/PORT/Ethernet0/description","value":"lan"}]' > /tmp/p.json
config apply-patch --dry-run /tmp/p.json && config apply-patch /tmp/p.json
```

Ce qui ne fonctionne pas dans cette image :

| Commande | Pourquoi | Alternative |
|---|---|---|
| `config reload` | arrête et relance les services via monit/systemd, absents du conteneur | à **ne pas utiliser** ; restauration via l'interface ou l'API (§ 8) |
| `config interface description` | n'existe pas dans cette version | `sonic-db-cli … HSET "PORT|EthernetX" description …` |

### 4.4 Lire et écrire les bases

```bash
sonic-cfggen -d --print-data                          # CONFIG_DB complète en JSON
sonic-cfggen -j fichier.json --write-to-db            # fusionne un fichier dans CONFIG_DB

sonic-db-cli CONFIG_DB KEYS 'INTERFACE|*'
sonic-db-cli CONFIG_DB HGETALL 'PORT|Ethernet0'       # config voulue du port
sonic-db-cli APPL_DB HGETALL 'PORT_TABLE:Ethernet0'   # état applicatif (oper_status…)
sonic-db-cli STATE_DB HGETALL 'PORT_TABLE|Ethernet0'  # état opérationnel
sonic-db-cli APPL_DB KEYS 'ROUTE_TABLE:*'             # routes poussées par FRR (fpmsyncd)
sonic-db-cli ASIC_DB KEYS '*ROUTE_ENTRY*'             # routes programmées dans l'« ASIC » virtuel
sonic-db-cli CONFIG_DB GET CONFIG_DB_INITIALIZED      # 1 quand CONFIG_DB est chargée

redis-cli -n 4 HGETALL 'PORT|Ethernet4'               # même chose avec redis-cli (4 = CONFIG_DB)
```

Séparateurs de clé : `|` dans CONFIG_DB et STATE_DB, `:` dans APPL_DB.

### 4.5 Services et logs

```bash
supervisorctl status                       # tous les démons SONiC + FRR
tail -f /var/log/syslog                    # logs (ou : show logging)
grep -i bgp /var/log/syslog | tail -20
ip -s link show Ethernet0                  # compteurs noyau du port
bash /usr/local/bin/lab-bootstrap.sh       # relance le bootstrap (idempotent)
```

> **Piège** : `supervisorctl restart bgpd` efface toute la config BGP. Elle vit en mémoire et le
> bootstrap ne la réapplique qu'au démarrage. Relancez ensuite le bootstrap (bouton de l'interface,
> `POST /api/nodes/<routeur>/bootstrap` ou la commande ci-dessus) : il réapplique la config FRR **de
> démarrage**. Sauvegardez d'abord vos modifications FRR si vous voulez les garder.

---

## 5. FRR (vtysh) : le routage

BGP est configuré directement dans FRR, avec la config de démarrage `lab/configs/<routeur>/frr.conf` :

```
router bgp 65001
 bgp router-id 10.255.0.1
 bgp log-neighbor-changes
 no bgp ebgp-requires-policy          ← FRR exige sinon une route-map pour l'eBGP
 neighbor 10.0.12.2 remote-as 65002
 neighbor 10.0.12.2 description sonic2
 address-family ipv4 unicast
  network 10.255.0.1/32
  network 192.168.1.0/24
 exit-address-family
exit
```

`bgp_asn` n'est volontairement pas renseigné dans CONFIG_DB : bgpcfgd générerait sinon sa propre
config « datacenter ».

### Consulter

```bash
vtysh -c "show bgp summary"
vtysh -c "show ip bgp"                                        # table BGP
vtysh -c "show ip bgp 192.168.2.0/24"                         # détail d'un préfixe
vtysh -c "show ip bgp neighbors 10.0.12.2"                    # état, timers, raison du dernier reset
vtysh -c "show ip bgp neighbors 10.0.12.2 advertised-routes"  # ce qu'on annonce
vtysh -c "show ip bgp neighbors 10.0.12.2 routes"             # ce qu'on reçoit et accepte
vtysh -c "show ip route"                                      # RIB complète
vtysh -c "show ip route bgp"
vtysh -c "show running-config"
```

### Configurer

En interactif (`docker exec -it clab-sonic-lab-sonic1 vtysh`) :

```
configure terminal
router bgp 65001
 address-family ipv4 unicast
  network 10.255.1.1/32
 exit-address-family
end
```

En une ligne :

```bash
vtysh -c 'conf t' -c 'router bgp 65001' -c 'address-family ipv4 unicast' -c 'network 10.255.1.1/32'
vtysh -c 'conf t' -c 'router bgp 65001' -c 'neighbor 10.0.12.2 shutdown'      # coupe la session
vtysh -c 'conf t' -c 'router bgp 65001' -c 'no neighbor 10.0.12.2 shutdown'
vtysh -c 'conf t' -c 'ip route 10.77.0.0/24 Null0'                            # route statique FRR
vtysh -c 'clear bgp ipv4 unicast 10.0.12.2 soft'                              # renégocie sans couper
```

Depuis un fichier :

```bash
vtysh -C -f /tmp/conf.frr      # vérifie seulement la syntaxe (code ≠ 0 et ligne fautive si erreur)
vtysh -f /tmp/conf.frr         # applique (fusion)
```

> `write memory` écrit `/etc/frr/frr.conf` dans le conteneur, qui n'est pas réutilisé dans ce lab.
> Pour une config FRR permanente : « Sauvegarder running → startup » (§ 8).

Lignes gérées par SONiC, masquées par l'interface et jamais retirées : `fpm address …`,
`zebra nexthop-group keep …`, `ip nht resolve-via-default`, la route-map `RM_SET_SRC` et
`ip protocol bgp route-map RM_SET_SRC`. Les deux dernières viennent de bgpcfgd : les routes BGP
installées dans le noyau ont pour source la Loopback0.

---

## 6. Clients Alpine

```bash
docker exec -it clab-sonic-lab-client1 sh
ip -o addr                         # adresses
ip route                           # routes (passerelle 192.168.1.1)
ip -o link                         # interfaces et MTU
ping -c 3 192.168.2.10             # client2
ping -c 2 -s 8000 192.168.2.10     # gros paquets (MTU 9100)
ping -c 3 10.255.0.2               # Loopback0 de sonic2
traceroute -n 192.168.2.10         # 192.168.1.1 → 10.255.0.2 → 192.168.2.10
```

Le deuxième saut s'affiche en 10.255.0.2 et non en 10.0.12.2 : bgpcfgd fixe la source des routes BGP
sur la Loopback0.

Les commandes `ping` et `traceroute` sont celles de busybox : `-M` n'existe pas. Pour d'autres outils,
utilisez `apk add`, qui passe par le réseau de management. Exemple, générer du trafic :

```bash
docker exec clab-sonic-lab-client2 sh -c 'apk add -q iperf3 && iperf3 -s -D'
docker exec clab-sonic-lab-client1 sh -c 'apk add -q iperf3 && iperf3 -c 192.168.2.10 -t 10'
docker exec clab-sonic-lab-client2 pkill iperf3
```

On mesure environ 5 Gbit/s à travers les deux routeurs. Les débits apparaissent dans l'onglet **État**
des routeurs. Les paquets installés disparaissent au redéploiement. `make iperf` fait la même mesure
et l'enregistre dans la métrologie (§ 10.4).

**MTU** : les clients sont réglés à 9100, comme les ports SONiC. Avec la valeur par défaut des liens
containerlab (9500), les fils du routeur (MTU 9122) jetteraient les gros segments TCP : le ping
passerait, mais pas les transferts.

---

## 7. L'interface web, page par page

http://127.0.0.1:8080. En haut à droite : Grafana (icône de graphique), documentation de l'API
(bouton « API »), thème clair ou sombre.

### Tableau de bord

- **En-tête** : nombre de nœuds prêts et état de chaque session BGP.
- **Topologie** en direct :
  - nœuds colorés par état (Prêt, Démarrage, Bootstrap, Attention, Erreur, Arrêté) ;
  - liens verts (up) ou rouges (down) ;
  - pour chaque extrémité : port SONiC, fil `ethN` et adresses ; mention « (shutdown) » si le port
    est administrativement coupé ;
  - badge eBGP au milieu du lien entre routeurs.

  La disposition suit la topologie sans rien déclarer. Une chaîne (le lab de base) tient sur une
  ligne. Sinon, le dessin est en étages : clients en bas, routeurs d'accès au-dessus, cœur ou spines
  en haut, chaque client sous son routeur. Quand les étiquettes n'ont pas la place, elles s'affichent
  au survol (ou au toucher) du câble et la session eBGP devient un point sur le câble. Un dessin plus
  large que la page est réduit, avec un bouton « Taille réelle ».

  Les équipements se déplacent à la souris : câbles et étiquettes suivent, et le dessin s'agrandit
  quand on sort de ses bords (la zone défile pour suivre). La disposition est enregistrée dans le
  navigateur, pour ce lab ; un nœud ajouté depuis se range sous le dessin. « Disposition
  automatique » efface ces positions. Un simple clic ouvre toujours la page du nœud.

  Un clic sur un nœud ouvre sa page. Un clic droit sur une étiquette d'interface, sur le voyant d'un
  port ou sur un câble propose, pour l'interface (ou chacune des deux extrémités du câble) :
  - une **capture Wireshark** (§ 7.1) ;
  - **Couper / Rétablir** le câble, *hors configuration du nœud* : `ip link set ethN down|up` dans
    l'espace réseau du conteneur, comme on débrancherait le câble. Le pair perd la porteuse : son
    voyant passe au rouge, celui du côté coupé s'éteint, et l'étiquette indique « (câble coupé) ».
    Rien n'est écrit dans CONFIG_DB ni FRR (l'`admin_status` SONiC reste `up`) ; un redéploiement du
    nœud rebranche le câble. Couper l'interface d'un client fait supprimer par le noyau ses routes
    statiques : elles sont sauvegardées dans le nœud (`/tmp/.sonic-lab-wire-ethN.routes`) et
    rejouées au rétablissement. Sur SONiC-VS, `EthernetN` passe `oper down` et le trafic s'arrête
    aussitôt, mais l'interface noyau reste up : une session eBGP ne tombe qu'à l'expiration du hold
    timer (180 s par défaut).
- **Bandeau de déploiement** quand le lab n'est pas déployé : démarrage, chargement, déploiement,
  échec, arrêt…, avec accès aux logs de containerlab.
- **Alertes sur le fichier de topologie** : fichier illisible ou `name` autre que `sonic-lab` (le
  déploiement serait refusé) ; nœuds déployés absents du fichier (modifié pendant que le lab tournait :
  redéployez pour l'appliquer) ; conteneurs du lab restés après l'arrêt. Lab arrêté, la topologie
  dessinée est celle du fichier, avant tout déploiement.
- **Cartes des nœuds** : image, IP de management, uptime, adresses, voisins BGP, et raccourcis
  Détails, CLI, Config.
- **Métrologie** : ouvre la vue d'ensemble dans Grafana (§ 10.3).
- **Tester la connectivité** : chaque client pingue les autres clients et la Loopback0 de chaque
  routeur. Le résultat s'affiche en matrice ; le survol d'une case montre la sortie du ping. Seules les
  adresses que la table de routage du client envoie dans le lab sont essayées : une cible qu'il ne
  joindrait que par sa route par défaut du management (eth0) est **non routée** (gris), pas en échec —
  c'est la segmentation attendue (VRF ou plateforme différente, loopbacks d'underlay vues depuis une
  VRF). Un client à plusieurs adresses (une carte par VRF) est vert dès que l'une répond. Les cibles
  qu'aucun client ne route sont masquées (interrupteur « Afficher les cibles non routées »).
- **Actions du lab** :
  - **Démarrer le lab / Arrêter le lab** (selon l'état) : démarre ou arrête le service `clab`, donc
    déploie ou détruit le lab (§ 2, « Changer d'infra »). L'arrêt demande confirmation ; il reste
    possible pendant un déploiement, pour abandonner une topologie qui se déploie mal. « Démarrer » est
    grisé si le service `clab` n'existe pas (`make up`).
  - **Redéployer le lab** : destroy puis deploy de `lab/sonic-lab.clab.yml`, même modifié depuis le
    déploiement.
  - **Sauvegarder tout** : running → startup pour chaque routeur.
  - **Réinitialiser** : startup → running en mode replace.
  - **Logs du déploiement**.

### Page d'un routeur

| Onglet | Contenu |
|---|---|
| **État** | phase et bouton « Relancer le bootstrap » ; version SONiC/FRR ; BGP (AS, router-id, sessions) ; interfaces (port, fil, description, admin/oper, MTU, vitesse, IP, compteurs et débits), avec l'option « Afficher les 32 ports » ; voisins BGP ; voisins ARP/NDP ; table de routage ; services supervisord. Rafraîchi toutes les 3 s. |
| **Métrologie** | tableau de bord Grafana « Routeur SONiC » du routeur, avec son historique : ports, BGP, routage, bases redis, services, syslog, conteneur (§ 10.3). Période, interfaces et annotations se règlent en haut ; « Ouvrir dans Grafana » l'affiche en pleine page. |
| **Configuration** | éditeur de config, détaillé ci-dessous |
| **Sauvegardes** | config de démarrage (comparer, restaurer, sauvegarder) et snapshots (créer avec un libellé, voir, comparer, restaurer, supprimer) |
| **CLI** | terminal web : CLI SONiC (`bash`) ou FRR (`vtysh`) ; ↻ ouvre une nouvelle session, ↗ une fenêtre séparée. La session est conservée en changeant d'onglet ou de page ; elle se termine au rechargement ou à la fermeture de l'onglet du navigateur |
| **Capture** | Wireshark dans le navigateur, en direct, sur une interface du routeur (§ 7.1) |
| **Commandes** | boutons `show …` prédéfinis ou commande libre (Entrée pour lancer) ; code de sortie, durée, copie |
| **Logs** | syslog du routeur : nombre de lignes, filtre, suivi toutes les 3 s, erreurs et avertissements colorés |

**Onglet Configuration**, pas à pas :

1. Choisir le domaine : **CONFIG_DB (SONiC)** en JSON, ou **FRR (routage)** en texte vtysh.
2. Choisir la source et cliquer **Récupérer** : *Running* (le routeur), *Conf de démarrage*
   (`lab/configs/`) ou un snapshot. Les boutons ⇪ et ⇩ importent et téléchargent un fichier.
3. Éditer. « modifié dans l'éditeur » s'affiche dès que le contenu diffère de la source.
4. Choisir le mode : **Merge** (ajoute et modifie, ne supprime rien) ou **Replace** (l'éditeur fait foi).
5. **Aperçu** : rien n'est appliqué.
   - CONFIG_DB : liste des ajouts, modifications et suppressions par table|clé et champ, plus le diff
     JSON.
   - FRR : syntaxe validée, lignes ajoutées par un merge, ou commandes de retrait d'un replace, avec
     les blocs qui seront recréés.
6. **Pousser**, avec confirmation en replace. Le résultat montre les opérations appliquées ou les
   changements effectifs.
7. Le badge **modifications non sauvegardées** compare la running à la config de démarrage ; un clic
   ouvre le diff. **Sauvegarder running → startup** rend la running permanente.

### Page d'un client

| Onglet | Contenu |
|---|---|
| **État** | interfaces (état, MTU, MAC, adresses, compteurs, débits) et table de routage |
| **Métrologie** | tableau de bord Grafana « Client Linux » : interfaces, trafic, latence vers chaque cible, tests iperf3, routes, conteneur |
| **Tests réseau** | ping (nombre de paquets réglable) et traceroute ; les badges remplissent la cible ; matrice de connectivité |
| **CLI** | terminal `sh` (conservé en changeant d'onglet ou de page) |
| **Capture** | Wireshark dans le navigateur sur une interface du client (§ 7.1) |
| **Commandes** | commandes prédéfinies (`ip addr`, `ip route`…) ou libre |

### 7.1 Capture de paquets (Wireshark)

L'onglet **Capture** d'un routeur ou d'un client ouvre un vrai Wireshark dans la page, en direct :

1. Choisir l'interface (`eth1`, `Ethernet0`…) et, si besoin, un **filtre de capture** (syntaxe BPF :
   `icmp`, `port 179`…), puis **Démarrer la capture**.
   Depuis la topologie, un clic droit sur une interface ou un câble fait la même chose sans filtre (une
   capture sans filtre déjà en cours sur cette interface est reprise).
2. **Ouvrir Wireshark** affiche les paquets au fil de l'eau, dès que la visionneuse répond (quelques
   secondes) ; ↗ l'ouvre dans une fenêtre séparée. Le tableau donne début, durée et taille de chaque capture.
3. ⇩ télécharge le fichier `.pcap` (lisible aussitôt, même capture en cours) ; ■ arrête la capture ;
   la corbeille arrête et supprime le fichier.

Fonctionnement : un conteneur sonde (`dumpcap`) partage l'espace réseau du nœud et écrit le `.pcap`
dans le volume `captures` ; un conteneur Wireshark (image `lscr.io/linuxserver/wireshark`, sans port
publié, mot de passe aléatoire) lit ce fichier en continu. L'interface web le sert sous
`/wireshark/<id>/` après ses propres contrôles (Host, Origin, Basic). Rien n'est modifié dans le nœud.

- L'image Wireshark est téléchargée par `make up` ; sinon au premier lancement (quelques minutes).
- Le bureau Wireshark exige un **contexte sécurisé** : `http://127.0.0.1:8080` convient, pas une
  adresse IP distante en HTTP (passer par HTTPS, § 2, « Héberger sur un serveur »).
- Limites : `CAPTURE_MAX_SESSIONS` captures simultanées (3), fichier plafonné à `CAPTURE_MAX_MB` Mo
  (500). Une capture s'arrête si le nœud est recréé (le fichier reste téléchargeable).
- Variables : `CAPTURE_IMAGE`, `CAPTURE_MAX_SESSIONS`, `CAPTURE_MAX_MB` (`.env.example`).
- API : `GET/POST /api/captures`, `GET /api/captures/interfaces?node=`, `POST /api/captures/{id}/stop`,
  `DELETE /api/captures/{id}`, `GET /api/captures/{id}/download`.

---

## 8. Configurations : démarrage, running, snapshots

| | Où | Quand |
|---|---|---|
| **Démarrage (startup)** | `lab/configs/<routeur>/config_db.json` et `frr.conf` | chargée à chaque déploiement |
| **Running** | dans le routeur (CONFIG_DB + mémoire de FRR) | ce qui tourne ; modifiée par l'interface, la CLI ou l'API |
| **Snapshot** | `lab/backups/<routeur>/<AAAAMMJJ-HHMMSS>[-libellé]/` (heure UTC) | copie ponctuelle de la running (`config_db.json`, `frr.conf`, `meta.json`) |

**En mode GitOps** (§ 11, « Infra en GitOps »), la conf de démarrage vit dans le projet GitLab
`lab-infra` : l'agent écrit `lab/configs/` à chaque déploiement, et « Sauvegarder running →
startup » ouvre une merge request au lieu d'écrire les fichiers. Les autres opérations ne changent
pas.

### Opérations

- **Sauvegarder running → startup** écrit les deux fichiers de `lab/configs/<routeur>/` et lance
  `config save -y` dans le routeur. Elle exclut :
  - les éléments propres à l'instance : table `LOGGER`, `DEVICE_METADATA.localhost.mac` ;
  - les lignes FRR gérées par SONiC (§ 5).

  Une entrée équivalente à celle du fichier garde la forme du fichier. Sauvegarder sans changement ne
  modifie donc rien. Les fichiers gardent le propriétaire du dossier `lab/`.
- **Restaurer** (startup ou snapshot) pousse CONFIG_DB puis FRR, en mode **replace**.
- **Modifier la config de démarrage à la main** : éditez les fichiers de `lab/configs/`, puis
  `make redeploy`. Le JSON de `config_db.json` doit rester valide.

### Règles du mode replace

**CONFIG_DB** :

- **Tables ordinaires** (INTERFACE, LOOPBACK_INTERFACE, VLAN, VLAN_MEMBER, VLAN_INTERFACE,
  STATIC_ROUTE, …) : les clés absentes de la config voulue sont supprimées, les entrées modifiées
  sont remplacées en entier.
- **PORT** : chaque entrée est remplacée champ par champ, mais jamais supprimée. Les champs matériels
  absents (`lanes`, `alias`, `index`, `speed`, `subport`) sont conservés. Les champs `admin_status` et
  `mtu` qui disparaissent sont remis explicitement à `down` et `9100`, parce que les démons SONiC
  n'appliquent pas de valeur par défaut quand un champ est simplement supprimé. Pour les comparaisons,
  « absent » et « valeur par défaut » sont équivalents.
- **Tables système** (DEVICE_METADATA, FEATURE, BREAKOUT_CFG, buffers et QoS, tables `*_MAP`, LOGGER,
  CRM…) : seulement fusionnées, jamais de suppression.
- **Ordre d'application** : suppressions d'abord (IP avant interface, membres avant VLAN), puis
  créations (parents avant enfants).

**FRR** :

- Une ligne ou un bloc absent de la config voulue est retiré (`no …`).
- Un bloc `router …` dont des lignes ont disparu est supprimé puis recréé, ce qui réinitialise les
  sessions BGP. L'aperçu le signale.
- Dans un bloc `interface X`, les lignes disparues sont niées une à une.
- La config voulue est ensuite appliquée en merge.
- Les lignes gérées par SONiC ne sont jamais touchées.

---

## 9. API REST et WebSocket

Documentation interactive : http://127.0.0.1:8080/api/docs. Les erreurs ont la forme
`{"error": "...", "detail": ...}`, avec les codes suivants :

- 400 : requête invalide, ou config refusée (le détail donne l'erreur `vtysh -C`) ;
- 404 : nœud ou snapshot inconnu ;
- 409 : nœud non démarré ;
- 502 : commande en échec dans le nœud ;
- 401, 403 : authentification ou origine refusée (§ 15).

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/health` | santé de l'interface |
| GET | `/api/monitoring` | adresse de Grafana, disponibilité, uid des tableaux de bord (onglet Métrologie) |
| GET · POST · DELETE | `/api/captures…` | captures de paquets Wireshark (§ 7.1) |
| GET | `/api/lab` | état complet : runner (`state`, `action` en cours, `error` de la dernière action), nœuds, liens, sessions BGP, `topology_file`, `topology_error`, `stale_nodes` (nœuds déployés absents du fichier) |
| GET | `/api/lab/runner/logs?tail=300` | logs de containerlab |
| POST | `/api/lab/start` | démarre le lab arrêté (service `clab`) : déploie le fichier de topologie actuel |
| POST | `/api/lab/stop` | arrête le lab (destroy, en tâche de fond) ; 409 si une autre action est en cours |
| POST | `/api/lab/redeploy` | redéploie le lab (destroy puis deploy) |
| POST | `/api/lab/connectivity` | matrice de ping (`{"count": 2}`) ; case `status` = `ok`, `fail` ou `na` (non routée), `totals` |
| POST | `/api/lab/save-all` · `/api/lab/reset-all` | sauvegarder ou réinitialiser tous les routeurs |
| GET | `/api/nodes/<n>` | état détaillé d'un nœud |
| POST | `/api/nodes/<n>/exec` | commande shell `{"command": "...", "timeout": 30}` |
| POST | `/api/nodes/<n>/ping` · `/traceroute` | `{"target": "192.168.2.10", "count": 3}` (traceroute : clients) |
| GET | `/api/nodes/<n>/logs?lines=300` | syslog (routeur) ou logs du conteneur (client) |
| POST | `/api/nodes/<n>/bootstrap` | relance le bootstrap (routeur) |
| POST | `/api/nodes/<n>/wires/<ethN>` | `{"up": false}` coupe le câble, `{"up": true}` le rétablit (extrémités de liens uniquement, § 7) |
| GET | `/api/nodes/<n>/config?domain=configdb\|frr&source=running\|startup\|snapshot:<id>` | lire une config |
| POST | `/api/nodes/<n>/config/preview` · `/config/push` | `{"domain", "mode": "merge"\|"replace", "content"}` |
| POST | `/api/nodes/<n>/config/save` | running → startup |
| POST | `/api/nodes/<n>/config/restore` | `{"source": "startup"\|"snapshot:<id>", "domains": [...]}` |
| GET | `/api/nodes/<n>/config/diff?against=startup` | running comparée à startup ou à un snapshot |
| GET / POST | `/api/nodes/<n>/snapshots` | lister, créer (`{"label": "..."}`) |
| GET / DELETE | `/api/nodes/<n>/snapshots/<id>` | lire (avec contenu), supprimer |
| WS | `/ws/nodes/<n>/terminal?shell=bash\|vtysh\|sh&cols=120&rows=32` | terminal |

### Exemples curl

```bash
API=http://127.0.0.1:8080

curl -s $API/api/lab | jq '{nœuds: [.nodes[] | "\(.name)=\(.phase)"], bgp: [.bgp_sessions[].state]}'
curl -s $API/api/nodes/sonic1 | jq '.bgp.peers'

# exporter la running CONFIG_DB et FRR dans des fichiers
curl -s "$API/api/nodes/sonic1/config?domain=configdb" | jq -r .content > sonic1-config_db.json
curl -s "$API/api/nodes/sonic1/config?domain=frr" | jq -r .content > sonic1-frr.conf

# pousser un fichier (aperçu d'abord, puis push)
jq -n --rawfile c sonic1-frr.conf '{domain: "frr", mode: "merge", content: $c}' > /tmp/body.json
curl -s -X POST $API/api/nodes/sonic1/config/preview -H 'content-type: application/json' -d @/tmp/body.json | jq .
curl -s -X POST $API/api/nodes/sonic1/config/push    -H 'content-type: application/json' -d @/tmp/body.json | jq .

# extrait CONFIG_DB en merge : couper le lien inter-routeurs
curl -s -X POST $API/api/nodes/sonic1/config/push -H 'content-type: application/json' \
  -d '{"domain":"configdb","mode":"merge","content":"{\"PORT\":{\"Ethernet4\":{\"admin_status\":\"down\"}}}"}'

# commande, ping
curl -s -X POST $API/api/nodes/sonic1/exec -H 'content-type: application/json' \
  -d '{"command":"show ip route"}' | jq -r .output
curl -s -X POST $API/api/nodes/client1/ping -H 'content-type: application/json' \
  -d '{"target":"192.168.2.10","count":3}' | jq .summary

# différences, sauvegarde, snapshot, restauration
curl -s $API/api/nodes/sonic1/config/diff | jq '{dirty, configdb: .configdb.changes, frr: .frr.diff}'
curl -s -X POST $API/api/nodes/sonic1/config/save | jq .
curl -s -X POST $API/api/nodes/sonic1/snapshots -H 'content-type: application/json' -d '{"label":"avant test"}' | jq .id
curl -s -X POST $API/api/nodes/sonic1/config/restore -H 'content-type: application/json' -d '{"source":"startup"}' | jq .ok
```

### Protocole du terminal (WebSocket)

- **Navigateur → serveur** : texte JSON, `{"type": "input", "data": "show version\r"}` ou
  `{"type": "resize", "cols": 120, "rows": 40}`.
- **Serveur → navigateur** : octets bruts du terminal, puis `{"type": "exit", "code": 0}` en fin de
  session, ou `{"type": "error", "message": "..."}`.

Exemple de client, à lancer dans le conteneur `web` qui contient la bibliothèque `websockets` :

```bash
docker compose exec -T web python - <<'EOF'
import asyncio, json, websockets
async def main():
    async with websockets.connect("ws://127.0.0.1:8080/ws/nodes/sonic1/terminal?shell=vtysh") as ws:
        await asyncio.sleep(0.5)
        await ws.send(json.dumps({"type": "input", "data": "show bgp summary\r"}))
        await asyncio.sleep(1)
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), 1)
                print(msg if isinstance(msg, str) else msg.decode(errors="replace"), end="")
            except asyncio.TimeoutError:
                break
asyncio.run(main())
EOF
```

### RESTCONF des routeurs

Chaque routeur expose une API RESTCONF (RFC 8040), sur son IP de management, port 443. Le serveur,
`lab/sonic/restconf/`, est lancé par le bootstrap sous supervisord (programme `restconf`). Il remplace
le mgmt-framework, absent de l'image `docker-sonic-vs`.

| | |
|---|---|
| URL | `https://172.20.30.11/restconf` (sonic1), `https://172.20.30.12/restconf` (sonic2), depuis l'hôte, GitLab, les jobs CI et les nœuds |
| Authentification | HTTP Basic : `RESTCONF_USER` / `RESTCONF_PASSWORD` du `.env` (créés par `make init`) ; après un changement, `make up` recrée le service `clab` (le lab est redéployé) et le runner |
| TLS | certificat auto-signé, créé au démarrage du routeur (`curl -k`) |
| Format | JSON seulement (`application/yang-data+json`, RFC 7951) |
| Journal | syslog du routeur, programme `restconf` : une ligne par requête (onglet Logs de l'interface) |

**Données `sonic-*` (CONFIG_DB).** Ce sont les modèles YANG de SONiC (`/usr/local/yang-models`), avec
la même forme d'URI que le mgmt-framework : `/restconf/data/<module>:<module>/<TABLE>/<LISTE>=<clés>/<champ>`.
Une liste à plusieurs clés prend ses valeurs séparées par des virgules, une `/` encodée en `%2F` :
`INTERFACE_IPPREFIX_LIST=Ethernet0,192.168.1.1%2F24`.

| Méthode | Effet |
|---|---|
| GET, HEAD | lecture de la running (`?depth=N`, `?content=config\|nonconfig\|all`) ; un conteneur vide est renvoyé vide, une instance absente donne 404 |
| PUT | crée ou remplace la ressource (201 si créée, 204 sinon) |
| PATCH | fusion dans une ressource existante (204 ; 404 si elle n'existe pas) |
| POST | crée une ressource enfant (201 + `Location` ; 409 si elle existe déjà) |
| DELETE | supprime la ressource (204 ; 404 si elle n'existe pas) |

Une écriture suit toujours le même chemin. Le serveur lit la CONFIG_DB et modifie la ressource dans
l'arbre YANG, puis valide l'arbre **complet** avec libyang (types, leafref, must). Il calcule ensuite
le JSON Patch à appliquer, et GCU (`config apply-patch`) l'applique dans le bon ordre. Une écriture
dure 1 à 3 s. Si rien ne change, il ne se passe rien.

Une donnée refusée renvoie 400. L'erreur indique le message de libyang et le chemin en cause :

```json
{"ietf-restconf:errors": {"error": [{"error-type": "application", "error-tag": "invalid-value",
  "error-path": "/sonic-interface:sonic-interface/INTERFACE/INTERFACE_LIST[name='Ethernet99']/name",
  "error-message": "Invalid leafref value \"Ethernet99\" - no target instance \"/port:sonic-port/port:PORT/port:PORT_LIST/port:name\" with the same value"}]}}
```

**Module `sonic-lab`** (`lab/sonic/restconf/yang/sonic-lab.yang`) :

| Ressource | Rôle |
|---|---|
| `sonic-lab:frr/running-config` | conf FRR en texte, sans les lignes gérées par SONiC. PUT = replace (même logique que l'interface web : un bloc `router bgp` modifié est recréé, ses sessions sont réinitialisées), PATCH = merge. `vtysh -C` valide la syntaxe avant toute application |
| `sonic-lab:frr/state/bgp-neighbor=<adresse>` | sessions BGP : `remote-as`, `state`, `description`, `uptime`, `prefixes-received` |
| `POST /restconf/operations/sonic-lab:ping` | ping depuis le routeur : `destination`, `source` (adresse ou interface), `count` (1 à 20) |
| `POST /restconf/operations/sonic-lab:preview` | aperçu d'une écriture (`path`, `method`, `data`) : `valid`, `errors`, `diff`, `patch` (CONFIG_DB), `commands` et `resets` (FRR). Rien n'est appliqué : GCU tourne en `--dry-run` |

**Découverte** : `/.well-known/host-meta` (sans authentification), `/restconf`,
`/restconf/data/ietf-yang-library:modules-state` (tous les modules, avec l'URL de leur schéma), et
`/restconf/yang/<module>` pour télécharger un fichier .yang. `/restconf/data` renvoie tout le
datastore.

Non pris en charge : XML, YANG-Patch (RFC 8072), paramètres de requête autres que `depth` et
`content`, notifications. Les données d'état SONiC (STATE_DB, compteurs) ne sont pas exposées, en
dehors de l'état BGP.

```bash
set -a; . ./.env; set +a          # identifiants RESTCONF du .env (depuis la racine du projet)
R=https://172.20.30.11/restconf; A="-u $RESTCONF_USER:$RESTCONF_PASSWORD"; J='Content-Type: application/yang-data+json'

curl -sk $A $R/data/sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0
curl -sk $A "$R/data/sonic-port:sonic-port?depth=3"
curl -sk $A $R/data/ietf-yang-library:modules-state | jq -r '."ietf-yang-library:modules-state".module[].name'
curl -sk $A $R/yang/sonic-interface > sonic-interface.yang

# description d'un port (merge), puis adresse IP d'une interface (création)
curl -sk $A -X PATCH -H "$J" -d '{"sonic-port:description":"vers sonic2"}' \
  $R/data/sonic-port:sonic-port/PORT/PORT_LIST=Ethernet4/description
curl -sk $A -X POST -H "$J" -d '{"sonic-interface:INTERFACE_IPPREFIX_LIST":[{"name":"Ethernet0","ip-prefix":"192.168.100.1/24"}]}' \
  $R/data/sonic-interface:sonic-interface/INTERFACE
curl -sk $A -X DELETE "$R/data/sonic-interface:sonic-interface/INTERFACE/INTERFACE_IPPREFIX_LIST=Ethernet0,192.168.100.1%2F24"

# FRR : lire, aperçu d'un replace, application
curl -sk $A $R/data/sonic-lab:frr/running-config | jq -r '."sonic-lab:running-config"' > frr.conf
jq -n --rawfile c frr.conf '{"sonic-lab:input": {path: "sonic-lab:frr/running-config", method: "PUT", data: {"sonic-lab:running-config": $c}}}' \
  | curl -sk $A -X POST -H "$J" -d @- $R/operations/sonic-lab:preview | jq '."sonic-lab:output"'
jq -n --rawfile c frr.conf '{"sonic-lab:running-config": $c}' | curl -sk $A -X PUT -H "$J" -d @- $R/data/sonic-lab:frr/running-config

# état BGP, ping
curl -sk $A $R/data/sonic-lab:frr/state | jq
curl -sk $A -X POST -H "$J" -d '{"sonic-lab:input":{"destination":"192.168.2.10","source":"192.168.1.1"}}' \
  $R/operations/sonic-lab:ping | jq '."sonic-lab:output" | {success, received, "rtt-avg"}'
```

Les écritures RESTCONF modifient la **running**, comme l'interface. L'indicateur « modifications non
sauvegardées » les signale, et « Sauvegarder running → startup » les rend permanentes. Relancer le
bootstrap depuis l'interface recharge le code du serveur, après une modification de
`lab/sonic/restconf/`.

---

## 10. Métrologie (Telegraf, InfluxDB, Grafana)

Une stack TIG démarre avec le lab (`make up`) et relève en continu l'état de tous les nœuds :

```
nœuds du lab  ──docker exec──►  Telegraf  ──►  InfluxDB  ──►  Grafana  ──►  onglet « Métrologie »
SONiC, Alpine                   collecteur      bucket lab     7 tableaux    de l'interface web
                                (toutes les     (7 jours)      de bord
                                 10 s)
routeurs SONiC ──sFlow, UDP 6343──►  (172.20.30.6)      flux échantillonnés : types de trafic (§ 10.8)
```

| Service | Accès | Rôle |
|---|---|---|
| `telegraf` | aucun port publié | collecteur du lab, statistiques des conteneurs et de l'hôte, auto-surveillance |
| `influxdb` | http://127.0.0.1:8086, `INFLUXDB_ADMIN_USER` / `INFLUXDB_ADMIN_PASSWORD` | séries temporelles : organisation `sonic-lab`, bucket `lab`, jeton `INFLUXDB_TOKEN` |
| `grafana` | http://127.0.0.1:3000, lecture sans compte ; `admin` / `GRAFANA_ADMIN_PASSWORD` pour modifier | tableaux de bord du dossier du nom du lab (`sonic-lab`) |

Comptes, jeton, ports et durée de conservation se règlent dans `.env` (§ 2).

### 10.1 Comment les données sont relevées

Toutes les 10 s, Telegraf réveille le collecteur `monitoring/telegraf/collector/lab_collector.py` :

1. **Découverte** des nœuds : ceux du fichier de topologie et les conteneurs portant le label
   `containerlab=sonic-lab`. Un nœud de la topologie sans conteneur est relevé comme absent.
2. **Relevé de chaque nœud**, en parallèle, par `docker exec` :
   - routeur SONiC : un script Python exécuté dans le routeur (`labmon/remote/sonic_collect.py`) lit
     toutes les bases redis, FRR en JSON (`vrf all`), `/sys/class/net`, `/proc`, supervisord et le
     syslog ;
   - autre nœud : un script shell compatible busybox (`labmon/remote/linux_collect.sh`).
3. **Sondes** : chaque nœud pingue les adresses des autres (§ 10.4).
4. Envoi à Telegraf au format line protocol, puis écriture dans InfluxDB.

Rien n'est installé dans les nœuds, et la collecte ne s'appuie sur aucune liste figée. Chaque cycle
relit les conteneurs, la topologie, tous les netdevs, toutes les clés redis, tous les VRF et voisins
BGP, tous les programmes supervisord. Un ajout sur un équipement apparaît donc au cycle suivant :

| Vous ajoutez… | Ce qui apparaît, sans rien configurer |
|---|---|
| un lien (`sonicX:ethN` dans la topologie, puis `make redeploy`) | le port `EthernetX` devient câblé : débit dans les graphes, ligne dans les liens |
| une adresse, une loopback, une SVI | l'interface et son adresse ; l'adresse devient une cible des sondes de tous les nœuds |
| un voisin BGP, un VRF, une famille d'adresses | la session, ses préfixes, ses messages ; un événement à chaque changement d'état |
| un VLAN, un PortChannel, une ACL, une route statique | l'interface noyau, les clés de la table dans CONFIG_DB, APPL_DB et ASIC_DB, les routes par protocole |
| un nœud (client3, sonic3…), puis `make redeploy` | le nœud dans toutes les listes des tableaux de bord, les sondes vers lui et depuis lui |
| des compteurs SONiC (`counterpoll …`) | les compteurs de COUNTERS_DB dans `lab_sonic_counters` |

### 10.2 Ce qui est collecté

| Mesure | Contenu |
|---|---|
| `lab_node` | inventaire et santé : conteneur démarré, prêt, uptime, IP de management, versions SONiC et FRR, HWSKU, OS, nombre de processus |
| `lab_interface` | tous les netdevs de tous les nœuds : tous les compteurs noyau, état, MTU, vitesse, adresses ; débit, paquets, erreurs et pertes par seconde, utilisation |
| `lab_link` | chaque lien de la topologie : état des deux extrémités, débit dans chaque sens |
| `lab_probe` | sondes ping : envoyés, reçus, perte, RTT min, moyen, max, écart-type, gigue |
| `lab_iperf` | tests `make iperf` : débit par seconde, retransmissions, RTT, résumé du test |
| `lab_bgp`, `lab_bgp_peer`, `lab_bgp_afi` | instances BGP ; voisins (état, uptime, messages par type, connexions, dernier reset, RTT estimé) ; préfixes reçus et envoyés par famille |
| `lab_route`, `lab_route_entry` | routes par protocole (RIB, FIB) et table de routage détaillée (500 préfixes au plus par famille) |
| `lab_neighbor` | voisins ARP et NDP par interface et par état |
| `lab_sonic_port` | les 32 ports : tous les champs de CONFIG_DB `PORT`, d'APPL_DB et de STATE_DB `PORT_TABLE`, fil `ethN` |
| `lab_sonic_service` | programmes supervisord : état, uptime, CPU, mémoire, threads, descripteurs |
| `lab_sonic_db` | nombre de clés par base redis et par table (dans ASIC_DB : par type d'objet SAI) |
| `lab_sonic_crm` | ressources de l'ASIC (CRM) utilisées et disponibles |
| `lab_sonic_counters` | compteurs de COUNTERS_DB : ports SAI, notifications reçues par les orchs… |
| `lab_sonic_redis` | statistiques du serveur redis |
| `lab_sonic_lldp` | voisins LLDP |
| `lab_syslog` | lignes de syslog cumulées par programme et par sévérité |
| `lab_flow` | flux sFlow, un point par cycle et par (routeur, port d'entrée, protocole, application, src, dst) : octets et paquets estimés (× taux d'échantillonnage), échantillons ; tags `peer_node` (nœud relié au port), `edge` (`yes` : trafic entrant dans le réseau depuis un client), `src_node`, `dst_node`, `encap`/`vni` (VXLAN) (§ 10.8) |
| `lab_flow_agent` | réception sFlow par routeur : datagrammes, échantillons de flux et de compteurs, datagrammes perdus (séquence), illisibles |
| `lab_event` | événements : sessions BGP, ports, nœuds, bootstrap, tests iperf3, tests de trafic (`make traffic`) |
| `lab_collector` | santé de la collecte : durée et erreurs par nœud, volume par cycle ; `task=flows` : écoute sFlow, datagrammes reçus et rejetés |
| `docker_container_*` | CPU, mémoire, réseau et disque de chaque conteneur du lab et de la stack |
| `cpu`, `mem`, `system`, `disk`, `internal_*` | hôte du lab et Telegraf |

Tags communs : `node`, `kind`, `role`, et `lab` sur toutes les mesures. Les valeurs numériques sont
toutes des float. `make collect` affiche ce que reçoit Telegraf.

FRR ne journalise rien dans cette image : les événements BGP viennent de la comparaison entre deux
relevés. Une session coupée puis rétablie entre deux relevés est repérée à son uptime qui recule.

Les compteurs natifs des ports SONiC (flex counters `PORT_STAT`) sont activés par la conf de démarrage
(`FLEX_COUNTER_TABLE` dans `lab/configs/<routeur>/config_db.json`), d'où les chiffres de
`show interfaces counters`. Les groupes RIF et QUEUE restent à zéro dans SONiC-VS : ils ne sont pas
activés.

### 10.3 Tableaux de bord

Ils sont rangés dans le dossier de Grafana portant le nom du lab (`sonic-lab`), dont la page d'accueil est la vue
d'ensemble. Sur un volume `grafana-data` existant, Grafana ne renomme pas un dossier déjà créé :
`curl -u "admin:$GRAFANA_ADMIN_PASSWORD" -X PUT -H 'Content-Type: application/json' -d '{"title":"<nom du lab>","overwrite":true}' http://127.0.0.1:3000/api/folders/sonic-lab`.

| Tableau de bord | Contenu |
|---|---|
| Lab SONiC — vue d'ensemble | nœuds, liens, sessions BGP, matrices de latence et de perte, ressources des nœuds, derniers événements |
| Routeur SONiC | ports, débits, compteurs SAI, BGP, routage (FRR → APPL_DB → ASIC_DB), CRM, bases redis, LLDP, services, syslog, conteneur |
| Client Linux | interfaces, trafic, sondes depuis le client, tests iperf3, routes, voisins, conteneur |
| Interfaces | toutes les interfaces de tous les nœuds, une ligne par nœud |
| Métrologie | latence, gigue et perte en matrices et dans le temps, joignabilité, débits iperf3 |
| Infrastructure | hôte, conteneurs, durée et erreurs de la collecte, Telegraf |
| Flux réseau (sFlow) | types de trafic : débit entrant par application et par protocole, répartition, conversations, détail par routeur et port, VXLAN par VNI, santé de la réception sFlow (§ 10.8) |

- Les listes du haut (nœud, interfaces, sources…) se remplissent à partir des données : un nouvel
  élément y apparaît de lui-même.
- Les événements (`lab_event`) s'affichent en annotations sur les graphes ; l'interrupteur
  « Événements du lab » les masque.
- Dans l'interface web, l'onglet **Métrologie** d'un routeur ou d'un client affiche son tableau de bord
  au thème de l'interface ; l'onglet **Flux**, le tableau « Flux réseau » filtré sur lui : sur le routeur
  (liste « Routeur », vue « Tous les ports ») ou, pour un client, sur le trafic qu'il émet ou reçoit (liste
  « Client / serveur »).
  L'icône de l'en-tête ouvre Grafana.
- Pour modifier un tableau de bord, connectez-vous avec le compte admin, éditez, enregistrez. La
  modification est gardée par Grafana tant que le fichier JSON du projet ne change pas. Pour l'intégrer au
  projet, exportez le JSON (Partager › Exporter) dans `monitoring/grafana/dashboards/` : Grafana relit ce
  dossier toutes les 30 s.

### 10.4 Mesures actives : latence et débit

**Sondes ping**, à chaque cycle. Chaque nœud envoie 5 pings, espacés de 0,2 s, vers chaque adresse du
lab des autres nœuds (management exclu). Pour chaque chemin : RTT min, moyen et max, écart-type,
gigue (moyenne des écarts entre RTT successifs), perte, joignabilité. Réglages dans `.env` :
`PROBE_SCOPE` (`all` ; `primary` pour la Loopback0 des routeurs et la première adresse des clients ;
`off`), `PROBE_COUNT`, `PROBE_INTERVAL`, `PROBE_MAX_TARGETS`.

**Débit, avec iperf3**, à la demande :

```bash
make iperf                                    # client1 → client2, 10 s
make iperf SRC=client2 DST=client1 DURATION=30
make iperf REVERSE=1                          # client2 émet vers client1 (iperf3 -R)
make iperf PARALLEL=4                         # 4 flux simultanés
```

iperf3 est installé dans les clients par `apk add` s'il manque : il faut un accès Internet, et
l'installation se perd au redéploiement. Le résultat s'affiche dans le terminal :

```
iperf3 : client1 → client2 (192.168.2.10), 10 s
iperf3 client1 → client2 (192.168.2.10) : 5,13 Gbit/s reçus, 30983 retransmission(s), RTT moyen 0,70 ms
```

Il apparaît aussi dans Grafana : débit par seconde, retransmissions et RTT dans les tableaux Métrologie
et Client Linux, annotation sur les graphes, et débit des ports traversés.

### 10.5 Interroger InfluxDB

L'interface d'InfluxDB (http://127.0.0.1:8086, compte `admin`) permet d'explorer les mesures et
d'écrire des requêtes Flux (Data Explorer). En ligne de commande :

```bash
# les commandes ci-dessous lisent les secrets du .env : set -a; . ./.env; set +a
# Flux, avec la CLI du conteneur
docker compose exec influxdb influx query 'from(bucket: "lab") |> range(start: -1m)
  |> filter(fn: (r) => r._measurement == "lab_node" and r._field == "up") |> last()'

# InfluxQL, par l'API : la requête est lue sur l'entrée standard, sans échappement
iq() { curl -sG http://127.0.0.1:8086/query -H "Authorization: Token $INFLUXDB_TOKEN" \
         --data-urlencode db=lab --data-urlencode q@- | jq -c '.results[0].series[]?'; }
iq <<'EOF'
SELECT last("state"), last("uptime_s") FROM "lab_bgp_peer" WHERE time > now() - 1m GROUP BY "node", "peer"
EOF
```

Autres requêtes InfluxQL utiles :

```sql
SHOW MEASUREMENTS
SHOW TAG VALUES FROM "lab_interface" WITH KEY = "interface" WHERE "node" = 'sonic1'
SELECT mean("rx_bps") FROM "lab_interface" WHERE "node" = 'sonic1' AND time > now() - 15m GROUP BY time(1m), "interface"
SELECT mean("rtt_avg_ms") FROM "lab_probe" WHERE "src" = 'client1' AND time > now() - 5m GROUP BY "target"
SELECT "node", "type", "message" FROM "lab_event" WHERE time > now() - 1h
```

### 10.6 Ajouter des mesures ou des panneaux

- **Une donnée de plus sur un routeur SONiC** : relevez-la dans `labmon/remote/sonic_collect.py` (une
  section indépendante, bibliothèque standard et redis-py seulement), convertissez-la en points dans
  `labmon/sonic.py`. Pour les autres nœuds : `labmon/remote/linux_collect.sh` et `labmon/linux.py`.
- **Vérifier** : `make test` (tests sur des sorties réelles du lab, dans `tests/fixtures/`), puis
  `make up` (reconstruit Telegraf) et `make collect`.
- **Une autre source** : ajoutez une entrée Telegraf dans `monitoring/telegraf/telegraf.conf`.
- **Un panneau** : créez-le dans Grafana, puis exportez le JSON (§ 10.3). `make smoke` exécute toutes
  les requêtes des tableaux de bord avec `scripts/check_dashboards.py` ; depuis l'hôte :
  `GRAFANA_URL=http://127.0.0.1:3000 python3 scripts/check_dashboards.py`.

### 10.7 Données, rétention, remise à zéro

- Les mesures sont gardées 7 jours (`INFLUXDB_RETENTION`) dans le volume `sonic-lab_influxdb-data`.
  Les réglages de Grafana sont dans `sonic-lab_grafana-data`.
- `make down` garde ces volumes : au `make up` suivant, l'historique est toujours là.
- Le lab de base produit environ 750 points, soit 9 000 valeurs, par cycle de 10 s (tableau
  Infrastructure). Comptez de l'ordre de 0,5 Go par jour avant qu'InfluxDB ne compacte ses fichiers,
  nettement moins ensuite.
- Pour changer la durée de conservation sans tout effacer :

  ```bash
  docker compose exec influxdb influx bucket update --id "$(docker compose exec -T influxdb influx bucket list -n lab --hide-headers | cut -f1)" --retention 14d
  ```
- Pour tout effacer, ou après avoir changé les comptes ou le jeton InfluxDB dans `.env` (pris en compte
  seulement au premier démarrage) :

  ```bash
  make down
  docker volume rm sonic-lab_influxdb-data sonic-lab_influxdb-config sonic-lab_grafana-data
  make up
  ```

### 10.8 Flux sFlow : les types de trafic

SONiC ne fait pas NetFlow ni IPFIX : son export de flux est **sFlow**, comme sur les commutateurs
réels. L'ASIC (ici, sa simulation dans SONiC-VS) échantillonne un paquet reçu sur N, et l'agent
`hsflowd` envoie l'en-tête du paquet (128 octets) au collecteur, avec le port d'entrée et le taux.
Le collecteur du lab, dans Telegraf, décode ces datagrammes, classe chaque paquet et écrit les mesures
`lab_flow` et `lab_flow_agent` (§ 10.2), affichées par le tableau « Flux réseau ».

```
client1 ──► sonic1 Ethernet0 ──► sonic1 (tc : 1 paquet sur 256 ──► psample ──► hsflowd)
                                     └── sFlow, UDP 6343, par eth0 ──► Telegraf 172.20.30.6 ──► InfluxDB
```

**Activer sFlow** : c'est la conf de la CD Ansible (§ 11), document `sonic-sflow` du projet
`network-config`. Elle est aussi dans les confs de démarrage du lab (`lab/configs/<routeur>/`, projet
`lab-infra`) depuis sa sauvegarde par « Sauvegarder running → startup » : sFlow repart seul après un
redéploiement.

| Élément | Valeur |
|---|---|
| sFlow global | up, compteurs toutes les 20 s, agent = adresse de Loopback0 |
| collecteur | `FLOW_COLLECTOR_IP`:6343 |
| sessions | les ports de `ports` des host_vars, à 1/256 ; tous les autres ports (`all`) à down |

```bash
make ansible PLAYBOOK=deploy.yml ARGS=--diff     # ou le pipeline de network-config
docker exec clab-sonic-lab-sonic1 show sflow     # état, collecteur
docker exec clab-sonic-lab-sonic1 show sflow interface | grep -E 'Ethernet(0|4) '
docker exec clab-sonic-lab-sonic1 tc filter show dev Ethernet0 ingress   # « sample rate 1/256 group 1 »
```

`sflowmgrd` lance `hsflowd` dès que sFlow passe à up, et l'arrête au retour à down. Deux entrées de
STATE_DB qu'il attend d'un vrai SONiC manquent à l'image ; le bootstrap des routeurs les écrit :
- `PORT_INDEX_TABLE` (index SONiC des ports, N + 1 pour EthernetN) ;
- `SYSTEM_READY`.

Le module noyau `psample` est chargé dans l'hôte au premier échantillonnage.

**Vérifier que sFlow fonctionne**, du routeur au tableau de bord :

| Où | Commande | Ce qu'on doit voir |
|---|---|---|
| conf du routeur | `docker exec clab-sonic-lab-sonic1 show sflow` | `sFlow Admin State: up`, collecteur `lab` 172.20.30.6:6343 |
| ports échantillonnés | `docker exec clab-sonic-lab-sonic1 show sflow interface` | Ethernet0 et Ethernet4 `up`, taux 256 |
| agent | `docker exec clab-sonic-lab-sonic1 pgrep -a hsflowd` | un processus `hsflowd` |
| ASIC (simulé) | `docker exec clab-sonic-lab-sonic1 tc filter show dev Ethernet0 ingress` | `sample rate 1/256 group 1` |
| datagrammes qui partent | `docker exec clab-sonic-lab-sonic1 tcpdump -i eth0 -n -c 5 udp port 6343` | `sFlowv5, IPv4 agent 10.255.0.1` vers 172.20.30.6, toutes les 20 s au moins (compteurs), plus souvent avec du trafic |
| échantillons décodés en direct | `docker exec clab-sonic-lab-sonic1 sh -c 'tcpdump -i eth0 -n -U -w - udp port 6343 2>/dev/null \| sflowtool -r - -l'` (Ctrl+C pour arrêter) | une ligne `FLOW` par paquet échantillonné : agent, port d'entrée (1 = Ethernet0, 5 = Ethernet4), MAC, IP source et destination, protocole, taille, taux 256 |
| collecteur | Grafana › « Flux réseau », ligne du haut | « Collecteur sFlow : en écoute », « Routeurs qui émettent : 2 », « Échantillonnage : 1 / 256 » |
| flux classés | `make traffic`, puis le même tableau | les applications (iperf3, http, dns, icmp) dans « Débit par application » |

`make smoke` fait la même vérification de bout en bout (étape « Flux sFlow »).

**Tester depuis les clients** : l'échantillonnage minimum du modèle YANG est d'un paquet sur 256. Il
faut donc générer du volume : un `curl` ou un `nslookup` isolé n'a presque aucune chance d'être
échantillonné. `make traffic` le fait pour vous :

```bash
make traffic                          # mix pendant 60 s : iperf3 TCP et UDP, HTTP, DNS, ICMP, en parallèle
make traffic TYPE=http DURATION=30    # un seul type : tcp, udp, http, dns, icmp
make traffic SRC=client2 DST=client1
```

Ce sont les mêmes commandes qu'on peut taper soi-même dans le terminal des clients (onglet CLI de
l'interface, ou `docker exec -it clab-sonic-lab-client1 sh`). Les outils s'installent par `apk`
(accès Internet par eth0) et disparaissent au redéploiement.

```sh
# client2 (serveur) : outils et serveurs
apk add -q iperf3 busybox-extras dnsmasq
iperf3 -s -D                                              # iperf3, port 5201
mkdir -p /tmp/www && dd if=/dev/zero of=/tmp/www/fichier.bin bs=1M count=64
busybox-extras httpd -p 80 -h /tmp/www                    # HTTP, port 80
dnsmasq --no-resolv --listen-address=192.168.2.10 --bind-interfaces --address=/lab.test/10.99.0.1

# client1 (client), chaque commande pendant 30 s environ
apk add -q iperf3 curl
iperf3 -c 192.168.2.10 -t 30 -b 200M                      # app « iperf3 », protocole tcp
iperf3 -c 192.168.2.10 -t 30 -u -b 50M                    # app « iperf3 », protocole udp
for i in $(seq 20); do curl -s -o /dev/null --limit-rate 25M http://192.168.2.10/fichier.bin; done   # « http »
for i in $(seq 20000); do nslookup -type=a h$i.lab.test 192.168.2.10 >/dev/null; done                # « dns »
ping -q -i 0.002 -s 1200 -w 30 192.168.2.10              # « icmp »
```

**Lire le tableau « Flux réseau »** (Grafana, ou onglet Flux d'un routeur ou d'un client) :

- **Listes du haut** : « Routeur » garde ce qu'échantillonnent les routeurs choisis ; « Client / serveur »
  garde le trafic dont la source ou la destination est l'un des nœuds choisis (adresse interne pour un
  paquet VXLAN). L'onglet Flux d'un client règle cette seconde liste sur lui. « Trafic » choisit ce que
  comptent la ligne du haut et la section « Trafic par application » (ci-dessous).

- **Trafic par application**, selon la liste « Trafic » :
  - « Entrée du réseau » (par défaut) : ce que les routeurs reçoivent sur leurs ports reliés à un client
    (`edge = yes`). Chaque paquet y est compté une fois, même s'il traverse plusieurs routeurs. Un
    routeur sans port vers un client (spine, routeur de cœur) n'y voit rien : panneaux
    vides quand il est seul sélectionné ;
  - « Tous les ports » : tout ce que reçoivent les routeurs choisis, transit compris. Un flux qui traverse
    plusieurs routeurs y est compté sur chacun. C'est la vue de l'onglet Flux d'un routeur.

  La section donne :
  - le débit par application et par protocole ;
  - la répartition sur la période ;
  - les conversations (source → destination, nœuds nommés).
- **Par routeur et par port** : tout ce que chaque port reçoit, avec le nœud relié. Un flux qui
  traverse deux routeurs y apparaît deux fois.
- **VXLAN** : dans une fabric VXLAN/EVPN, les spines ne voient que des tunnels (UDP 4789). Le collecteur
  décode le paquet interne et donne son application, par VNI.
- **Collecte sFlow** : datagrammes reçus par routeur, pertes (séquence de l'agent), datagrammes
  illisibles ou venus d'une autre source que l'IP de management d'un routeur.

L'application est déduite du port bien connu du serveur (http 80, https 443, dns 53, ssh 22, bgp 179,
iperf3 5201-5202, vxlan 4789…), sinon `autre-tcp` / `autre-udp`. Pour un paquet sans port, c'est le
protocole (`icmp`, `arp`…). Les volumes sont des **estimations** : taille du paquet échantillonné ×
256. Le panneau « Échantillons reçus » indique leur précision : plus il y a d'échantillons, plus
l'estimation est fiable.

```bash
# requête directe : trafic entrant par application sur 10 minutes
curl -s -G http://127.0.0.1:8086/query -H "Authorization: Token $INFLUXDB_TOKEN" --data-urlencode db=lab \
  --data-urlencode "q=SELECT sum(\"bytes\") FROM \"lab_flow\" WHERE \"edge\" = 'yes' AND time > now() - 10m GROUP BY \"app\""
```

Limites :
- échantillonnage en réception seulement (`sample_direction rx`), comme le permet SONiC-VS ;
- une conf sFlow changée par la CD n'est que dans la running : sauvegardez-la ensuite dans la conf
  de démarrage depuis l'interface (en mode GitOps, une merge request dans `lab-infra`) ;
- le collecteur n'accepte que les datagrammes venant de l'IP de management d'un routeur du lab
  (sources acceptées dès le démarrage d'après la topologie, puis à chaque cycle d'après la découverte),
  et plafonne le nombre de séries par cycle (`FLOW_MAX_KEYS`, 2000) ;
- `SFLOW_PORT=0` coupe la réception des flux.

---

## 11. GitLab

GitLab CE tourne dans le compose (service `gitlab`), sur le réseau de management du lab :

| | |
|---|---|
| Interface, git en HTTP | http://172.20.30.5 (`GITLAB_IP`) |
| git en SSH | `git@172.20.30.5:<groupe>/<projet>.git`, port 22 de l'IP de GitLab |
| Joignable depuis | l'hôte et les nœuds du lab ; sur l'hôte, SSH est aussi publié sur 127.0.0.1:2222 (`GITLAB_SSH_BIND`, `GITLAB_SSH_PORT`) |
| Données | volumes `gitlab-config`, `gitlab-data` et `gitlab-logs` |

Le premier démarrage prend 5 minutes environ (`docker compose logs -f gitlab`), les suivants 2 à 3.
GitLab est réglé pour un lab : environ 3 Go de RAM (Puma sans workers, Prometheus désactivé), aucun
mail envoyé, pas d'inscription libre.

### Comptes et groupe

À chaque démarrage, une fois GitLab configuré, `gitlab/provision/provision.rb` (lancé par
`gitlab-rails runner`) crée ce qui manque et l'écrit dans les logs, préfixé par `[sonic-lab]` :

| Élément | Par défaut | Variable |
|---|---|---|
| comptes | `lab` avec un mot de passe aléatoire (`make init`) | `GITLAB_USERS` |
| groupe | `ima-netw-automation-grp-3`, privé, ces comptes en Owner | `GITLAB_GROUP` |
| administrateur | `root`, mot de passe aléatoire (`make init`) | `GITLAB_ROOT_PASSWORD` |
| jeton d'API de root | aléatoire (`make init`), utilisé par gitlab-sync, le runner, l'agent GitOps et l'interface | `GITLAB_API_TOKEN` |

- Un compte existant n'est pas recréé. Le mot de passe du `.env` lui est appliqué tant qu'il ne s'est
  jamais connecté à l'interface ; ensuite, celui que l'utilisateur a choisi est conservé.
- GitLab refuse tout mot de passe de moins de 8 caractères, à la création **et à la connexion** : un
  compte dont le mot de passe est plus court est ignoré, avec une erreur dans les logs. Un mot de passe
  qui contient le nom d'utilisateur, refusé d'ordinaire par GitLab, est accepté : le provisioning
  l'impose.
- Le mot de passe de `root` n'est lu qu'au premier démarrage (volume vide). Pour le changer ensuite :
  `docker compose exec gitlab gitlab-rake "gitlab:password:reset[root]"`.
- Le jeton est valable un an et renouvelé au démarrage quand il expire dans moins de 30 jours. Un jeton
  révoqué dans GitLab n'est jamais réactivé : changez sa valeur dans `.env`, puis `make up` (l'ancien
  jeton du même nom est révoqué). Il sert aussi à vos scripts :
  `curl -H "PRIVATE-TOKEN: $GITLAB_API_TOKEN" http://172.20.30.5/api/v4/projects`.

### Dépôts versionnés avec le projet

Les volumes Docker restent sur la machine. Pour que les dépôts suivent le projet, le service
`gitlab-sync` les recopie dans `gitlab/repos/`, versionné avec le reste. Un bundle contient tout
l'historique du projet, branches comprises : relisez un export (`git diff --stat gitlab/repos/`) avant
de le committer.

```
gitlab/repos/<namespace>/<projet>.json     nom, description, visibilité, branche par défaut, refs
gitlab/repos/<namespace>/<projet>.bundle   branches et tags (git bundle) ; absent si le dépôt est vide
```

Au démarrage, puis toutes les `GITLAB_SYNC_INTERVAL` secondes, il compare GitLab, ces fichiers et
l'état du passage précédent :

| Situation | Effet |
|---|---|
| projet créé, push, branche ou tag, description changée dans GitLab | fichiers du projet réécrits |
| GitLab neuf : projet cloné sur un autre poste, volumes supprimés | projets absents recréés : groupes, branches, tags, branche par défaut |
| `git pull` du projet qui apporte le travail d'un autre poste | branches en avance, nouvelles branches et nouveaux tags poussés dans GitLab |
| même branche modifiée des deux côtés | version de GitLab conservée, avertissement dans les logs ; l'autre reste dans l'historique git du projet |
| projet supprimé dans GitLab | ses fichiers sont supprimés |
| fichier supprimé | réexporté au passage suivant : c'est dans GitLab qu'on supprime un projet |

- Ne voyagent pas : issues, merge requests, wiki, variables CI, membres des autres groupes. Un projet
  personnel n'est restauré que si son propriétaire figure dans `GITLAB_USERS`.
- Les fichiers appartiennent au propriétaire du dossier `gitlab/repos/`, pas à root.
- Un dernier export a lieu à l'arrêt (`make down`) ; `make gitlab-sync` en lance un tout de suite.
- Le jeton n'apparaît ni dans les URL ni dans les logs : git le reçoit en en-tête HTTP, par
  l'environnement.

```bash
make gitlab-sync                                    # synchronisation immédiate
docker compose logs -f gitlab-sync                  # exports, restaurations, avertissements
git clone gitlab/repos/<groupe>/<projet>.bundle   # un bundle se clone directement
```

### Depuis les nœuds du lab

Les routeurs et les clients joignent GitLab par leur interface de management (eth0) :

```bash
docker exec clab-sonic-lab-client1 sh -c 'apk add -q git && \
  git clone http://<compte>:<motdepasse>@172.20.30.5/<groupe>/<projet>.git'
```

### Runner et CI/CD réseau avec Ansible

Le service `gitlab-runner` (image GitLab Runner 19.6, `gitlab/runner/`) s'enregistre tout seul. Son
`entrypoint.sh` procède ainsi :

1. il attend l'API de GitLab avec `GITLAB_API_TOKEN`, comme gitlab-sync ;
2. il réutilise le jeton du runner gardé dans le volume `sonic-lab_gitlab-runner-config` s'il est
   encore valide (`POST /api/v4/runners/verify`). Sinon, après un GitLab neuf par exemple, il crée un
   runner d'instance (`POST /api/v4/user/runners`) ;
3. il aligne la description (`sonic-lab`) et les tags (`GITLAB_RUNNER_TAGS`, `sonic-lab,restconf`) du
   runner sur le `.env`, et supprime les runners homonymes orphelins ;
4. il régénère `config.toml` depuis le `.env`, puis lance `gitlab-runner run`.

| Réglage | Valeur |
|---|---|
| Executor | `docker` : un conteneur par job, image `GITLAB_RUNNER_IMAGE` (`python:3.12-alpine`) par défaut, `pull_policy` `if-not-present` : les jobs Ansible utilisent l'image locale `sonic-lab-ansible:latest`, construite par `make up` (`gitlab/ansible/`, service ponctuel `ansible` de compose) |
| Réseau des jobs | `sonic-lab-mgmt` : GitLab (`http://gitlab`) et le RESTCONF des routeurs. Pas de socket Docker, pas de mode privilégié, pas d'accès à l'API de l'interface |
| Clone | `http://gitlab`, par le réseau interne, quel que soit l'`external_url` de GitLab |
| Variables fournies aux jobs | `RESTCONF_USER`, `RESTCONF_PASSWORD` (non masquées dans les journaux), `FLOW_COLLECTOR_IP` (collecteur sFlow) |
| Jobs en parallèle | `GITLAB_RUNNER_CONCURRENT` (2) |
| Jobs acceptés | ceux de tous les projets, avec ou sans tag |

Le provisioning désactive aussi Auto DevOps : sans cela, chaque projet sans `.gitlab-ci.yml` lancerait
un pipeline voué à l'échec.

**Projet de CI/CD réseau.** `make ci-project` crée `<GITLAB_GROUP>/network-config` à partir du modèle
`gitlab/network-config/`. Le script refuse d'écraser un projet existant (`PROJECT=autre make
ci-project`). Il pousse le contenu avec `ci.skip` : la création ne déploie rien. gitlab-sync exporte
ensuite le projet dans `gitlab/repos/`.

C'est un projet **Ansible**. La conf des routeurs y est décrite en YAML, des templates Jinja2 la
traduisent en documents RESTCONF, et un module de la collection du projet applique ces documents
après les avoir fait valider par le routeur.

```
inventory/hosts.yml                 routeurs → IP de management (ansible_host) ; connexion locale
inventory/group_vars/all.yml        RESTCONF (identifiants du runner), ports par défaut, sFlow, VERIFY_TIMEOUT
inventory/host_vars/<routeur>.yml   ports, interfaces, loopback, bgp, verify.ping
roles/sonic_config/                 templates → documents ; rendu, sauvegarde, application
playbooks/render.yml                documents rendus dans build/<routeur>/ (artefact de lint)
playbooks/deploy.yml                sauvegarde dans rollback/ puis application, routeur par routeur
playbooks/verify.yml                voisins BGP Established, pings depuis les routeurs (sonic-lab:ping)
playbooks/rollback.yml              PUT des sauvegardes de rollback/
collections/ansible_collections/sonic_lab/restconf/   module sonic_lab.restconf.config
tests/                              pytest : module (faux routeur), rendu, syntaxe
.gitlab-ci.yml, ansible.cfg, .ansible-lint
```

Documents rendus par le rôle `sonic_config`, appliqués dans cet ordre :

| Document | Ressource | Méthode |
|---|---|---|
| `sonic-port` | `/restconf/data/sonic-port:sonic-port` | PATCH (fusion : description, `admin_status`, `mtu`) |
| `sonic-loopback-interface` | `…/sonic-loopback-interface:sonic-loopback-interface` | PUT (le module entier) |
| `sonic-interface` | `…/sonic-interface:sonic-interface` | PUT |
| `sonic-sflow-sessions`, `sonic-sflow` | `…/sonic-sflow:sonic-sflow` | PATCH des sessions, puis PUT (§ 10.8) |
| `frr.conf` | `…/sonic-lab:frr/running-config` | PUT (replace par blocs) |

Le module `sonic_lab.restconf.config` traite chaque document dans l'ordre :
1. il appelle l'opération `sonic-lab:preview` du routeur (YANG, GCU en `--dry-run`, `vtysh -C`) ;
2. il s'arrête si le routeur refuse le document ;
3. il ne fait rien si le preview ne voit aucun changement (opérations du JSON Patch pour la CONFIG_DB,
   diff et commandes pour FRR) ;
4. sinon il fait le PUT ou le PATCH.

En `--check`, seul le preview est fait ; `--diff` affiche les différences calculées par le routeur.
Le serveur répond 204 même quand rien ne change : c'est le preview qui donne `changed`, donc un
deuxième déploiement identique donne `changed=0`.

`ansible.netcommon.restconf_config` ne convient pas : en PUT, il n'envoie que la différence avec
l'existant, ce qui retire les autres tables du module.

| Job | Merge request, branche | Branche par défaut | Rôle |
|---|---|---|---|
| lint | ✔ | ✔ | `ansible-lint` (profil production), `render.yml` (documents dans `build/`, artefact), `pytest tests` |
| preview | ✔ | ✔ | `deploy.yml --check --diff` : chaque document validé par le routeur, différences affichées, rien d'appliqué |
| deploy | | ✔ | `deploy.yml --diff` : GET de l'état courant dans `rollback/` (artefact), puis application ; routeur par routeur, arrêt au premier échec |
| verify | ✔ | ✔ | `verify.yml` : voisins BGP des host_vars Established et pings de `verify.ping` sans perte, en réessayant jusqu'à `VERIFY_TIMEOUT` (180 s) |
| rollback | | si une étape échoue | `rollback.yml` : PUT des sauvegardes de `rollback/` |

`deploy`, `verify` et `rollback` partagent le `resource_group` `sonic-lab` : jamais deux pipelines sur
le lab en même temps. Le déploiement touche la running : sauvegardez ensuite dans la conf de démarrage
depuis l'interface pour qu'il survive à un redéploiement.

Les jobs désignent `ansible.cfg` par `ANSIBLE_CONFIG`. Le dossier d'un job docker de GitLab est
accessible en écriture à tous, et Ansible y ignorerait le fichier sans cela.

Depuis l'hôte, `make ansible` lance les mêmes playbooks sur le modèle `gitlab/network-config/`, dans
l'image des jobs, sur le réseau de management :

```bash
make ci-project                                    # crée le projet dans GitLab
make ansible                                       # deploy.yml --check --diff : le preview
make ansible PLAYBOOK=deploy.yml ARGS=--diff       # déployer
make ansible PLAYBOOK=verify.yml ARGS=             # vérifier
make ansible PLAYBOOK=render.yml ARGS=             # rendu dans gitlab/network-config/build/
make ansible ARGS="--check --diff --limit sonic1"  # un seul routeur
docker compose logs -f gitlab-runner               # enregistrement, jobs
```

Pour gérer un autre module SONiC (VLAN…), ajoutez un template et une entrée dans
`roles/sonic_config/defaults/main.yml` (`sonic_config_documents`), au format que renvoie un GET RESTCONF.

**Suivre un pipeline en direct.** Chaque job affiche la sortie d'Ansible au fil de l'eau. Ce qu'on y lit :

| Job | Ce que montre le journal |
|---|---|
| `preview` | différences calculées par chaque routeur, un bloc `=== <document> : <méthode> <ressource>` par document modifié, ou rien si tout est déjà conforme |
| `deploy` | les mêmes blocs, puis le `PLAY RECAP` : `changed` ne compte que les routeurs modifiés, et `changed=0` veut dire « déjà conforme » |
| `verify` | sessions BGP et pings |

Trois façons de suivre :

- **dans GitLab** : projet `network-config` › Build › Pipelines (ou le widget de la merge request),
  puis clic sur un job. Le journal défile en direct.
- **depuis le terminal**, avec `make ci-watch` :

  ```bash
  make ci-watch                  # dernier pipeline : suivi en direct s'il tourne, rejoué s'il est fini
  make ci-watch NEXT=1           # attend le prochain pipeline : lancez-le, puis faites git push
  make ci-watch REF=ma-branche   # dernier pipeline d'une branche (merge request)
  make ci-watch PIPELINE=42      # un pipeline précis ; LOGS=0 : états des jobs seulement
  ```

  Le script (`scripts/ci_watch.sh`) affiche le journal de chaque job dans l'ordre des étapes, puis le
  bilan des jobs. Il rend 0 si le pipeline réussit.
- **côté routeur** : chaque requête de la CD apparaît dans le syslog du routeur. On y voit les GET de
  la sauvegarde, un `sonic-lab:preview` par document, puis les PUT et PATCH, et les opérations de GCU
  (`Patch Applier`) :

  ```bash
  docker exec clab-sonic-lab-sonic1 tail -n0 -f /var/log/syslog | grep --line-buffered -E 'restconf|Patch Applier'
  #   … restconf 172.20.30.2 admin POST /restconf/operations/sonic-lab:preview 200 79 ms
  #   … restconf 172.20.30.1 admin PUT /restconf/data/sonic-sflow:sonic-sflow 201 5908 ms
  ```

  Dans l'interface web, c'est l'onglet **Logs** du routeur, filtré sur `restconf`.

**État après coup** :

| Question | Où regarder |
|---|---|
| qu'est-ce qui est déployé, et quand ? | GitLab › Operate › Environments › `lab` : commit, pipeline et date de chaque déploiement |
| quels documents ont été appliqués, et sur quel état ? | artefacts des jobs : `build/` (documents rendus, job `lint`) et `rollback/` (état d'avant, job `deploy`) |
| le réseau est-il conforme au dépôt ? | `make ansible` (preview depuis l'hôte) : `changed=0` partout = conforme ; sinon le diff montre ce qui a bougé (modification à la main, redéploiement) |
| la CD a-t-elle modifié la running ? | dans l'interface, l'indicateur « modifications non sauvegardées » d'un routeur |

Le dépôt du projet modèle reste dans ce projet (`gitlab/network-config/`). Le projet GitLab créé vit
ensuite sa vie, comme les autres dépôts de `gitlab/repos/`.

### Infra en GitOps (topologie et confs de démarrage)

L'infra du lab (`lab/sonic-lab.clab.yml` et `lab/configs/<routeur>/`) peut suivre un projet GitLab.
Une merge request la modifie ; la fusion dans la branche par défaut la déploie. Le déploiement est
fait par le lab lui-même, en **pull** : l'agent GitOps (`lab/gitops/`) tourne dans le service `clab`,
qui a déjà containerlab et la socket Docker. Les jobs CI restent sans privilège.

**Mise en place.**

```bash
make infra-project        # crée <GITLAB_GROUP>/lab-infra (ou GITOPS_PROJECT) depuis l'état actuel du lab
```

Le projet contient la topologie, les deux fichiers de chaque routeur, un `.gitlab-ci.yml` (job
`validate`) et `ci/validate.py`, copie du validateur du lab. La fusion n'y est permise que si le
pipeline réussit, statuts de l'agent compris. Son contenu étant celui du lab, l'agent l'**adopte** au
cycle suivant sans redéployer.

Tant que le projet n'existe pas, l'agent ne fait rien (`GITOPS_ENABLED=0` le désactive tout à fait),
et l'interface sauvegarde dans `lab/configs/` comme avant.

**Merge request.** Pour chaque nouveau commit de tête d'une MR, environ 20 s après le push :

1. l'agent extrait du commit la topologie et les confs, sans rien d'autre, dans un dossier de
   travail ;
2. il les valide avec **son** validateur (voir les règles plus bas). Il n'exécute jamais de code venu
   du dépôt ;
3. il calcule le plan : `containerlab deploy --dry-run -f json` par rapport au lab en marche, plus ce
   que containerlab ne voit pas (définitions de nœuds, confs de démarrage) ;
4. il commente la MR (une seule note, mise à jour à chaque push) avec :
   - le tableau des nœuds ajoutés, supprimés ou recréés (avec la raison), des liens et des confs de
     démarrage modifiées ;
   - les avertissements ;
   - un schéma Mermaid (ajoutés en vert, recréés en orange, confs restaurées en bleu) ;
5. il publie le statut `lab/plan`. Un plan refusé bloque la fusion.

Le plan est recalculé quand un autre commit est déployé entre-temps.

**Fusion.** Quand la branche par défaut a un nouveau commit et que son job `validate` a réussi
(sans CI, au bout de 90 s), l'agent :

1. publie le statut `lab/deploy` (en cours) et ouvre un déploiement GitLab (environnement `lab`) ;
2. valide de nouveau et recalcule le plan ;
3. sauvegarde `lab/sonic-lab.clab.yml` et `lab/configs/` dans `lab/.gitops/backups/` (les 20
   derniers). Il signale une **dérive** : des fichiers modifiés sur l'hôte hors Git ;
4. installe les fichiers dans `lab/`, en place : les confs sont montées fichier par fichier dans les
   routeurs, et un nouvel inode ne leur serait pas visible ;
5. détruit les nœuds dont la définition a changé (`containerlab destroy --node-filter`), puis
   réconcilie le lab (`containerlab deploy`, sans `--reconfigure`) ;
6. attend que tous les nœuds tournent et que les routeurs soient prêts (`/var/run/lab-ready`, au plus
   420 s). Il relance le bootstrap d'un routeur recréé qui ne l'a pas fait ;
7. remet dans leur nouvelle conf de démarrage les routeurs non recréés dont la conf a changé, par
   l'API de l'interface (`/api/nodes/<n>/config/restore`) ;
8. publie le résultat : statut, déploiement, commentaire sur la MR fusionnée, `lab/.gitops/state.json`.

Si une étape échoue, l'agent réinstalle la sauvegarde et réconcilie de nouveau : c'est le
**rollback**. Le commit en échec n'est pas retenté ; corrigez par une nouvelle MR. Pendant le
déploiement, `lab/.runner-status` vaut `deploying` et l'interface affiche le déploiement en cours.

**Ce que containerlab réconcilie, et le reste.** Vérifié sur containerlab 0.83 :

| Changement dans la topologie | Effet |
|---|---|
| nœud ajouté ou supprimé | créé ou détruit par containerlab |
| lien ajouté ou retiré | recâblé par containerlab ; un routeur SONiC concerné est **recréé** (pas d'interface à chaud) |
| image, `mgmt-ipv4`, `env`, `exec`, `binds` d'un nœud ou de son kind | ignoré par containerlab : l'agent **recrée** le nœud, et les routeurs SONiC qui lui sont reliés le sont avec lui |
| contenu de `configs/<routeur>/` | ignoré par containerlab : un routeur recréé le charge au démarrage, les autres sont restaurés sans coupure |

Un routeur recréé repart de sa conf de démarrage : sa conf running est perdue, et ses sessions BGP
sont coupées pendant 30 s environ.

**Règles du validateur** (`lab/gitops/validate.py`). Le projet en contient une copie pour la CI, mais
c'est celle du lab qui fait foi.

| Règle | Détail |
|---|---|
| nom et réseau | `name: sonic-lab` ; `mgmt` : `sonic-lab-mgmt`, 172.20.30.0/24, sans `ipv4-range` |
| IP de management | obligatoire, dans le sous-réseau, unique, ni .0, ni .1, ni .255, ni `GITLAB_IP` |
| kinds et images | `sonic-vs` et `linux` ; images de `GITOPS_ALLOWED_IMAGES` (`docker-sonic-vs:latest,alpine:3.22`) |
| clés | racine : `name`, `mgmt`, `topology` ; nœud : `kind`, `image`, `mgmt-ipv4`, `env`, `sysctls`, `binds`, `exec` ; pas de `defaults` |
| éléments du lab | routeurs : env `BGPD_GRACEFUL_RESTART_ARG` et `RESTCONF_*`, binds du bootstrap, de `restconf/`, `frr_config.py` et `parsers.py`, exec du bootstrap |
| confs de démarrage | binds `configs/<routeur>/config_db.json` et `frr.conf` vers leur cible, fichiers présents, JSON valide |
| binds | seulement ceux du lab et `configs/<nœud>/…` du nœud lui-même |
| liens | `nœud:ethN`, nœuds existants, extrémités uniques, `eth1` à `eth32` sur un routeur, pas de lien d'un nœud vers lui-même |

**Sauvegarde depuis l'interface.** En mode GitOps, « Sauvegarder running → startup » (onglets
Configuration et Sauvegardes d'un routeur, ou « Sauvegarder tout » du tableau de bord) procède ainsi :

- il lit la running des routeurs, mise en forme comme d'habitude ;
- il la compare aux fichiers de la branche par défaut ;
- s'il y a une différence, il crée une branche `interface/sauvegarde-<routeurs>-<date>`, un commit
  et une **merge request**, dont le lien s'affiche dans la notification ;
- sans différence, il ne crée rien ;
- si GitLab ne répond pas, il signale l'erreur et n'écrit rien en local.

Le tableau de bord affiche un badge « Infra GitOps · <commit> déployé », qui renvoie au projet.

**Rôles des deux projets.** `lab-infra` porte la topologie et les confs **de démarrage**, chargées au
déploiement. `network-config` porte la conf **running**, poussée par RESTCONF sans redéploiement. Ne
gérez pas le même élément dans les deux : un déploiement de `lab-infra` qui recrée un routeur efface
ce que `network-config` y avait poussé.

### Repartir de zéro

```bash
make down
docker volume rm sonic-lab_gitlab-config sonic-lab_gitlab-data sonic-lab_gitlab-logs
make up        # GitLab neuf : comptes et groupe recréés, dépôts restaurés depuis gitlab/repos/
```

Le runner s'enregistre à nouveau tout seul : GitLab refuse son ancien jeton. Son volume
(`sonic-lab_gitlab-runner-config`) peut rester.

Pour abandonner aussi les dépôts, supprimez-les dans GitLab avant `make down`, ou videz
`gitlab/repos/` après (en gardant son `README.md`).

---

## 12. Scénarios pas à pas

### 1. Couper et rétablir le lien entre les routeurs

- **Interface** : sonic1 › Configuration › CONFIG_DB, mode Merge, contenu
  `{"PORT": {"Ethernet4": {"admin_status": "down"}}}`, puis Aperçu et Pousser. Sur le tableau de bord,
  le lien devient rouge et eBGP passe à Active. Pour rétablir : Sauvegardes › « Restaurer la conf de
  démarrage », ou le même extrait avec `"up"`.
- **CLI** :
  ```bash
  docker exec clab-sonic-lab-sonic1 config interface shutdown Ethernet4
  docker exec clab-sonic-lab-sonic1 vtysh -c "show bgp summary"           # état Active
  docker exec clab-sonic-lab-client1 ping -c 2 -W 2 192.168.2.10          # échoue
  docker exec clab-sonic-lab-sonic1 config interface startup Ethernet4    # BGP revient en quelques secondes
  ```

### 2. Annoncer un nouveau réseau en BGP

```bash
docker exec clab-sonic-lab-sonic1 config loopback add Loopback1
docker exec clab-sonic-lab-sonic1 config interface ip add Loopback1 10.255.1.1/32
docker exec clab-sonic-lab-sonic1 vtysh -c 'conf t' -c 'router bgp 65001' \
    -c 'address-family ipv4 unicast' -c 'network 10.255.1.1/32'
docker exec clab-sonic-lab-sonic2 vtysh -c 'show ip route 10.255.1.1/32'   # « Known via bgp »
docker exec clab-sonic-lab-client2 ping -c 2 10.255.1.1
```

Dans l'interface, sonic1 › Configuration › FRR : ajoutez `  network 10.255.1.1/32` dans
`address-family ipv4 unicast`, puis Aperçu et Pousser (merge).

### 3. Route statique

```bash
docker exec clab-sonic-lab-sonic1 config route add prefix 10.50.0.0/24 nexthop 10.0.12.2
docker exec clab-sonic-lab-sonic1 vtysh -c 'show ip route 10.50.0.0/24'    # « Known via static »
docker exec clab-sonic-lab-sonic1 config route del prefix 10.50.0.0/24 nexthop 10.0.12.2
```

### 4. VLAN avec interface routée (SVI)

```bash
docker exec clab-sonic-lab-sonic1 config vlan add 100
docker exec clab-sonic-lab-sonic1 config vlan member add -u 100 Ethernet8
docker exec clab-sonic-lab-sonic1 config interface ip add Vlan100 192.168.100.1/24
docker exec clab-sonic-lab-sonic1 show vlan brief
```

Ethernet8 n'est pas câblé. Pour l'utiliser vraiment, ajoutez un lien (scénario 8).

### 5. Générer du trafic et observer les débits

```bash
make iperf DURATION=30
```

Pendant le test, sonic1 › État affiche plusieurs Gbit/s dans la colonne « Débit RX / TX »
d'Ethernet0 et d'Ethernet4. Après coup, sonic1 › Métrologie montre la montée en débit des deux ports
et l'annotation du test. Le tableau Grafana « Métrologie » donne le débit seconde par seconde, les
retransmissions et le RTT.

### 6. Expérimenter puis revenir en arrière

1. sonic1 › Sauvegardes › Créer le snapshot, libellé « avant test ».
2. Modifiez librement, en CLI ou depuis l'interface.
3. Pour revenir en arrière : « Comparer » pour voir les écarts, puis ↺ pour restaurer le snapshot en
   mode replace. Ou « Restaurer la conf de démarrage ».

### 7. Rendre une modification permanente

1. Faites la modification, par exemple le scénario 2.
2. Sur la page du routeur, cliquez « Sauvegarder running → startup ». Les fichiers de
   `lab/configs/<routeur>/` sont mis à jour : vérifiez avec `git diff lab/configs`.
3. `make redeploy` : la modification est rechargée au démarrage.

### 8. Ajouter un lien ou un nœud

Ajoutez dans `lab/sonic-lab.clab.yml` un second lien entre les routeurs :

```yaml
  links:
    # …liens existants…
    - endpoints: ["sonic1:eth3", "sonic2:eth3"]      # eth3 = Ethernet8 des deux côtés
```

Puis :

```bash
make redeploy
docker exec clab-sonic-lab-sonic1 config interface startup Ethernet8
docker exec clab-sonic-lab-sonic1 config interface ip add Ethernet8 10.0.12.5/30
docker exec clab-sonic-lab-sonic2 config interface startup Ethernet8
docker exec clab-sonic-lab-sonic2 config interface ip add Ethernet8 10.0.12.6/30
docker exec clab-sonic-lab-sonic1 ping -c 2 10.0.12.6
```

Ajoutez ensuite le voisin BGP dans FRR si besoin, puis « Sauvegarder running → startup ». Un nouveau
client se déclare comme `client1` dans la topologie : `kind: linux`, `mgmt-ipv4` libre, et `exec` pour
le MTU, l'IP et les routes.

La métrologie suit sans réglage : le nouveau lien apparaît dans « Liens de la topologie », Ethernet8
devient câblé et ses débits s'affichent, les nouvelles adresses deviennent des cibles des sondes, et
un nouveau nœud s'ajoute aux listes des tableaux de bord. L'interface aussi : le nœud a sa carte et
sa page, et la topologie du tableau de bord se redessine avec lui.

### 9. Voir un ajout apparaître dans la métrologie

Ouvrez le tableau Grafana « Métrologie » (ou sonic1 › Métrologie), puis :

```bash
docker exec clab-sonic-lab-sonic1 config loopback add Loopback1
docker exec clab-sonic-lab-sonic1 config interface ip add Loopback1 10.255.1.1/32
```

Au cycle suivant (10 s au plus) :

- Loopback1 et son adresse apparaissent dans les interfaces de sonic1, et la table LOOPBACK_INTERFACE
  de CONFIG_DB passe de 2 à 4 clés ;
- 10.255.1.1 devient une cible de toutes les sondes : client1 la joint (sonic1 est sa passerelle),
  client2 et sonic2 non (100 % de perte), car elle n'est pas encore annoncée.

Annoncez-la en BGP :

```bash
docker exec clab-sonic-lab-sonic1 vtysh -c 'conf t' -c 'router bgp 65001' \
    -c 'address-family ipv4 unicast' -c 'network 10.255.1.1/32'
```

Les sondes passent au vert, les préfixes envoyés par sonic1 passent de 4 à 5, et la route
10.255.1.1/32 via 10.0.12.1 apparaît dans la table de routage de sonic2. Revenez en arrière avec
« Restaurer la conf de démarrage » sur sonic1 : Loopback1 disparaît des mesures et n'est plus sondée.
La session BGP, recréée par le mode replace, laisse un événement « réinitialisé puis rétabli » dans les
annotations.

### 10. Changer la conf des routeurs par la CD Ansible

Exemple : changer la description du lien de sonic1 vers sonic2.

1. `make ci-project` (une fois), puis cloner `<groupe>/network-config`.
2. Sur une branche, modifier `inventory/host_vars/sonic1.yml` :

   ```yaml
   ports:
     - {name: Ethernet0, description: client1}
     - {name: Ethernet4, description: "lien vers sonic2 (eBGP)"}
   ```

3. Pousser et ouvrir une merge request. Le job `lint` publie les documents rendus (`build/`). Le job
   `preview` affiche ce que sonic1 changera (`description` d'Ethernet4 dans `sonic-port`), et le
   routeur valide la modification (modèle YANG, GCU en simulation). Le job `verify` contrôle l'état
   actuel.
4. Fusionner. `deploy` sauvegarde l'état des deux routeurs et applique la modification (seul
   `sonic-port` change sur sonic1). `verify` attend ensuite les sessions BGP et les pings. Un échec
   déclenche `rollback`.

Pour essayer sans GitLab, depuis le projet du lab, sur le modèle `gitlab/network-config/` :
`make ansible` (preview), puis `make ansible PLAYBOOK=deploy.yml ARGS=--diff`.

### 11. Voir les types de trafic dans Grafana (sFlow)

1. Activer sFlow (si ce n'est pas déjà fait par le pipeline) : `make ansible PLAYBOOK=deploy.yml
   ARGS=--diff`. `docker exec clab-sonic-lab-sonic1 show sflow` affiche « sFlow Admin State: up » et le
   collecteur 172.20.30.6.
2. Ouvrir Grafana › « Flux réseau » (ou l'onglet **Flux** de sonic1 dans l'interface).
3. `make traffic` : 60 s de iperf3 (TCP et UDP), HTTP, DNS et ICMP de client1 vers client2. Une
   annotation marque le début et la fin du test.
4. En une dizaine de secondes, « Débit par application » montre iperf3, http, dns et icmp, et
   « Conversations » montre client1 ↔ client2. « Par routeur et par port » montre le même trafic sur
   Ethernet0 (depuis le client) et Ethernet4 (entre routeurs) des deux routeurs.
5. Refaire l'expérience à la main dans le terminal des clients (§ 10.8), par exemple un téléchargement
   HTTP en boucle : seule l'application « http » monte.

---

## 13. Tests

```bash
make test          # tests du backend, du collecteur, de gitlab-sync, de l'agent GitOps et du projet Ansible
                   # (uv, Python 3.12), puis du RESTCONF (Python 3.11, celui des routeurs), sans toucher au projet
make test-docker   # mêmes tests dans Docker (stages « test » de web/, monitoring/telegraf/, gitlab/sync/,
                   # lab/sonic/restconf/, lab/gitops/ et gitlab/ansible/, qui passe aussi ansible-lint)
make smoke         # bout en bout sur le lab déployé
```

- **Tests du backend** : moteurs de diff CONFIG_DB et FRR, parseurs, topologie, stockage, toutes les
  routes de l'API avec un faux Docker, sécurité.
- **Tests du collecteur** (`monitoring/telegraf/collector/tests/`) : encodage line protocol, analyse
  des sorties réelles d'un routeur et d'un client (`tests/fixtures/`), débits et remises à zéro,
  sondes (busybox et iputils), événements BGP, cycle complet avec un faux Docker. Flux sFlow :
  décodage de datagrammes réels de hsflowd (`tests/fixtures/sflow_*.hex`) et construits (VLAN, IPv6,
  fragments, VXLAN, datagrammes invalides), classement par application, agrégation, sources rejetées,
  plafond de séries, nommage des routeurs, ports et voisins, écoute UDP réelle sur 127.0.0.1 ;
  commande `traffic` avec un faux Docker.
- **Tests du projet Ansible** (`gitlab/network-config/tests/`, aussi lancés par son job `lint`) :
  module `sonic_lab.restconf.config` avec un faux routeur (preview, `changed` d'après le JSON Patch,
  `--check`, arrêt au premier refus, erreurs RESTCONF), rendu des documents depuis les host_vars
  (JSON valide, adresses sur des ports déclarés, sessions sFlow, conf FRR), syntaxe des playbooks.
- **Tests de l'agent GitOps** (`lab/gitops/tests/`) : validateur (vraie topologie du lab, puis
  chaque règle enfreinte), plan (sortie réelle de containerlab, nœuds redéfinis et cascade, confs de
  démarrage), agent avec un faux GitLab, un faux containerlab et une fausse interface : adoption,
  plan d'une MR, déploiement, recréation forcée, restauration sans containerlab, CI en attente ou en
  échec, rollback, dérive, interruption, extraction sûre de l'archive, écriture en place.
- **Tests du RESTCONF** (`lab/sonic/restconf/tests/`) : chemins RFC 8040, arbres YANG (PUT, PATCH,
  POST, DELETE, depth, content), JSON Patch de la CONFIG_DB, messages de libyang et de GCU, module
  `sonic-lab` (vérifié par `pyang --strict`), séquence vtysh du replace FRR. Le service complet et le
  serveur HTTP réel (authentification, TLS) tournent contre un faux routeur.
- **Tests de gitlab-sync** (`gitlab/sync/tests/`) : fusion à trois des refs, format des fichiers,
  client de l'API (serveur HTTP local), et cycles complets entre deux « postes » simulés (faux GitLab,
  vrais dépôts git) : export, restauration sur un GitLab neuf, import après un `git pull`,
  divergences, suppressions, branche par défaut.
- **`make smoke`**, sans laisser de trace :
  1. 4 nœuds prêts, BGP établi, ping client1 → client2 ;
  2. métrologie : InfluxDB et Grafana répondent, 7 tableaux de bord, les 4 nœuds, la sonde
     client1 → client2 et la session BGP relevés dans InfluxDB ;
  3. snapshot ;
  4. shutdown d'Ethernet4 : BGP tombe, le ping échoue, le diff le détecte, la métrologie voit la
     session tomber et enregistre l'événement ;
  5. restauration du snapshot de l'étape 3 (la running d'avant le test, même non sauvegardée) : tout
     revient, y compris dans la métrologie ;
  6. aperçu d'un replace FRR, et refus d'une config invalide ;
  7. sauvegarde sans modification des fichiers (sautée si la running diffère de la conf de démarrage) ;
  8. terminal web ;
  9. matrice de connectivité ;
  10. RESTCONF des deux routeurs : découverte, 401 sans identifiants, yang-library, lecture, PATCH
      appliqué par GCU puis annulé, valeur refusée par les modèles YANG, `preview` d'une conf FRR
      invalide, état BGP, opération ping ;
  10 bis. flux sFlow : STATE_DB préparée par le bootstrap ; sFlow de la CD utilisé s'il est actif,
      sinon conf de test posée par RESTCONF puis retirée ; ping de client1 échantillonné par sonic1,
      classé `icmp` et nommé dans `lab_flow` ; collecteur en écoute ;
  11. toutes les requêtes de tous les tableaux de bord Grafana (`scripts/check_dashboards.py`) ;
  12. GitLab : jeton d'API, comptes Owner du groupe, accès depuis client1, SSH ; aller-retour d'un
      projet de test (push avec le mot de passe du `.env`, export dans `gitlab/repos/`, suppression
      définitive, fichiers retirés) ;
  13. runner et CI/CD : runner « sonic-lab » en ligne, projet temporaire créé par le script de
      `make ci-project`, vrai pipeline Ansible en lecture seule (lint, preview, verify) sur une
      branche, projet supprimé ;
  14. infra en GitOps, si le projet `lab-infra` existe : agent à jour sur la branche par défaut, MR
      de test qui ajoute un client ; plan publié (`lab/plan`, commentaire avec schéma), puis MR et
      branche supprimées **sans fusion** ;
  15. nettoyage.

  Options : `API=http://hôte:port`, `SMOKE_AUTH=user:motdepasse`, `INFLUX=http://hôte:8086`,
  `GRAFANA=http://hôte:3000`, `INFLUXDB_TOKEN=…`, `GITLAB=http://172.20.30.5`, `GITLAB_API_TOKEN=…`,
  `RESTCONF=https://172.20.30.11/restconf`, `RESTCONF_USER=…`, `RESTCONF_PASSWORD=…` (lus aussi
  dans `.env`, comme `WEB_AUTH_*` pour `SMOKE_AUTH`).

---

## 14. Dépannage

| Symptôme | Cause probable | Que faire |
|---|---|---|
| `lancez docker compose depuis la racine du projet` | `docker compose` lancé ailleurs | `cd` à la racine ou utilisez `make` |
| L'interface ne répond pas sur :8080 | service `web` arrêté ou port pris | `make ps`, `docker compose logs web` ; `WEB_PORT=8081` dans `.env` |
| `web` redémarre en boucle : « interface exposée … sans authentification » | `WEB_BIND` hors de la boucle locale, ou nom non local dans `WEB_ALLOWED_HOSTS`, sans `WEB_AUTH_*` | `WEB_AUTH_USER` et `WEB_AUTH_PASSWORD` dans `.env`, puis `make up` |
| `make up` : « required variable … is missing a value » | `.env` absent ou incomplet (secret vide) | `make init` (nouveau `.env`), ou remplissez la variable citée |
| Bandeau « Échec du déploiement », « empreinte … inattendue » | `docker-sonic-vs.gz` n'est pas la build testée | vérifiez le fichier ([Image SONiC](#image-sonic)) ; autre build voulue : `SONIC_ARCHIVE_SHA256` dans `.env` |
| Bandeau « Échec du déploiement » | erreur containerlab, ou topologie refusée par le runner (fichier illisible, `name` autre que `sonic-lab`) | « Logs du déploiement » (ou `docker compose logs clab`), corrigez puis `make redeploy` |
| « Démarrer le lab » grisé dans « Actions du lab », « Service clab absent » | le conteneur du service `clab` n'existe pas (après `make down`) | `make up` |
| Routeur « Erreur : SONiC ne tourne pas dans ce conteneur » | conteneur redémarré hors containerlab | `make redeploy` |
| Routeur « Attention : bootstrap non terminé » | bootstrap en échec ou interrompu | « Relancer le bootstrap », ou `docker exec clab-sonic-lab-sonic1 bash /usr/local/bin/lab-bootstrap.sh` |
| Config BGP disparue | `bgpd` redémarré | relancer le bootstrap (§ 4.5) |
| Session BGP pas Established | port down, IP manquante, config FRR | `show interfaces status`, `show ip interfaces`, `vtysh -c "show ip bgp neighbors 10.0.12.2"` (dernier reset), `ping 10.0.12.2` depuis sonic1 |
| Ping client → client en échec | route, passerelle ou lien | sur le client : `ip route`, `ping 192.168.1.1` ; sur les routeurs : `show ip route`, état des liens dans le tableau de bord |
| Ping OK mais transferts bloqués | MTU incohérent | MTU du client : `ip -o link show eth1` (attendu 9100) |
| `hôte non autorisé` (HTTP 400) | Host absent de `WEB_ALLOWED_HOSTS` | ajoutez le nom ou l'IP dans `.env`, puis `make up` |
| `origine refusée` (HTTP 403), terminal qui se ferme aussitôt | page servie depuis une autre origine (autre nom d'hôte, proxy qui réécrit Host) | ouvrez l'interface par son URL directe ; un reverse proxy doit transmettre l'en-tête Host d'origine |
| « modifications non sauvegardées » persiste après restauration | champ ajouté dans une table système (fusion seulement) | cliquez le badge pour voir le diff ; retirez le champ avec `sonic-db-cli … HDEL`, ou sauvegardez |
| Conteneurs `clab-sonic-lab-*` restés après un incident (alerte « Des conteneurs du lab sont restés après l'arrêt ») | destroy interrompu (arrêt brutal) | rien à faire : le runner supprime tous les conteneurs du lab avant de déployer (« Démarrer le lab », `make start` ou `make redeploy`). À la main : `docker rm -f $(docker ps -aq --filter label=containerlab=sonic-lab)` |
| « The 'sonic-lab' lab has already been deployed » | conteneurs d'une version précédente de la topologie (fichier changé pendant que le lab tournait) qu'un runner d'avant cette version n'a pas supprimés | `make redeploy` (le runner actuel les supprime avant de déployer), sinon la commande `docker rm -f` ci-dessus |
| Recharger l'image SONiC | image corrompue ou remplacée | `make down`, `docker image rm docker-sonic-vs:latest`, `make up` |
| Onglet Métrologie : « Grafana ne répond pas » | service `grafana` arrêté ou encore en démarrage | `make ps`, `docker compose logs grafana` |
| Tableaux de bord vides ou « No data » | collecte arrêtée, ou jeton InfluxDB changé dans `.env` après le premier démarrage | `docker compose logs telegraf`, `make collect` ; pour un jeton changé : § 10.7 |
| Un nœud absent des tableaux de bord | conteneur arrêté, ou collecte en échec sur ce nœud | tableau Infrastructure › « État de la collecte » (détail de l'erreur) |
| Port 3000 ou 8086 déjà pris | autre service sur l'hôte | `GRAFANA_PORT` ou `INFLUXDB_PORT` dans `.env`, puis `make up` |
| Cycle de collecte plus long que 10 s | lab agrandi, sondes nombreuses | `PROBE_SCOPE=primary` ou `TELEGRAF_INTERVAL=30s` dans `.env` |
| Nombre de processus d'un client qui augmente sans cesse (Client Linux › Processus) | processus zombies : chaque commande de l'interface web passe par le `timeout` de busybox, et le PID 1 des clients (`/bin/sh`) ne les récolte pas | sans effet immédiat ; un redéploiement les efface |
| `make up` : `network sonic-lab-mgmt was found but has incorrect label` | réseau créé par containerlab avant que compose ne le gère (lab non détruit par une version précédente) | `docker network rm sonic-lab-mgmt` (après `make down`), puis `make up` |
| http://172.20.30.5 ne répond pas | GitLab encore en démarrage (5 min au premier) | `docker compose logs -f gitlab` ; `make ps` (état `healthy` attendu) |
| Un compte GitLab n'existe pas ou refuse son mot de passe | erreur du provisioning (mot de passe de moins de 8 caractères, format de `GITLAB_USERS`), ou compte déjà utilisé : son mot de passe n'est plus celui du `.env` | `docker compose logs gitlab \| grep sonic-lab` ; `docker compose exec gitlab gitlab-rake "gitlab:password:reset[<compte>]"` |
| `gitlab-sync` : « en attente de GitLab : jeton refusé » qui dure | `GITLAB_API_TOKEN` changé ou pas de 20 caractères, provisioning en échec | `docker compose logs gitlab \| grep sonic-lab`, corrigez `.env`, `make up` |
| `gitlab-sync` : « divergente », « refusée par GitLab » | la même branche a avancé dans GitLab et dans `gitlab/repos/` (deux postes) | la version de GitLab est gardée ; récupérez l'autre dans l'historique git du projet (`git show <commit>:gitlab/repos/…bundle > autre.bundle`, puis `git fetch autre.bundle`) |
| `gitlab-sync` : « utilisateur … absent de GitLab » | projet personnel d'un compte absent de `GITLAB_USERS` | ajoutez le compte à `GITLAB_USERS`, `make up` |
| RESTCONF injoignable (`https://172.20.30.11` ne répond pas) | serveur non lancé (bootstrap pas terminé) ou en erreur | `docker exec clab-sonic-lab-sonic1 supervisorctl status restconf` ; `grep restconf /var/log/syslog` dans le routeur ; relancer le bootstrap |
| RESTCONF : 400 « Invalid … » avec un `error-path` | donnée refusée par les modèles YANG (type, leafref, must) | corrigez la donnée au chemin indiqué. Un chemin hors de la ressource écrite signale une CONFIG_DB déjà non conforme (écrite par la CLI ou l'interface) : corrigez ce champ |
| RESTCONF : 400 « refusé par GCU » | GCU n'a pas su ordonner ou appliquer le patch | rejouez en `sonic-lab:preview` pour voir le patch ; `config apply-patch --dry-run -v` dans le routeur pour le détail |
| RESTCONF : 401 | identifiants différents de ceux des routeurs | `RESTCONF_USER` / `RESTCONF_PASSWORD` du `.env`. Après un changement : `make up` (le lab est redéployé) |
| `gitlab-runner` : « en attente de l'API GitLab » qui dure | GitLab en démarrage, ou `GITLAB_API_TOKEN` refusé | `docker compose logs gitlab \| grep sonic-lab` ; corrigez `.env`, `make up` |
| Job CI bloqué en « pending » | runner hors ligne, ou job avec un tag inconnu | Admin › CI/CD › Runners (runner `sonic-lab`), `docker compose logs gitlab-runner` ; tags acceptés : `GITLAB_RUNNER_TAGS`, jobs sans tag aussi |
| Job CI : « routeur injoignable (https://172.20.30.x/restconf) » | job hors du réseau de management, ou routeur en redéploiement | `docker exec sonic-lab-gitlab-runner-1 grep network_mode /etc/gitlab-runner/config.toml` ; attendre la fin du redéploiement |
| Job CI : « pull access denied for sonic-lab-ansible » | image des jobs pas construite (compose lancé sans le service `ansible`) | `make up` (ou `docker compose build ansible`) ; `docker images sonic-lab-ansible` |
| Job `preview` ou `deploy` : « … refusé par le routeur : … » | document invalide pour les modèles YANG, GCU ou `vtysh -C` (erreur et chemin dans le message) | corrigez les host_vars ; `make ansible` reproduit le preview depuis l'hôte, `make ansible PLAYBOOK=render.yml ARGS=` montre les documents rendus (`gitlab/network-config/build/`) |
| Job `lint` : « ansible.cfg ignoré » ou module `sonic_lab.restconf.config` introuvable | `ANSIBLE_CONFIG` absent (dossier du job accessible en écriture à tous) | gardez `ANSIBLE_CONFIG: $CI_PROJECT_DIR/ansible.cfg` dans `.gitlab-ci.yml` |
| Tableau « Flux réseau » vide | sFlow pas déployé, pas de trafic, ou collecteur pas en écoute | `docker exec clab-sonic-lab-sonic1 show sflow` (admin up, collecteur `FLOW_COLLECTOR_IP`) ; panneau « Collecteur sFlow » ; `make traffic` (il faut du volume : 1 paquet sur 256) |
| sFlow configuré mais aucun échantillon | `hsflowd` absent (`pgrep hsflowd`), STATE_DB non préparée (bootstrap ancien), ou filtres `tc` absents | `docker exec clab-sonic-lab-sonic1 sonic-db-cli STATE_DB HGET 'PORT_INDEX_TABLE\|Ethernet0' index` (attendu : 1) et `HGET 'SYSTEM_READY\|SYSTEM_STATE' Status` (UP) ; sinon relancez le bootstrap. `tc filter show dev Ethernet0 ingress` doit montrer `sample rate 1/256` |
| « Pertes, erreurs et sources rejetées » : des rejetés | datagrammes sFlow venus d'une autre IP que celle de management d'un routeur (essai à la main, job CI) | normal pour une source hors lab ; un routeur rejeté a une IP de management absente de la découverte (`make collect`) |
| `syncd`/`orchagent` : « sflowAddPort: Failed » ou « sflowDelPort: Failed » dans le syslog des routeurs | sFlow activé avant que les sessions des ports soient prises en compte : sflowmgrd échantillonne d'abord tous les ports, et les 30 ports non câblés échouent, puis sont retirés. Au démarrage d'un routeur dont la conf de démarrage contient sFlow, c'est systématique (environ 60 lignes, une fois) : sflowmgrd lit `SFLOW` avant `SFLOW_SESSION` | sans conséquence : seuls Ethernet0 et Ethernet4 restent échantillonnés (`tc filter show dev Ethernet8 ingress` est vide). La CD, elle, pose les sessions avant d'activer sFlow et n'en produit pas |
| Badge GitOps absent, `lab/.gitops/state.json` en `inactive` | projet de l'infra absent, ou autre nom que `GITOPS_PROJECT` | `make infra-project` ; `docker compose logs clab \| grep gitops` |
| Pas de commentaire de plan sur une MR | agent arrêté, ou MR vers une autre branche que celle par défaut | `docker compose logs clab \| grep gitops` ; `GITOPS_ENABLED` ; le plan arrive environ 20 s après le push |
| `lab/plan` refusé | règle du validateur (voir § 11), ou erreur de containerlab (détail dans le commentaire) | corrigez la topologie dans la MR |
| `lab/deploy` en échec, « Rollback effectué » | containerlab, routeur pas prêt ou restauration en échec : le lab est revenu au commit précédent | lisez le commentaire de la MR et `docker compose logs clab` ; corrigez par une nouvelle MR (le commit en échec n'est pas retenté) |
| `lab/deploy` : « Rollback en échec » | le lab est dans un état intermédiaire | `make redeploy` (déploie les fichiers en place), puis une nouvelle MR |
| Commentaire « Dérive locale » | fichiers de `lab/` modifiés sur l'hôte hors Git (sauvegarde locale, édition) | la version locale est dans `lab/.gitops/backups/` ; reportez-la par une MR si elle doit rester |
| Sauvegarde de l'interface : « merge request impossible » | GitLab injoignable depuis le conteneur `web`, ou jeton refusé | `make ps`, `docker compose logs web` ; rien n'est écrit en local |
| Redéploiement en échec, « address already in use » sur une IP de management | un conteneur de job a pris l'IP d'un nœud pendant que le lab était détruit (pas de plage d'IP réservée : containerlab la refuse) | attendre la fin des jobs en cours, puis `make redeploy` |

Diagnostic rapide d'un routeur :

```bash
docker exec clab-sonic-lab-sonic1 bash -c 'supervisorctl status | grep -v RUNNING; \
  sonic-db-cli CONFIG_DB GET CONFIG_DB_INITIALIZED; ls /var/run/lab-ready; \
  show interfaces status | head -4; vtysh -c "show bgp summary"'
```

Certains services non RUNNING sont **normaux**, il ne faut pas s'en inquiéter :

- STOPPED : `arp_update`, `gbsyncd`, `gearsyncd`, `redis-chassis` ;
- EXITED : `start.sh`, `zsocket`, `restore_neighbors`, `dependent-startup`, `waitfor_lldp_ready`,
  `start-snmp`, `wait-for-snmp-config`.

Un service clé (orchagent, syncd, zebra, bgpd…) en FATAL, BACKOFF ou EXITED est en revanche anormal :
le routeur passe en « Erreur » dans l'interface.

### Limites connues

- Redémarrer un conteneur SONiC à la main (`docker restart`) ne relance pas SONiC : containerlab ne
  démarre supervisord qu'au déploiement. Redéployez (`make redeploy` ou le bouton de l'interface).
- Le routeur expose 32 ports, dont seuls Ethernet0 (eth1) et Ethernet4 (eth2) sont câblés. Un lien
  `sonicX:ethN` ajouté à la topologie correspond au port `Ethernet{4×(N-1)}`, à passer `admin_status: up`.
- Le mode replace FRR est une version simplifiée par blocs : l'image ne fournit pas `frr-reload.py`.
- `config reload` est à proscrire dans le routeur (monit et systemd absents). `config apply-patch`
  (GCU), sur lequel s'appuie le RESTCONF, fonctionne depuis que le bootstrap retire `switch_type: switch`.
- Redémarrer `bgpd` seul efface la config BGP : relancez ensuite le bootstrap.
- LLDP et SNMP ne sont pas opérationnels dans cette image ; `show lldp …` et `show services` échouent.
  La métrologie n'en a pas besoin.
- `show interfaces counters` fonctionne, mais ses colonnes de débit restent à N/A ; les compteurs de
  RIF et de files d'attente restent à zéro dans SONiC-VS.
- FRR ne journalise rien : les événements BGP de la métrologie viennent de la comparaison de deux relevés.
- Traceroute n'existe que sur les clients ; l'image SONiC n'embarque que `ping`.
- Flux : sFlow seulement (pas de NetFlow/IPFIX), 1 paquet reçu sur 256 au mieux dans SONiC-VS. Les
  débits sont des estimations et un trafic bref passe inaperçu (§ 10.8).

---

## 15. Sécurité

L'interface a accès à la socket Docker, ce qui équivaut à un accès root sur l'hôte. Les nœuds
containerlab sont de plus des conteneurs privilégiés. Protections en place :

- **Écoute sur 127.0.0.1** par défaut (`WEB_BIND`).
- **En-tête Host vérifié** (`WEB_ALLOWED_HOSTS`) : bloque le DNS rebinding, où un site tiers ferait
  pointer son nom vers 127.0.0.1.
- **En-tête Origin vérifié** pour les WebSockets et les requêtes modifiantes (POST, DELETE…). Un site
  ouvert dans le navigateur ne peut ni ouvrir un terminal ni déclencher une action. Les clients sans
  Origin (curl, scripts) restent acceptés.
- **HTTP Basic** (`WEB_AUTH_USER` / `WEB_AUTH_PASSWORD`). Obligatoire dès que l'interface est exposée
  (`WEB_BIND` hors de la boucle locale, ou nom non local dans `WEB_ALLOWED_HOSTS`) : elle refuse sinon
  de démarrer.

Métrologie :

- **Telegraf** a aussi la socket Docker (découverte des nœuds, `docker exec`) et lit le système de
  fichiers de l'hôte en lecture seule (`/:/hostfs:ro`, métriques de l'hôte). Il ne publie aucun port.
  Il est sur le réseau de management (`FLOW_COLLECTOR_IP`), partagé avec les nœuds et les jobs CI,
  pour recevoir les flux sFlow. Sur ce réseau, seul l'UDP 6343 du collecteur écoute ; le listener
  InfluxDB de `make iperf` et `make traffic` reste sur 127.0.0.1, et tout autre listener doit y
  rester. Les datagrammes sont filtrés avant décodage (IP de management d'un routeur du lab), lus avec
  des limites, et le nombre de séries écrites par cycle est plafonné. Un conteneur du réseau peut
  usurper l'IP d'un routeur (pas d'anti-usurpation sur le pont Docker) : il ne peut qu'ajouter de faux
  flux, rien exécuter.
- **Grafana** et **InfluxDB** écoutent sur 127.0.0.1 par défaut (`GRAFANA_BIND`, `INFLUXDB_BIND`).
  Grafana est consultable sans compte, en lecture seule : c'est ce qui permet l'onglet Métrologie
  (intégration en iframe autorisée).
- Aucun mot de passe ni jeton par défaut : `make init` les génère, et docker compose refuse de
  démarrer sans eux. Pour les changer après le premier démarrage : § 10.7 (InfluxDB),
  `grafana cli admin reset-admin-password` (Grafana).

GitLab :

- **Seul SSH est publié**, sur 127.0.0.1:2222 par défaut : GitLab n'est joignable que depuis l'hôte et
  les nœuds du lab, par le réseau de management.
- Mots de passe et jeton sont générés par `make init`. `GITLAB_API_TOKEN` est un jeton administrateur :
  gardez `.env` secret. Un jeton révoqué dans GitLab n'est jamais réactivé.
- `gitlab-sync` ne publie rien et ne parle qu'à GitLab, avec ce jeton ; il écrit seulement dans
  `gitlab/repos/`.

Runner et RESTCONF :

- **`gitlab-runner`** a la socket Docker (création des conteneurs des jobs) et `GITLAB_API_TOKEN`
  (enregistrement). Il ne publie aucun port.
- **Les jobs** n'ont ni la socket Docker, ni le mode privilégié, ni accès à l'API de l'interface
  (qui permet de `docker exec` dans les nœuds). Ils sont sur le réseau de management, donc joignent
  GitLab, les nœuds (eth0) et le RESTCONF des routeurs. Ils reçoivent `RESTCONF_USER` et
  `RESTCONF_PASSWORD` en clair : **tout compte GitLab qui peut lancer un pipeline peut changer la
  conf des routeurs**. Le runner accepte les jobs de tous les projets. Les jobs Ansible tournent dans
  l'image locale `sonic-lab-ansible` ; leurs modules s'exécutent dans le job (connexion locale) et ne
  parlent aux routeurs que par RESTCONF.
- **Le RESTCONF** n'écoute que sur l'interface de management des routeurs. Il utilise
  l'authentification HTTP Basic sur TLS, avec un certificat auto-signé non vérifié par défaut. Son mot
  de passe (`RESTCONF_PASSWORD`) est généré par `make init` ; il est fourni à tous les jobs CI.

Infra en GitOps :

- **Fusionner dans la branche par défaut de `lab-infra` revient à être root sur l'hôte** : les
  nœuds du lab sont des conteneurs privilégiés, déployés par l'agent avec la socket Docker.
- L'agent n'installe que la topologie et les confs, extraites de l'archive du commit (fichiers
  ordinaires, pas de lien, pas de `..`). Il n'exécute aucun code du dépôt. Son validateur limite les
  images, les binds et les clés.
- Il ne peut pas limiter les commandes `exec` lancées dans un nœud privilégié. La protection est la
  branche protégée (Maintainers et Owners), donc la sécurité de ces comptes GitLab.
- L'interface web a besoin de `GITLAB_API_TOKEN`, un jeton administrateur, pour ouvrir les merge
  requests de sauvegarde. Elle avait déjà la socket Docker.

---

## 16. Développement

### Backend (`web/backend/app/`)

| Module | Rôle |
|---|---|
| `main.py` | création de l'app, erreurs, middlewares (Basic), refus de démarrer exposée sans authentification, service de la SPA |
| `lab.py` | état du lab, runner, liens, sessions BGP, actions globales |
| `nodes/sonic.py`, `nodes/linux.py` | pilotes des nœuds (état, config, ping…) |
| `configdb_diff.py`, `frr_config.py` | moteurs de diff et de replace (purs, testés) |
| `remote/collect_sonic.py` | collecteur exécuté dans le routeur (stdlib + swsscommon) |
| `remote/apply_configdb.py` | applique les opérations CONFIG_DB dans le routeur |
| `docker_api.py` | docker exec, copie de fichiers, TTY (aiodocker) |
| `store.py` | configs de démarrage et snapshots |
| `security.py` | contrôles Host et Origin |
| `settings.py` | réglages lus dans l'environnement |
| `topology.py`, `parsers.py`, `cache.py` | lecture de la topologie, analyse des sorties des nœuds, cache court des états |
| `monitoring.py` | adresse et disponibilité de Grafana (`/api/monitoring`) |
| `captures.py`, `api/captures.py`, `api/wireshark.py` | captures de paquets : sondes `dumpcap`, visionneuses Wireshark et leur relais (§ 7.1) |
| `connectivity.py` | matrice de connectivité (cibles routées par chaque client) |
| `gitops.py` | mode GitOps : état de l'agent, merge requests de sauvegarde |
| `api/` | routes HTTP et WebSocket |

### Frontend (`web/frontend/src/`)

React, Mantine, TanStack Query, CodeMirror et xterm.js. Pages dans `pages/`, composants dans
`components/` (dont `GrafanaFrame.tsx`, l'onglet Métrologie).

### Métrologie (`monitoring/`)

| Fichier | Rôle |
|---|---|
| `telegraf/telegraf.conf` | entrées (collecteur, Docker, hôte, Telegraf), sortie InfluxDB |
| `telegraf/collector/lab_collector.py` | point d'entrée : `serve` (Telegraf, avec l'écoute sFlow), `once` (`make collect`), `iperf` (`make iperf`), `traffic` (`make traffic`) |
| `telegraf/collector/labmon/collector.py` | un cycle : découverte, relevés en parallèle, interfaces, liens, sondes, auto-surveillance |
| `telegraf/collector/labmon/remote/` | scripts exécutés dans les nœuds : `sonic_collect.py`, `linux_collect.sh`, `probe.sh` |
| `telegraf/collector/labmon/sonic.py`, `linux.py` | sorties des nœuds → points InfluxDB |
| `telegraf/collector/labmon/interfaces.py`, `probes.py`, `iperf.py` | interfaces et liens, sondes, tests iperf3 |
| `telegraf/collector/labmon/sflow.py` | décodage des datagrammes sFlow v5 et des en-têtes de paquets (VLAN, IPv4/IPv6, TCP/UDP/ICMP, VXLAN), classement par application (pur, borné) |
| `telegraf/collector/labmon/flows.py` | écoute UDP (thread), agrégation entre deux cycles, sources acceptées, points `lab_flow` et `lab_flow_agent` nommés avec la découverte du cycle |
| `telegraf/collector/labmon/traffic.py` | `make traffic` : serveurs et clients (iperf3, httpd, dnsmasq, ping) sur les clients, événements |
| `telegraf/collector/labmon/lineproto.py`, `state.py` | encodage line protocol, mémoire entre cycles (débits, événements) |
| `grafana/provisioning/` | datasources (InfluxQL par défaut, Flux) et dossier des tableaux de bord |
| `grafana/dashboards/*.json` | les 7 tableaux de bord |

Lancement local du collecteur, contre le lab déployé (Docker et la CLI `docker` sur l'hôte) :

```bash
cd monitoring/telegraf/collector
LAB_DIR=$PWD/../../../lab uv run --no-project --python 3.12 --with pyyaml python lab_collector.py once --rates
```

### GitLab (`gitlab/`)

| Fichier | Rôle |
|---|---|
| `provision/provision.sh` | lancé par l'image après chaque `gitlab-ctl reconfigure` (`GITLAB_POST_RECONFIGURE_SCRIPT`) ; ne doit jamais échouer |
| `provision/provision.rb` | `gitlab-rails runner` : réglages, comptes, groupe, jeton. S'appuie sur des classes internes de GitLab 19.6 : à revérifier lors d'une montée de version de l'image |
| `sync/gitlab_sync.py` | service `gitlab-sync` : `run` (la boucle), `trigger` (un cycle de la boucle, attendu et affiché : `make gitlab-sync`), `once` (cycle autonome, sans état : ne supprime rien) ; client de l'API, fusion à trois des refs (`plan_import`), export et restauration |
| `sync/tests/` | faux GitLab et vrais dépôts git (`conftest.py`) ; tests unitaires et cycles complets |
| `runner/entrypoint.sh` | service `gitlab-runner` : enregistrement idempotent par l'API, `config.toml` régénéré à chaque démarrage |
| `network-config/` | modèle du projet de CI/CD réseau avec Ansible (`make ci-project`, `scripts/ci_project.sh`) : rôle `sonic_config` (templates des documents RESTCONF), playbooks, module `sonic_lab.restconf.config` (logique dans `module_utils/restconf.py`, testée sans réseau), tests. `make ansible` le lance contre le lab |
| `ansible/` | image des jobs CI (`sonic-lab-ansible`), versions épinglées dans `requirements.txt` (aussi utilisé par `make test`) ; étage `test` : ansible-lint et pytest du modèle |

Lancement local des tests : `cd gitlab/sync && uv run --no-project --python 3.12 --with-requirements requirements-dev.txt python -m pytest -q tests`.

### RESTCONF (`lab/sonic/restconf/`)

S'exécute dans les routeurs, avec le Python 3.11 de l'image. containerlab monte le paquet dans
`/usr/local/lib/sonic-lab/`, avec `frr_config.py` et `parsers.py` de l'interface web : ces deux
modules doivent donc rester purs et compatibles Python 3.11.

| Module | Rôle |
|---|---|
| `rfc8040.py` | chemins d'URI, paramètres, types de média, documents d'erreur, yang-library (pur) |
| `datatree.py` | arbres JSON RFC 7951 : résolution d'un chemin sur le schéma, GET/PUT/PATCH/POST/DELETE, depth, content (pur) |
| `configdb_patch.py` | différence de deux CONFIG_DB → JSON Patch pour GCU (pur) |
| `messages.py` | erreurs de libyang et de GCU rendues lisibles (pur) |
| `labmodule.py` | module `sonic-lab` : schéma, état BGP, entrées et sorties de ping et preview (pur) |
| `service.py` | routage RESTCONF et méthodes, sur des sources injectées (pur) |
| `sonic.py` | CONFIG_DB par libyang (`sonic_yang`) et GCU (`config apply-patch`) |
| `frr.py` | vtysh (running-config, `-C`, replace et merge comme l'interface), état BGP, ping |
| `server.py` | HTTPS (certificat auto-signé), authentification Basic, journal ; `python3 -m restconf` |
| `yang/sonic-lab.yang` | modèle du module du lab (vérifié par `pyang --strict` dans les tests) |

Après une modification, relancez le bootstrap d'un routeur, qui redémarre le programme `restconf`, ou
redéployez.

### Agent GitOps (`lab/gitops/`)

Lancé par `lab/clab-runner.sh` (`python3 -m gitops`, dans son propre groupe de processus), depuis le
projet monté dans le service `clab`. Python et PyYAML viennent de l'image `lab/clab/Dockerfile`.

| Module | Rôle |
|---|---|
| `validate.py` | validateur de topologie, autonome (stdlib + PyYAML) : copié tel quel dans le projet `lab-infra` (`ci/validate.py`) |
| `plan.py` | plan : sortie de containerlab, nœuds redéfinis et cascade, confs modifiées, routeurs à restaurer ; commentaire Markdown et schéma Mermaid (pur) |
| `gitlab.py` | client de l'API GitLab (stdlib) |
| `agent.py` | boucle, plan des MR, déploiement, rollback, état ; commandes (`containerlab`, `docker`) et interface web injectées pour les tests |

Une modification de l'agent est prise en compte au redémarrage du service `clab` (`make redeploy`).

### En local

```bash
# backend sur :8080 (arrêtez d'abord le service web : docker compose stop web)
cd web/backend
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
LAB_DIR=$PWD/../../lab .venv/bin/uvicorn app.main:create_app --factory --port 8080 --reload

# frontend avec rechargement à chaud sur :5173 (proxy /api et /ws vers :8080)
cd web/frontend && npm install && npm run dev
```

L'image de l'interface se reconstruit avec `make up`.

---

## 17. Aide-mémoire

```bash
# cycle de vie
make init                                         # une fois : .env avec des secrets aléatoires
make up | make down | make redeploy | make logs | make ps | make smoke
cat lab/.runner-status

# accès
docker exec -it clab-sonic-lab-sonic1 bash        # CLI SONiC
docker exec -it clab-sonic-lab-sonic1 vtysh       # CLI FRR
docker exec -it clab-sonic-lab-client1 sh

# SONiC
show interfaces status | show ip interfaces | show ip route | show ip bgp summary | show vlan brief
show interfaces counters ; counterpoll show
config interface shutdown|startup EthernetX
config interface ip add|remove EthernetX A.B.C.D/NN
config vlan add N ; config vlan member add -u N EthernetX
config route add|del prefix P nexthop NH
sonic-cfggen -d --print-data ; sonic-db-cli CONFIG_DB HGETALL 'PORT|Ethernet0'
supervisorctl status ; tail -f /var/log/syslog ; bash /usr/local/bin/lab-bootstrap.sh

# FRR
vtysh -c "show bgp summary" ; vtysh -c "show ip bgp neighbors 10.0.12.2 advertised-routes"
vtysh -c 'conf t' -c 'router bgp 65001' -c 'address-family ipv4 unicast' -c 'network X/32'
vtysh -C -f fichier ; vtysh -f fichier

# clients
ping -c 3 192.168.2.10 ; traceroute -n 192.168.2.10 ; ip route

# API
curl -s localhost:8080/api/lab | jq
curl -s localhost:8080/api/nodes/sonic1/config/diff | jq .dirty
curl -s -X POST localhost:8080/api/nodes/sonic1/config/restore -H 'content-type: application/json' -d '{"source":"startup"}'

# secrets du .env dans le shell (pour les commandes ci-dessous) : set -a; . ./.env; set +a

# métrologie
# Grafana http://127.0.0.1:3000 (admin/GRAFANA_ADMIN_PASSWORD pour modifier) ; InfluxDB http://127.0.0.1:8086
make iperf [SRC=client1 DST=client2 DURATION=10 REVERSE=1 PARALLEL=4]
make collect                                      # ce que relève le collecteur
docker compose logs -f telegraf
docker compose exec influxdb influx query 'from(bucket: "lab") |> range(start: -1m) |> filter(fn: (r) => r._measurement == "lab_node") |> last()'
GRAFANA_URL=http://127.0.0.1:3000 python3 scripts/check_dashboards.py

# GitLab : http://172.20.30.5 (comptes de GITLAB_USERS ; root/GITLAB_ROOT_PASSWORD)
make gitlab-sync                                  # dépôts GitLab -> gitlab/repos/, et inversement
docker compose logs -f gitlab-sync
docker compose logs gitlab | grep sonic-lab       # provisioning : comptes, groupe, jeton
curl -s -H "PRIVATE-TOKEN: $GITLAB_API_TOKEN" http://172.20.30.5/api/v4/projects | jq '.[].path_with_namespace'

# RESTCONF (https://172.20.30.11 et .12, RESTCONF_USER/RESTCONF_PASSWORD)
curl -sk -u "$RESTCONF_USER:$RESTCONF_PASSWORD" https://172.20.30.11/restconf/data/sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0
curl -sk -u "$RESTCONF_USER:$RESTCONF_PASSWORD" https://172.20.30.11/restconf/data/sonic-lab:frr/state
docker exec clab-sonic-lab-sonic1 supervisorctl status restconf

# CI/CD avec Ansible
make ci-project                                   # projet <groupe>/network-config dans GitLab
make ansible                                      # preview depuis l'hôte (deploy.yml --check --diff)
make ansible PLAYBOOK=deploy.yml ARGS=--diff      # déployer (dont sFlow)
make ansible PLAYBOOK=verify.yml ARGS=            # BGP et pings
make ci-watch NEXT=1                              # puis git push : le pipeline en direct dans le terminal
docker compose logs -f gitlab-runner

# flux sFlow
make traffic                                      # 60 s de trafic typé client1 → client2
docker exec clab-sonic-lab-sonic1 show sflow      # conf sFlow du routeur
# Grafana › tableau « Flux réseau » (ou onglet Flux d'un routeur dans l'interface)

# infra en GitOps
make infra-project                                # projet <groupe>/lab-infra, adopté sans redéploiement
docker compose logs -f clab | grep gitops         # plans, déploiements, rollbacks
cat lab/.gitops/state.json                        # commit appliqué, état
ls lab/.gitops/backups/                           # sauvegardes avant chaque déploiement
```
