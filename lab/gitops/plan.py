"""Plan d'un changement de topologie : ce que containerlab fera, plus ce qu'il ne voit pas. Module pur.

containerlab 0.83 réconcilie un lab déployé (deploy, --dry-run -f json) mais ne suit que l'ensemble
des nœuds et les liens : un changement d'image, d'IP de management, d'env, d'exec ou de binds n'y
apparaît pas. Le plan ajoute donc :
  - les nœuds « redéfinis » : leur définition (nœud + son kind) a changé ; l'agent les détruit avant la
    réconciliation (destroy --node-filter), qui les recrée ;
  - la cascade : les routeurs sonic-vs reliés à un nœud recréé, recréés à leur tour par containerlab
    (le kind ne prend pas les interfaces à chaud) ;
  - les confs de démarrage modifiées : appliquées par « restaurer la conf de démarrage » aux routeurs
    qui ne sont ni ajoutés ni recréés (un nœud recréé charge déjà la nouvelle conf au démarrage).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SONIC = "sonic-vs"
CONFIG_FILES = ("config_db.json", "frr.conf")
NOTE_MARKER = "<!-- sonic-lab-gitops:plan -->"
DEPLOY_MARKER = "<!-- sonic-lab-gitops:deploy -->"

_EP = re.compile(r"^([^:]+):(.+)$")


def _topo(data: dict[str, Any]) -> dict[str, Any]:
    return (data or {}).get("topology") or {}


def nodes_of(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(k): (v or {}) for k, v in (_topo(data).get("nodes") or {}).items()}


def kind_of(data: dict[str, Any], node: str) -> str | None:
    return (nodes_of(data).get(node) or {}).get("kind")


def links_of(data: dict[str, Any]) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    out = []
    for link in _topo(data).get("links") or []:
        eps = (link or {}).get("endpoints") or []
        parsed = [_EP.match(str(e)) for e in eps]
        if len(parsed) == 2 and all(parsed):
            out.append(((parsed[0].group(1), parsed[0].group(2)), (parsed[1].group(1), parsed[1].group(2))))
    return out


def definitions(data: dict[str, Any]) -> dict[str, str]:
    """Empreinte de la définition de chaque nœud : le nœud et la définition de son kind."""
    kinds = _topo(data).get("kinds") or {}
    return {
        name: json.dumps({"kind": kinds.get(node.get("kind")), "node": node}, sort_keys=True, default=str)
        for name, node in nodes_of(data).items()
    }


def config_files(files: dict[str, bytes], node: str) -> dict[str, bytes]:
    return {f: files[f"configs/{node}/{f}"] for f in CONFIG_FILES if f"configs/{node}/{f}" in files}


@dataclass
class Plan:
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    redefined: list[str] = field(default_factory=list)  # détruits par l'agent, recréés par containerlab
    recreated: dict[str, str] = field(default_factory=dict)  # nœud → raison
    added_links: list[str] = field(default_factory=list)
    deleted_endpoints: list[str] = field(default_factory=list)
    config_changes: dict[str, list[str]] = field(default_factory=dict)  # nœud → fichiers modifiés
    restore: list[str] = field(default_factory=list)  # routeurs à remettre dans leur conf de démarrage

    @property
    def topology_changed(self) -> bool:
        return bool(self.added or self.deleted or self.recreated or self.added_links or self.deleted_endpoints)

    @property
    def changed(self) -> bool:
        return self.topology_changed or bool(self.restore)

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)} nœud(s)")
        if self.deleted:
            parts.append(f"-{len(self.deleted)} nœud(s)")
        if self.recreated:
            parts.append(f"{len(self.recreated)} recréé(s)")
        links = len(self.added_links) + len(self.deleted_endpoints)
        if links:
            parts.append(f"{links} lien(s)")
        if self.restore:
            parts.append(f"{len(self.restore)} conf(s) de démarrage")
        return ", ".join(parts) if parts else "aucun changement"


_REASONS = {
    "added link": "lien ajouté",
    "deleted endpoint": "lien retiré",
    "deletedendpoint": "lien retiré",
}


def compute(
    old: dict[str, Any],
    new: dict[str, Any],
    clab: dict[str, Any],
    old_files: dict[str, bytes],
    new_files: dict[str, bytes],
) -> Plan:
    """old/new : topologies (YAML chargé) ; clab : JSON de containerlab deploy --dry-run -f json ;
    old_files/new_files : {"configs/<nœud>/<fichier>": contenu} du lab installé et du commit."""
    plan = Plan(
        added=sorted(clab.get("added-nodes") or []),
        deleted=sorted(clab.get("deleted-nodes") or []),
        added_links=list(clab.get("added-links") or []),
        deleted_endpoints=list(clab.get("deleted-endpoints") or []),
    )
    reasons = clab.get("node-change-reasons") or {}
    for node in clab.get("recreated-nodes") or []:
        plan.recreated[node] = _REASONS.get(str(reasons.get(node, "")).strip(), str(reasons.get(node) or "changement de liens"))

    old_def, new_def = definitions(old), definitions(new)
    excluded = set(plan.added) | set(plan.deleted)
    plan.redefined = sorted(n for n in new_def if n in old_def and old_def[n] != new_def[n] and n not in excluded)
    for node in plan.redefined:
        plan.recreated[node] = "définition modifiée"
    # cascade : recréer un nœud recrée ses liens ; un routeur sonic-vs relié est alors recréé lui aussi
    for (a, _), (b, _) in links_of(new):
        for node, peer in ((a, b), (b, a)):
            if node in plan.redefined and peer not in excluded and peer not in plan.recreated and kind_of(new, peer) == SONIC:
                plan.recreated[peer] = f"lien vers {node} (recréé)"
    plan.recreated = dict(sorted(plan.recreated.items()))

    for node in nodes_of(new):
        if node in plan.added:
            continue
        before, after = config_files(old_files, node), config_files(new_files, node)
        changed = sorted(f for f in set(before) | set(after) if before.get(f) != after.get(f))
        if changed:
            plan.config_changes[node] = changed
    plan.restore = sorted(
        n for n in plan.config_changes if kind_of(new, n) == SONIC and n not in plan.recreated and n not in excluded
    )
    return plan


# --- rendu -------------------------------------------------------------------------------------


def _mid(node: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", node)


def mermaid(topology: dict[str, Any], plan: Plan) -> str:
    """Schéma Mermaid (rendu par GitLab) : nœuds ajoutés en vert, recréés en orange, conf restaurée en bleu."""
    lines = ["graph LR"]
    for name, node in nodes_of(topology).items():
        ip = str(node.get("mgmt-ipv4") or "")
        label = f"{name}<br/><small>{node.get('kind', '?')} · {ip}</small>"
        lines.append(f'  {_mid(name)}["{label}"]')
    for (a, ia), (b, ib) in links_of(topology):
        lines.append(f'  {_mid(a)} ---|"{ia} · {ib}"| {_mid(b)}')
    styles = {"added": plan.added, "recreated": list(plan.recreated), "restored": plan.restore}
    lines += [
        "  classDef added fill:#d3f9d8,stroke:#2b8a3e",
        "  classDef recreated fill:#ffe8cc,stroke:#d9480f",
        "  classDef restored fill:#d0ebff,stroke:#1864ab",
    ]
    for cls, members in styles.items():
        present = [m for m in members if m in nodes_of(topology)]
        if present:
            lines.append(f"  class {','.join(_mid(m) for m in present)} {cls}")
    return "\n".join(lines)


def _cell(items: list[str]) -> str:
    return ", ".join(f"`{i}`" for i in items) if items else "—"


def note(
    *,
    sha: str,
    applied_sha: str | None,
    plan: Plan | None,
    errors: list[str],
    warnings: list[str],
    topology: dict[str, Any] | None,
    clab_error: str = "",
) -> str:
    """Commentaire de merge request (une seule note, mise à jour à chaque push)."""
    out = [NOTE_MARKER, f"### Plan du lab : commit `{sha[:8]}`", ""]
    if errors or clab_error:
        out.append("**✘ Refusé : la fusion ne doit pas avoir lieu.**")
        out.append("")
        out += [f"- ✘ {e}" for e in errors]
        if clab_error:
            out += ["", "containerlab :", "", "```", clab_error.strip()[:3000], "```"]
    elif plan is not None and not plan.changed:
        out.append("✔ Topologie valide. **Aucun changement** : la fusion ne touchera pas le lab.")
    elif plan is not None:
        out.append("✔ Topologie valide. À la fusion dans la branche par défaut, l'agent appliquera :")
        out += ["", "| Changement | Détail |", "|---|---|"]
        out.append(f"| Nœuds ajoutés | {_cell(plan.added)} |")
        out.append(f"| Nœuds supprimés | {_cell(plan.deleted)} |")
        recreated = [f"`{n}` ({why})" for n, why in plan.recreated.items()]
        out.append(f"| Nœuds recréés | {', '.join(recreated) if recreated else '—'} |")
        out.append(f"| Liens ajoutés | {_cell(plan.added_links)} |")
        out.append(f"| Extrémités retirées | {_cell(plan.deleted_endpoints)} |")
        confs = [f"`{n}` : {', '.join(files)}" for n, files in plan.config_changes.items()]
        out.append(f"| Confs de démarrage modifiées | {'<br>'.join(confs) if confs else '—'} |")
        out.append(f"| Restaurées sans recréation | {_cell(plan.restore)} |")
        if any(kind_of(topology or {}, n) == SONIC for n in plan.recreated) or plan.deleted:
            out += [
                "",
                "⚠️ Un routeur recréé repart de sa conf de démarrage : sa conf running est perdue et ses "
                "sessions BGP sont coupées pendant 30 s environ.",
            ]
    if warnings:
        out += ["", "Avertissements :", ""] + [f"- ⚠ {w}" for w in warnings]
    if topology and not errors and plan is not None:
        out += ["", "```mermaid", mermaid(topology, plan), "```"]
    base = f"commit déployé `{applied_sha[:8]}`" if applied_sha else "lab actuellement déployé"
    out += ["", f"<sub>Calculé par l'agent GitOps du lab (containerlab --dry-run) par rapport au {base}.</sub>"]
    return "\n".join(out) + "\n"


def deploy_note(*, sha: str, ok: bool, plan: Plan | None, message: str, drift: list[str], rolled_back: bool) -> str:
    """Commentaire posté sur la merge request fusionnée après le déploiement."""
    head = f"### {'✔ Déployé' if ok else '✘ Échec du déploiement'} : commit `{sha[:8]}`"
    out = [DEPLOY_MARKER, head, "", message]
    if plan is not None and ok:
        out += ["", f"Changements : {plan.summary()}."]
    if rolled_back:
        out += ["", "Le lab a été remis dans l'état du commit précédent (rollback)."]
    if drift:
        out += ["", "Dérive locale (fichiers modifiés sur l'hôte hors Git, sauvegardés avant d'être remplacés) :", ""]
        out += [f"- `{d}`" for d in drift]
    return "\n".join(out) + "\n"
