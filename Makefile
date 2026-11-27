# Raccourcis du lab SONiC. Les cibles se placent d'elles-mêmes à la racine du projet :
# containerlab (dans le service compose « clab ») a besoin du chemin absolu du projet.
ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
COMPOSE := cd $(ROOT) && PWD=$(ROOT) docker compose
# valeur d'une variable du .env, sinon la valeur par défaut : $(call envval,NOM,défaut)
envval = $(or $(shell sed -n 's/^$(1)=//p' $(ROOT)/.env 2>/dev/null | tail -1),$(2))

.DEFAULT_GOAL := help
.PHONY: help init image up down stop start redeploy logs ps test test-docker smoke iperf traffic collect gitlab-sync ci-project ci-watch infra-project ansible

help: ## Affiche cette aide
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

init: ## Crée .env depuis .env.example, avec des mots de passe et des jetons aléatoires (garde un .env existant)
	$(ROOT)/scripts/init_env.sh

# Build SONiC-VS avec laquelle le lab est testé (GUIDE.md, « Image SONiC ») : builds publiques du projet
# SONiC, sonic-buildimage master, plateforme vs.
SONIC_BUILD ?= 1280150
SONIC_SHA256 ?= e3416916c2985f98cccef9faefc9cdecb62a5b2bc5f7abe758f6420cf0d6d1b7
SONIC_URL = https://sonic-build.azurewebsites.net/api/sonic/artifacts?branchName=master&platform=vs&buildId=$(SONIC_BUILD)&target=target%2Fdocker-sonic-vs.gz
image: ## Télécharge l'image SONiC testée (docker-sonic-vs.gz, 234 Mo) si elle manque, et vérifie son empreinte
	@if [ -f $(ROOT)/docker-sonic-vs.gz ]; then echo "docker-sonic-vs.gz déjà présent"; else \
		echo "téléchargement de docker-sonic-vs.gz (build $(SONIC_BUILD), 234 Mo)…" && \
		curl -fL --retry 3 -o $(ROOT)/docker-sonic-vs.gz.part '$(SONIC_URL)' && \
		echo "$(SONIC_SHA256)  $(ROOT)/docker-sonic-vs.gz.part" | sha256sum -c --quiet && \
		mv $(ROOT)/docker-sonic-vs.gz.part $(ROOT)/docker-sonic-vs.gz && echo "docker-sonic-vs.gz : empreinte vérifiée" \
		|| { rm -f $(ROOT)/docker-sonic-vs.gz.part; echo "échec du téléchargement ou empreinte inattendue"; exit 1; }; fi

up: ## Construit les images, puis démarre le lab, l'interface web, la métrologie, GitLab et son runner
	$(COMPOSE) up -d --build
	-@$(COMPOSE) --profile tools pull --quiet wireshark-image || echo "image Wireshark non téléchargée : elle le sera à la première capture"
	@echo "Interface : http://127.0.0.1:$(call envval,WEB_PORT,8080)   (suivi du déploiement : make logs)"
	@echo "Grafana   : http://127.0.0.1:$(call envval,GRAFANA_PORT,3000)   InfluxDB : http://127.0.0.1:$(call envval,INFLUXDB_PORT,8086)"
	@echo "GitLab    : http://$(call envval,GITLAB_IP,172.20.30.5)   (premier démarrage : 5 min environ)"

down: ## Détruit le lab (containerlab destroy), arrête l'interface, la métrologie et GitLab (données gardées)
	$(COMPOSE) down

# « start » et non « up -d clab » : clab est en réseau hôte, compose ne créerait pas sonic-lab-mgmt
# (containerlab le créerait alors avec ses labels, et le make up suivant échouerait).
stop: ## Arrête le lab seul (containerlab destroy) ; interface, métrologie et GitLab continuent
	$(COMPOSE) stop clab

start: ## Démarre le lab arrêté : déploie lab/sonic-lab.clab.yml tel qu'il est maintenant
	$(COMPOSE) start clab

redeploy: ## Redéploie le lab (destroy, puis deploy de lab/sonic-lab.clab.yml et des confs de démarrage)
	$(COMPOSE) restart clab

logs: ## Suit les logs du déploiement et de l'interface
	$(COMPOSE) logs -f --tail=100

ps: ## État des services compose et des nœuds du lab
	@$(COMPOSE) ps
	@echo
	@docker ps -a --filter label=containerlab=sonic-lab --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'

test: ## Tests du backend, du collecteur, de gitlab-sync, de l'agent GitOps, du projet Ansible (uv + Python 3.12) et du RESTCONF (3.11)
	cd $(ROOT)/web/backend && uv run --no-project --python 3.12 --with-requirements requirements-dev.txt python -m pytest -q
	cd $(ROOT)/monitoring/telegraf/collector && uv run --no-project --python 3.12 --with-requirements requirements-dev.txt python -m pytest -q -p no:cacheprovider tests
	cd $(ROOT)/gitlab/sync && uv run --no-project --python 3.12 --with-requirements requirements-dev.txt python -m pytest -q -p no:cacheprovider tests
	cd $(ROOT)/lab/sonic/restconf && uv run --no-project --python 3.11 --with-requirements requirements-dev.txt python -m pytest -q -p no:cacheprovider tests
	cd $(ROOT)/lab && uv run --no-project --python 3.12 --with-requirements gitops/requirements-dev.txt python -m pytest -q -p no:cacheprovider gitops/tests
	cd $(ROOT)/gitlab/network-config && uv run --no-project --python 3.12 --with-requirements ../ansible/requirements.txt python -m pytest -q -p no:cacheprovider tests

