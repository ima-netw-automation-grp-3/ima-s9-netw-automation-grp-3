#!/usr/bin/python
# Module Ansible de la collection locale sonic_lab.restconf (projet network-config du lab SONiC).

from __future__ import annotations

DOCUMENTATION = r"""
---
module: config
short_description: Applique des documents RESTCONF sur un routeur SONiC du lab, validés par le routeur
description:
  - Pour chaque document, dans l'ordre, appelle l'opération C(sonic-lab:preview) du routeur
    (modèles YANG, GCU en simulation, C(vtysh -C)), puis fait le PUT ou le PATCH s'il change quelque chose.
  - S'arrête au premier document refusé ; les résultats déjà obtenus sont joints à l'échec.
  - En mode check, seul le preview est fait ; avec C(--diff), les différences calculées par le routeur s'affichent.
  - Un PUT remplace tout le module YANG (ou toute la conf FRR) ; un PATCH fusionne sans rien retirer.
options:
  url:
    description: Racine RESTCONF du routeur, par exemple C(https://172.20.30.11/restconf).
    type: str
    required: true
  user:
    description: Utilisateur HTTP Basic.
    type: str
    default: admin
  password:
    description: Mot de passe HTTP Basic.
    type: str
    required: true
  validate_certs:
    description: Vérifier le certificat (auto-signé et recréé avec chaque routeur dans le lab).
    type: bool
    default: false
  timeout:
    description: Délai d'une requête, en secondes (GCU applique un patch en plusieurs secondes).
    type: int
    default: 300
  documents:
    description: Documents à appliquer, dans l'ordre.
    type: list
    elements: dict
    required: true
    suboptions:
      name:
        description: Nom affiché.
        type: str
      path:
        description: Ressource, par exemple C(sonic-port:sonic-port) ou C(sonic-lab:frr/running-config).
        type: str
        required: true
      method:
        description: C(put) remplace la ressource, C(patch) fusionne.
        type: str
        choices: [put, patch]
        default: put
      content:
        description: Document JSON (RFC 7951) dont la seule clé est la ressource.
        type: raw
        required: true
author:
  - Équipe du lab SONiC
"""

EXAMPLES = r"""
- name: Ports et adresses, validés puis appliqués
  sonic_lab.restconf.config:
    url: https://172.20.30.11/restconf
    password: "{{ restconf_password }}"
    documents:
      - path: sonic-port:sonic-port
        method: patch
        content: {"sonic-port:sonic-port": {"PORT": {"PORT_LIST": [{"name": "Ethernet0", "mtu": 9100}]}}}
"""

RETURN = r"""
documents:
  description: Un résultat par document traité (name, path, method, changed, diff, resets, status ou error).
  returned: always
  type: list
  elements: dict
"""

from ansible.module_utils.basic import AnsibleModule  # noqa: E402

from ansible_collections.sonic_lab.restconf.plugins.module_utils.restconf import (  # noqa: E402
    ApplyError,
    Restconf,
    apply_documents,
    diff_sections,
)


def main() -> None:
    module = AnsibleModule(
        argument_spec={
            "url": {"type": "str", "required": True},
            "user": {"type": "str", "default": "admin"},
            "password": {"type": "str", "required": True, "no_log": True},
            "validate_certs": {"type": "bool", "default": False},
            "timeout": {"type": "int", "default": 300},
            "documents": {
                "type": "list",
                "elements": "dict",
                "required": True,
                "options": {
                    "name": {"type": "str"},
                    "path": {"type": "str", "required": True},
                    "method": {"type": "str", "choices": ["put", "patch"], "default": "put"},
                    "content": {"type": "raw", "required": True},
                },
            },
        },
        supports_check_mode=True,
    )
    p = module.params
    client = Restconf(p["url"], p["user"], p["password"], p["validate_certs"], p["timeout"])
    try:
        results = apply_documents(client, p["documents"], module.check_mode)
    except ApplyError as exc:
        module.fail_json(msg=str(exc), documents=exc.results, changed=any(r.get("status") for r in exc.results))
    for r in results:
        if r.get("changed") and r.get("resets"):
            module.warn(f"{r['name']} : sessions BGP réinitialisées par le remplacement de la conf FRR : {', '.join(r['resets'])}")
    module.exit_json(changed=any(r["changed"] for r in results), documents=results, diff=diff_sections(results))


if __name__ == "__main__":
    main()
