"""Mémoire du collecteur entre deux cycles : débits, CPU, offsets syslog, totaux et états précédents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class Rates:
    """Dérivées de compteurs monotones ; aucun débit si le compteur recule (remise à zéro)."""

    def __init__(self) -> None:
        self._prev: dict[Any, tuple[float, float]] = {}

    def rate(self, key: Any, clock: float | None, value: float | None) -> float | None:
        if clock is None or value is None:
            return None
        prev = self._prev.get(key)
        self._prev[key] = (clock, value)
        if prev is None:
            return None
        dt, dv = clock - prev[0], value - prev[1]
        if dt <= 0 or dv < 0:
            return None
        return dv / dt

    def keep_only(self, keys: set[Any]) -> None:
        for key in [k for k in self._prev if k not in keys]:
            del self._prev[key]


@dataclass
class NodeState:
    container_id: str | None = None
    if_rates: Rates = field(default_factory=Rates)  # compteurs d'interfaces
    svc_rates: Rates = field(default_factory=Rates)  # temps CPU des services
    syslog: dict[str, int] | None = None  # {"inode": …, "offset": …} pour la lecture suivante
    syslog_totals: dict[tuple[str, str], float] = field(default_factory=dict)
    # (vrf, voisin) -> état, uptime et compteurs de connexions au relevé précédent
    bgp: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    oper: dict[str, bool] = field(default_factory=dict)  # interface -> état opérationnel
    ready: bool | None = None
    up: bool | None = None
    recreated: bool = False  # nouveau conteneur depuis le relevé précédent (redéploiement)


class State:
    def __init__(self) -> None:
        self.nodes: dict[str, NodeState] = {}

    def node(self, name: str, container_id: str | None) -> NodeState:
        """État d'un nœud ; remis à zéro si le conteneur a été recréé (redéploiement)."""
        st = self.nodes.get(name)
        if st is None:
            st = self.nodes[name] = NodeState(container_id=container_id)
        elif container_id and st.container_id != container_id:
            previous = st
            st = self.nodes[name] = NodeState(container_id=container_id)
            st.up = previous.up
            st.recreated = previous.container_id is not None
        elif container_id and st.container_id is None:
            st.container_id = container_id  # nœud vu absent puis créé : pas une recréation
        return st
