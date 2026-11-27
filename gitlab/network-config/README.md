# network-config : CI/CD réseau du lab SONiC avec Ansible

La configuration des routeurs du lab est décrite en YAML. Ansible la déploie par RESTCONF, le
serveur RFC 8040 qui tourne dans chaque routeur.

- Chaque merge request est validée par les routeurs eux-mêmes : `--check --diff`, que le module
  traduit en opération `sonic-lab:preview`.
- La fusion dans la branche par défaut déploie, puis vérifie.
- Si une étape échoue, l'état d'avant est remis automatiquement.

Ce projet est créé dans GitLab par `make ci-project`, depuis le dossier `gitlab/network-config/` du
projet du lab.

## Contenu

```
inventory/hosts.yml              routeurs et IP de management (RESTCONF)
inventory/group_vars/all.yml     identifiants RESTCONF (variables du runner), valeurs par défaut, sFlow
inventory/host_vars/<routeur>.yml  la conf du routeur : ports, interfaces, loopback, bgp, verify
roles/sonic_config/              templates Jinja2 → documents RESTCONF et frr.conf, puis application
playbooks/render.yml             rendu dans build/<routeur>/ (sans joindre les routeurs)
playbooks/deploy.yml             sauvegarde (rollback/), puis application, routeur par routeur
playbooks/verify.yml             voisins BGP Established, pings depuis les routeurs
playbooks/rollback.yml           remet l'état sauvegardé par deploy
collections/…/sonic_lab/restconf module sonic_lab.restconf.config (preview, puis PUT ou PATCH)
tests/                           tests pytest : module (faux routeur), rendu (cohérence)
.gitlab-ci.yml, ansible.cfg, .ansible-lint
```

### Une conf de routeur

```yaml
# inventory/host_vars/sonic1.yml
ports:                       # description, admin_status (up), mtu (9100)
  - {name: Ethernet0, description: client1}
  - {name: Ethernet4, description: sonic2}
interfaces:                  # adresses IP des ports
  - {name: Ethernet0, ip: 192.168.1.1/24}
  - {name: Ethernet4, ip: 10.0.12.1/30}
loopback: 10.255.0.1/32      # Loopback0
bgp:
  asn: 65001
  router_id: 10.255.0.1
  neighbors:
    - {address: 10.0.12.2, remote_as: 65002, description: sonic2}
  networks: [10.255.0.1/32, 192.168.1.0/24]
verify:                      # après déploiement, en plus des voisins BGP
  ping:
    - {destination: 192.168.2.10, source: 192.168.1.1}
```

`containerlab ethN` correspond au port SONiC `Ethernet{4×(N-1)}` : eth1 = Ethernet0, eth2 = Ethernet4.

### Documents produits

Les templates du rôle `sonic_config` en tirent ces documents, appliqués dans l'ordre de
`roles/sonic_config/defaults/main.yml` :

| Document | Méthode | Contenu |
|---|---|---|
| `sonic-port` | PATCH | fusion : description, état et MTU des ports ; le reste des ports ne bouge pas |
| `sonic-loopback-interface` | PUT | Loopback0 et son adresse ; le module entier est remplacé |
| `sonic-interface` | PUT | adresses IP des ports |
| `sonic-sflow` | PATCH puis PUT | sessions d'abord, puis sFlow actif (voir plus bas) |
| `frr.conf` | PUT sur `sonic-lab:frr/running-config` | conf BGP, remplacée bloc par bloc |

Ordre : CONFIG_DB d'abord, FRR ensuite. Un PUT remplace tout le module YANG ; un PATCH fusionne sans
rien retirer.

`make ansible PLAYBOOK=render.yml` (depuis le projet du lab) ou le job `lint` écrivent ces documents
dans `build/<routeur>/`. Les fichiers portent le nom du module : `<module>.json` pour un PUT,
`<module>.patch.json` pour un PATCH.

Pour gérer un autre module SONiC (`sonic-vlan`…), ajoutez son template et une entrée dans
`sonic_config_documents`.

### sFlow : les types de trafic dans Grafana

Le document `sonic-sflow` active sFlow sur chaque port de `ports`.

| Réglage | Valeur | Remarque |
|---|---|---|
| échantillonnage | 1 paquet reçu sur 256 | `sflow_sample_rate`, minimum du modèle YANG |
| compteurs | toutes les 20 s | |
| agent | adresse de Loopback0 | |
| collecteur | Telegraf du lab, `FLOW_COLLECTOR_IP`:6343 | la variable vient du runner |

