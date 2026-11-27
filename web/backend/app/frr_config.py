"""Configuration FRR (vtysh) : normalisation, comparaison et « replace » simplifié.

L'image SONiC ne fournit pas frr-reload.py. Le mode replace fonctionne donc par blocs de premier
niveau :
  - une ligne ou un bloc présent dans le running mais absent de la conf voulue est retiré
    (`no <ligne>`, `no <en-tête de bloc>`) ;
  - un bloc modifié dont des lignes ont disparu est supprimé puis réappliqué (pour `router bgp`,
    cela réinitialise les sessions : l'interface le signale) ;
  - les blocs `interface X` ne peuvent pas être supprimés : leurs lignes disparues sont niées une à une ;
  - la conf voulue est ensuite appliquée en merge (`vtysh -f`).

Les lignes gérées par SONiC lui-même (fpm, nht, route-map RM_SET_SRC posée par bgpcfgd...) sont
ignorées : jamais comparées, jamais retirées, jamais sauvegardées dans la conf de démarrage.
Ce module est pur (aucune E/S).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

SYSTEM_PATTERNS = [
    re.compile(p)
    for p in (
        r"^frr (version|defaults)\b",
        r"^hostname \S+$",
        r"^service integrated-vtysh-config$",
        r"^zebra nexthop-group keep \d+$",
        r"^(no )?fpm\b",
        r"^(ip|ipv6) nht resolve-via-default$",
        r"^(ip|ipv6) protocol \S+ route-map RM_SET_SRC6?$",
        r"^route-map RM_SET_SRC6? ",
        r"^line vty$",
    )
]
_HEADER_NOISE = re.compile(r"^(Building configuration|Current configuration|end$)")
_ROUTER_RE = re.compile(r"^router \S+")


def is_system_line(header: str) -> bool:
    return any(p.search(header) for p in SYSTEM_PATTERNS)


@dataclass(frozen=True)
class Item:
    """Commande de premier niveau, avec ses lignes enfants (indentation normalisée)."""

    header: str
    children: tuple[str, ...] = ()

    @property
    def is_interface(self) -> bool:
        return self.header.startswith("interface ")

    @property
    def is_router(self) -> bool:
        return bool(_ROUTER_RE.match(self.header))

    def render(self) -> list[str]:
        if not self.children and not _looks_like_block(self.header):
            return [self.header]
        return [self.header, *self.children, "exit"]


def _looks_like_block(header: str) -> bool:
    return header.split()[0] in {"router", "interface", "route-map", "vrf", "key", "bfd", "pbr-map"}


@dataclass
class ParsedConfig:
    items: list[Item] = field(default_factory=list)

    def by_header(self) -> dict[str, Item]:
        return {it.header: it for it in self.items}


def _squash(line: str) -> str:
    return " ".join(line.split())


def _normalize_children(raw: list[tuple[int, str]]) -> tuple[str, ...]:
    """Ré-indente les lignes enfants avec 1 espace par niveau, quelle que soit l'indentation source."""
    widths = sorted({w for w, _ in raw})
    level = {w: i + 1 for i, w in enumerate(widths)}
    return tuple(" " * level[w] + text for w, text in raw)


def parse(text: str, *, keep_system: bool = False) -> ParsedConfig:
    """Découpe une configuration FRR en commandes de premier niveau."""
    items: list[Item] = []
    header: str | None = None
    children: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal header, children
        if header is not None and (keep_system or not is_system_line(header)):
            items.append(Item(header, _normalize_children(children)))
        header, children = None, []

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("!") or _HEADER_NOISE.match(stripped):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            # fin de bloc : « exit », ou « exit-vrf » qui clôt les blocs vrf dans la running de FRR
            if stripped in ("exit", "exit-vrf"):
                flush()
                continue
            flush()
            header = _squash(stripped)
        elif header is not None:
            children.append((indent, _squash(stripped)))
        # une ligne indentée hors de tout bloc est ignorée
    flush()
    # un même en-tête répété (ex. bloc écrit en deux fois) : on fusionne ses lignes enfants
    merged: dict[str, Item] = {}
    for it in items:
        prev = merged.get(it.header)
        if prev is None:
            merged[it.header] = it
        else:
            extra = tuple(c for c in it.children if c not in prev.children)
            merged[it.header] = Item(it.header, prev.children + extra)
    return ParsedConfig(list(merged.values()))


