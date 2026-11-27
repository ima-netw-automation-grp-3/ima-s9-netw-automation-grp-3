# lab-infra : l'infra du lab SONiC en GitOps

Ce dépôt **est** l'infra du lab : la topologie containerlab et les confs de démarrage des routeurs.
Une merge request la modifie, et la fusion dans la branche par défaut la déploie. L'agent GitOps du
lab applique les changements ; personne ne lance containerlab à la main.

Ce projet a été créé par `make infra-project`, à partir des fichiers du lab au moment de la création.

## Contenu

```
sonic-lab.clab.yml               topologie containerlab (nœuds, liens, IP de management)
configs/<routeur>/config_db.json conf CONFIG_DB de démarrage
configs/<routeur>/frr.conf       conf FRR de démarrage
ci/validate.py                   validateur (copie de celui du lab ; c'est celui du lab qui fait foi)
.gitlab-ci.yml                   job validate
```

## Cycle d'une modification

1. **Branche et merge request.** Exemples : ajouter un client, un lien, un routeur avec ses
   `configs/`, ou modifier une conf de démarrage.
2. **Validation.** Le job `validate` vérifie les règles du lab, décrites plus bas.
3. **Plan.** En 20 s environ, l'agent du lab commente la MR avec le **plan**, puis publie le statut
   `lab/plan`. Le plan montre :
   - les nœuds ajoutés, supprimés ou recréés, et les liens ;
   - les confs de démarrage modifiées ;
   - un schéma de la topologie, les nœuds ajoutés en vert, les nœuds recréés en orange, les confs
     restaurées en bleu.

   Si le plan est refusé, la MR ne peut pas être fusionnée.
4. **Fusion.** L'agent applique le commit :
   - il installe les fichiers dans le lab ;
   - il recrée les nœuds dont la définition a changé ;
   - il réconcilie le lab avec containerlab, qui ajoute, supprime et recâble ;
   - il attend que les routeurs soient prêts ;
   - il remet les routeurs non recréés dans leur nouvelle conf de démarrage, sans coupure.

   Il publie ensuite le statut `lab/deploy`, un déploiement (Opérer › Environnements › `lab`), et un
   commentaire sur la MR.
5. **En cas d'échec**, l'agent remet le commit précédent (rollback) et signale l'échec. Le même
   commit n'est pas retenté : corrigez par une nouvelle MR.

Ce qui provoque une recréation :

- containerlab recrée un routeur SONiC dont un lien change, car il ne prend pas les interfaces à
  chaud ;
- l'agent recrée tout nœud dont la définition change (image, IP, env, exec, binds), car containerlab
  ne réconcilie que les nœuds et les liens. Les routeurs SONiC qui lui sont reliés sont recréés avec
  lui.

Un routeur recréé repart de sa conf de démarrage : sa conf running est perdue, et ses sessions BGP
sont coupées pendant 30 s environ. Le plan le signale.

## Règles

Le validateur (`ci/validate.py`, et surtout celui du lab) refuse :

- un `name` autre que `sonic-lab`, ou un réseau de management modifié (`sonic-lab-mgmt`,
  172.20.30.0/24, sans `ipv4-range`) ;
- une IP de management absente, en double, hors du sous-réseau ou réservée (.1, .5 pour GitLab, .6
  pour le collecteur sFlow de Telegraf) ;
- un kind autre que `sonic-vs` et `linux`, ou une image hors de la liste `GITOPS_ALLOWED_IMAGES` ;
- un routeur sans les éléments fournis par le lab : env `BGPD_GRACEFUL_RESTART_ARG` et `RESTCONF_*`,
  binds du bootstrap et du RESTCONF, exec `bash /usr/local/bin/lab-bootstrap.sh` ;
- un routeur sans ses deux fichiers `configs/<routeur>/`, ou dont le `config_db.json` n'est pas un
  JSON valide ;
- tout autre bind, ainsi que les clés non prévues (`network-mode`, `ports`, `cmd`…) ;
- un lien mal formé, vers un nœud inconnu, une interface déjà prise, ou au-delà de `eth32` sur un
  routeur.

Pour un nouveau routeur, recopiez le bloc d'un routeur existant, puis ses deux fichiers de
`configs/` en changeant `hostname`, les adresses et le BGP. `ethN` correspond au port SONiC
`Ethernet{4×(N-1)}`.

## Sauvegarder depuis l'interface web

Dans l'interface web du lab, « Sauvegarder running → startup » crée une **merge request** ici, avec
la conf running des routeurs. Une fois fusionnée, elle devient la conf de démarrage.

## Sécurité

Les nœuds du lab sont des conteneurs privilégiés : **fusionner dans la branche par défaut revient à
déployer du code en root sur l'hôte**. Le validateur limite les images, les montages et les clés,
mais pas les commandes `exec` lancées dans les nœuds. Seuls les Maintainers et les Owners peuvent
fusionner dans la branche protégée. Gardez ces comptes sûrs.