Ce n'est pas NetFlow : SONiC ne fait que sFlow. Le tableau Grafana « Flux réseau » montre le trafic
par application, protocole et conversation. `sflow_enabled: false` arrête sFlow.

Les sessions (tous les ports à down, sauf ceux de `ports`) sont posées par un PATCH avant le PUT qui
active sFlow. Sans cela, sflowmgrd échantillonnerait d'abord tous les ports, et les ports non câblés
mettraient syncd en erreur.

## Pipeline

| Étape | Merge request, branche | Branche par défaut | Rôle |
|---|---|---|---|
| lint | ✔ | ✔ | `ansible-lint` (profil production), rendu dans `build/` (artefact), `pytest tests` |
| preview | ✔ | ✔ | `deploy.yml --check --diff`. Pour chaque document, le routeur valide les modèles YANG, simule GCU (`--dry-run`) et passe la conf FRR à `vtysh -C`. Le job affiche la différence et refuse au premier document invalide |
| deploy | | ✔ | sauvegarde de l'état courant (`rollback/`, artefact), puis application, routeur par routeur, arrêt au premier échec |
| verify | ✔ | ✔ | voisins BGP des host_vars Established, pings de `verify.ping` sans perte, pendant au plus `VERIFY_TIMEOUT` (180 s) |
| rollback | | si échec | PUT des sauvegardes de `rollback/` |

`deploy`, `verify` et `rollback` partagent le `resource_group` `sonic-lab` : deux pipelines ne
modifient jamais le lab en même temps.

Les jobs tournent dans l'image `sonic-lab-ansible` (ansible-core, ansible-lint, pytest). Elle est
construite par le `docker compose` du lab, sur le réseau de management. Le runner leur fournit
`RESTCONF_USER`, `RESTCONF_PASSWORD` et `FLOW_COLLECTOR_IP`.

### Idempotence

Le serveur RESTCONF répond 204 même quand rien ne change. Le module `sonic_lab.restconf.config`
décide donc de `changed` d'après le preview du routeur :
- pour la CONFIG_DB, d'après les opérations du JSON Patch ;
- pour FRR, d'après le diff et les commandes.

Un deuxième déploiement identique donne `changed=0`.

Pourquoi un module et pas `ansible.netcommon.restconf_config` ? En PUT, celui-ci n'envoie que la
différence avec l'existant, ce qui retire les tables voisines du module, et il ne valide rien côté
routeur.

Le déploiement touche la configuration **running** des routeurs. Pour qu'elle survive à un
redéploiement du lab, sauvegardez-la depuis l'interface web (« Sauvegarder running → startup »). En
mode GitOps, cela ouvre une merge request dans `lab-infra`.

## Travailler

1. Créez une branche, modifiez `inventory/host_vars/<routeur>.yml`, puis ouvrez une merge request.
2. Lisez le job `preview`. Il montre les différences sur chaque routeur, ou la raison d'un refus :
   erreur YANG avec le chemin en cause, ou syntaxe FRR. Les documents rendus sont dans l'artefact
   du job `lint`.
3. Fusionnez : `deploy`, puis `verify`. Si une étape échoue, `rollback` remet la conf d'avant.

En local, depuis la racine du projet du lab, avec la même image que les jobs :

```bash
make ansible                                   # deploy.yml --check --diff : le preview seul
make ansible PLAYBOOK=deploy.yml ARGS=--diff   # déployer
make ansible PLAYBOOK=verify.yml ARGS=         # vérifier
make ansible PLAYBOOK=deploy.yml ARGS="--check --diff --limit sonic1"
```

## RESTCONF des routeurs

URL : `https://<IP de management>/restconf`, avec l'authentification HTTP Basic du `.env` du lab
(`RESTCONF_USER`, `RESTCONF_PASSWORD`, créés par `make init`). Le certificat est auto-signé :
utilisez `curl -k`, ou `validate_certs: false` dans Ansible.

```bash
# identifiants du .env du lab : set -a; . ./.env; set +a
R=https://172.20.30.11/restconf; A="-u $RESTCONF_USER:$RESTCONF_PASSWORD"
curl -sk $A $R/data/sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0
curl -sk $A $R/data/sonic-sflow:sonic-sflow                  # conf sFlow
curl -sk $A $R/data/sonic-lab:frr/state                      # sessions BGP
curl -sk $A -X POST -H 'Content-Type: application/yang-data+json' \
  -d '{"sonic-lab:input":{"destination":"192.168.2.10","source":"192.168.1.1"}}' $R/operations/sonic-lab:ping
```