test-docker: ## Mêmes tests dans Docker (sans uv)
	docker build --target test $(ROOT)/web
	docker build --target test $(ROOT)/monitoring/telegraf
	docker build --target test $(ROOT)/gitlab/sync
	docker build --target test --build-context webapp=$(ROOT)/web/backend/app $(ROOT)/lab/sonic/restconf
	docker build --target test -f $(ROOT)/lab/gitops/Dockerfile $(ROOT)/lab
	docker build --target test --build-context project=$(ROOT)/gitlab/network-config $(ROOT)/gitlab/ansible

smoke: ## Test de bout en bout sur le lab déployé (API, data plane, terminal, RESTCONF, métrologie, GitLab, CI/CD)
	$(ROOT)/scripts/smoke.sh

SRC ?= client1
DST ?= client2
DURATION ?= 10
iperf: ## Test de débit iperf3 entre clients (SRC=client1 DST=client2 DURATION=10 REVERSE=1 PARALLEL=4)
	$(COMPOSE) exec -T telegraf python3 /opt/lab-collector/lab_collector.py iperf --src $(SRC) --dst $(DST) \
		--duration $(DURATION) $(if $(REVERSE),--reverse) $(if $(PARALLEL),--parallel $(PARALLEL))

TYPE ?= mix
# durée par défaut de make traffic : 60 s (DURATION=… sur la ligne de commande l'emporte)
TRAFFIC_DURATION = $(if $(filter command line environment,$(origin DURATION)),$(DURATION),60)
traffic: ## Trafic typé entre clients pour les flux sFlow (SRC=client1 DST=client2 TYPE=mix|tcp|udp|http|dns|icmp DURATION=60)
	$(COMPOSE) exec -T telegraf python3 /opt/lab-collector/lab_collector.py traffic --src $(SRC) --dst $(DST) \
		--type $(TYPE) --duration $(TRAFFIC_DURATION)

collect: ## Affiche deux cycles de collecte (line protocol) : ce que reçoit Telegraf
	$(COMPOSE) exec -T telegraf python3 /opt/lab-collector/lab_collector.py once --rates

gitlab-sync: ## Synchronise tout de suite les dépôts GitLab avec gitlab/repos/
	$(COMPOSE) exec -T gitlab-sync python3 /app/gitlab_sync.py trigger

PROJECT ?= network-config
ci-project: ## Crée le projet de CI/CD réseau dans GitLab depuis gitlab/network-config (PROJECT=network-config)
	PROJECT=$(PROJECT) $(ROOT)/scripts/ci_project.sh

ci-watch: ## Suit en direct un pipeline de network-config, journal Ansible des jobs (NEXT=1 REF=… PIPELINE=… LOGS=0)
	@PROJECT=$(PROJECT) $(ROOT)/scripts/ci_watch.sh

infra-project: ## Crée le projet GitLab de l'infra en GitOps (topologie + confs de démarrage actuelles)
	$(ROOT)/scripts/infra_project.sh

PLAYBOOK ?= deploy.yml
ARGS ?= --check --diff
# Projet Ansible du modèle gitlab/network-config, lancé depuis l'hôte avec l'image des jobs CI, sur le
# réseau de management (comme un job) ; par défaut le preview seul. Déployer : ARGS=--diff.
# Identifiants RESTCONF du .env passés par l'environnement : ni affichés ni visibles dans la commande.
ansible: ## Lance un playbook de gitlab/network-config contre le lab (PLAYBOOK=deploy.yml ARGS="--check --diff")
	@echo "ansible-playbook playbooks/$(PLAYBOOK) $(ARGS)   (image sonic-lab-ansible, réseau sonic-lab-mgmt)"
	@RESTCONF_USER='$(call envval,RESTCONF_USER,admin)' RESTCONF_PASSWORD='$(call envval,RESTCONF_PASSWORD,)' \
		FLOW_COLLECTOR_IP='$(call envval,FLOW_COLLECTOR_IP,172.20.30.6)' \
		docker run --rm --network sonic-lab-mgmt --user $$(id -u):$$(id -g) -e HOME=/tmp \
		-e ANSIBLE_CONFIG=/work/ansible.cfg -e RESTCONF_USER -e RESTCONF_PASSWORD -e FLOW_COLLECTOR_IP \
		-v $(ROOT)/gitlab/network-config:/work -w /work sonic-lab-ansible:latest \
		ansible-playbook playbooks/$(PLAYBOOK) $(ARGS)
