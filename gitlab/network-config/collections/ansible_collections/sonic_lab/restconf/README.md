# sonic_lab.restconf

Collection locale du projet : le module `sonic_lab.restconf.config` applique des documents RESTCONF
(RFC 8040, JSON RFC 7951) sur un routeur SONiC du lab, chacun validé d'abord par le routeur lui-même
(opération `sonic-lab:preview` : modèles YANG, GCU en simulation, `vtysh -C`).

- `--check` : le preview seul, rien n'est appliqué ; `--diff` affiche les différences du routeur.
- `changed` vient du preview (le serveur répond 204 même quand rien ne change).
- Les documents d'une liste sont appliqués dans l'ordre, et l'application s'arrête au premier refus.

Le client HTTP et la logique d'application sont dans `plugins/module_utils/restconf.py`, testés par
`tests/` (faux routeur, sans réseau).
