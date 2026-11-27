"""Configuration du collecteur, lue dans les variables d'environnement du conteneur Telegraf."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EVENT_REGEX = r"%ADJCHANGE|%NOTIFICATION|oper state set from|lab-bootstrap"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    lab_name: str = "sonic-lab"
    topology_path: Path = Path("/lab/sonic-lab.clab.yml")
    sonic_kinds: frozenset[str] = frozenset({"sonic-vs"})
    exec_timeout: float = 8.0
    workers: int = 16
    probe_scope: str = "all"  # all | primary | off
    probe_count: int = 5
    probe_interval: float = 0.2
    probe_deadline: int = 3
    probe_max_targets: int = 64
    routes_max: int = 500
    event_regex: str = DEFAULT_EVENT_REGEX
    listener_url: str = "http://127.0.0.1:8186/api/v2/write"
    # flux sFlow des routeurs (mode serve) : port UDP (0 = pas d'écoute), adresse d'écoute (l'IP du
    # collecteur sur le réseau de management, FLOW_COLLECTOR_IP), plafond de clés par cycle
    sflow_port: int = 6343
    sflow_bind: str = "0.0.0.0"
    flow_max_keys: int = 2000

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ.get
        lab_dir = Path(env("LAB_DIR", "/lab"))
        kinds = env("SONIC_KINDS", "sonic-vs")
        scope = env("PROBE_SCOPE", cls.probe_scope).strip().lower()
        return cls(
            lab_name=env("LAB_NAME", cls.lab_name),
            topology_path=lab_dir / env("TOPOLOGY_FILE", "sonic-lab.clab.yml"),
            sonic_kinds=frozenset(k.strip() for k in kinds.split(",") if k.strip()),
            exec_timeout=_float("EXEC_TIMEOUT", cls.exec_timeout),
            workers=_int("COLLECTOR_WORKERS", cls.workers),
            probe_scope=scope if scope in ("all", "primary", "off") else cls.probe_scope,
            probe_count=max(1, _int("PROBE_COUNT", cls.probe_count)),
            probe_interval=max(0.2, _float("PROBE_INTERVAL", cls.probe_interval)),
            probe_deadline=max(1, _int("PROBE_DEADLINE", cls.probe_deadline)),
            probe_max_targets=max(1, _int("PROBE_MAX_TARGETS", cls.probe_max_targets)),
            routes_max=max(0, _int("ROUTES_MAX", cls.routes_max)),
            event_regex=env("EVENT_REGEX", cls.event_regex),
            listener_url=env("LISTENER_URL", cls.listener_url),
            sflow_port=max(0, min(65535, _int("SFLOW_PORT", cls.sflow_port))),
            sflow_bind=env("SFLOW_BIND") or cls.sflow_bind,
            flow_max_keys=max(1, _int("FLOW_MAX_KEYS", cls.flow_max_keys)),
        )