def render(cfg: ParsedConfig) -> str:
    """Rend une configuration normalisée, dans le style de `show running-config`."""
    lines: list[str] = []
    items = cfg.items
    for i, it in enumerate(items):
        rendered = it.render()
        lines.extend(rendered)
        is_single = len(rendered) == 1
        next_single = i + 1 < len(items) and len(items[i + 1].render()) == 1
        # FRR regroupe les commandes d'une ligne consécutives sous un seul séparateur
        if not (is_single and next_single):
            lines.append("!")
    return "\n".join(lines) + ("\n" if lines else "")


def normalize(text: str) -> str:
    """Configuration « utilisateur » : sans en-têtes, sans lignes système, indentation homogène."""
    return render(parse(text))


def negate(line: str) -> str:
    line = line.strip()
    return line[3:] if line.startswith("no ") else f"no {line}"


@dataclass
class ReplacePlan:
    commands: list[str]
    removed: list[str]
    resets: list[str]  # blocs supprimés puis recréés (ex. router bgp : sessions réinitialisées)

    def to_dict(self) -> dict[str, object]:
        return {"commands": self.commands, "removed": self.removed, "resets": self.resets}


def replace_plan(running_text: str, desired_text: str) -> ReplacePlan:
    """Commandes à exécuter AVANT d'appliquer la conf voulue en merge pour obtenir un replace."""
    running = parse(running_text).by_header()
    desired = parse(desired_text).by_header()
    commands: list[str] = []
    removed: list[str] = []
    resets: list[str] = []

    for header, cur in running.items():
        want = desired.get(header)
        if want is None:
            removed.append(header)
            if cur.is_interface:
                level1 = [c for c in cur.children if c.startswith(" ") and not c.startswith("  ")]
                negations = [" " + negate(c) for c in level1 if c.strip() != "exit"]
                if negations:
                    commands.extend([header, *negations, "exit"])
            else:
                commands.append(negate(header))
            continue
        missing = [c for c in cur.children if c not in set(want.children)]
        if not missing:
            continue  # identique, ou uniquement des ajouts (le merge suffit)
        if cur.is_interface:
            level1 = [c for c in missing if not c.startswith("  ") and c.strip() != "exit"]
            if level1:
                commands.extend([header, *(" " + negate(c) for c in level1), "exit"])
        else:
            commands.append(negate(header))
            resets.append(header)
    return ReplacePlan(commands=commands, removed=removed, resets=resets)


def merge_additions(running_text: str, desired_text: str) -> str:
    """Lignes que le merge va ajouter (avec leur en-tête de bloc pour le contexte)."""
    running = parse(running_text).by_header()
    out: list[str] = []
    for it in parse(desired_text).items:
        cur = running.get(it.header)
        if cur is None:
            out.extend(it.render())
            out.append("!")
            continue
        new_children = [c for c in it.children if c not in set(cur.children)]
        if new_children:
            out.extend([it.header, *new_children, "exit", "!"])
    return "\n".join(out) + ("\n" if out else "")


def unified_diff(before: str, after: str, *, before_name: str = "running", after_name: str = "voulue") -> str:
    return "".join(
        difflib.unified_diff(
            normalize(before).splitlines(keepends=True),
            normalize(after).splitlines(keepends=True),
            fromfile=before_name,
            tofile=after_name,
        )
    )


def equivalent(a: str, b: str) -> bool:
    """Même configuration, à l'ordre des commandes de premier niveau près.

    FRR affiche sa running dans son propre ordre (routes statiques avant router bgp...), pas dans celui
    d'un fichier écrit à la main. L'ordre des lignes à l'intérieur d'un bloc, lui, compte.
    """
    return parse(a).by_header() == parse(b).by_header()


def clean_vtysh_error(output: str) -> str:
    """Rend lisibles les erreurs de `vtysh -C -f` (qui cite toujours /etc/frr/frr.conf)."""
    lines = [
        ln for ln in output.splitlines() if not ln.startswith("Configuration file[") and ln.strip()
    ]
    return "\n".join(lines).strip() or output.strip()
