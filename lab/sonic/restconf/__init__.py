"""Serveur RESTCONF (RFC 8040) du lab, exécuté DANS chaque routeur SONiC.

L'image docker-sonic-vs n'a pas de mgmt-framework : ce paquet en reprend l'essentiel pour le lab.
  - données sonic-* (CONFIG_DB) : modèles YANG de /usr/local/yang-models, URI de la même forme que le
    mgmt-framework SONiC, écritures validées par libyang puis appliquées par GCU (config apply-patch) ;
  - module sonic-lab : configuration FRR en texte, état BGP, opérations ping et preview.

Modules purs (testés sur l'hôte, sans les bibliothèques de SONiC) : rfc8040, datatree, configdb_patch,
messages, labmodule, service. Entrées/sorties dans le routeur : sonic (libyang, GCU), frr (vtysh, ping),
server (HTTPS). Lancé par supervisord (voir lab/sonic/lab-bootstrap.sh) : python3 -m restconf.

Compatibilité : Python 3.11 (celui du routeur), bibliothèque standard uniquement pour les modules purs.
"""
