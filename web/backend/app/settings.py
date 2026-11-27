"""Configuration de l'application, lue depuis les variables d'environnement."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_STATIC = Path(__file__).resolve().parent.parent / "static"
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def is_loopback(name: str) -> bool:
    """Nom ou adresse de la boucle locale (localhost, 127.0.0.0/8, ::1)."""
    name = name.strip().lower()
    return name in ("localhost", "::1", "[::1]") or name.startswith("127.")


def _hosts(raw: str | None) -> tuple[str, ...]:
    """Noms d'hôte acceptés : ceux de WEB_ALLOWED_HOSTS, plus toujours ceux de la boucle locale.

    Les noms de la boucle locale restent acceptés quand la liste est personnalisée : le healthcheck
    du conteneur et `make smoke` passent par 127.0.0.1. Cela n'ouvre rien au DNS rebinding, qui
    présente toujours un nom contrôlé par un tiers.
    """
    extra = tuple(h.strip().lower() for h in (raw or "").split(",") if h.strip())
    return DEFAULT_ALLOWED_HOSTS + tuple(h for h in extra if h not in DEFAULT_ALLOWED_HOSTS)


@dataclass(frozen=True)
class Settings:
    lab_name: str = "sonic-lab"
    lab_dir: Path = Path("/lab")
    topology_file: str = "sonic-lab.clab.yml"
    compose_project: str = "sonic-lab"
    runner_service: str = "clab"
    docker_url: str | None = None
    status_ttl: float = 2.0
    exec_timeout: float = 30.0
    auth_user: str = ""
    auth_password: str = ""
    # Adresse de publication de l'interface (WEB_BIND de compose) : hors boucle locale, elle est exposée
    web_bind: str = "127.0.0.1"
    static_dir: Path = _DEFAULT_STATIC
    # Noms d'hôte acceptés dans l'en-tête Host (protection contre le DNS rebinding) ; "*" = tous.
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    # Métrologie : Grafana vu par le navigateur (vide = même hôte que l'interface, port grafana_port)
    # et depuis ce conteneur (test de disponibilité).
    grafana_url: str = ""
    grafana_port: int = 3000
    grafana_internal_url: str = "http://grafana:3000"
    # GitOps : projet GitLab de l'infra (vide = <gitlab_group>/lab-infra) et API GitLab vue d'ici.
    # Actif quand l'agent GitOps (service clab) le signale dans lab/.gitops/state.json.
    gitops_project: str = ""
    gitlab_group: str = ""
    gitlab_url: str = "http://gitlab"
    gitlab_token: str = ""
    # Captures de paquets : image Wireshark (visionneuse) et dumpcap (sonde dans le réseau du nœud),
    # volume Docker partagé et son point de montage ici, réseau compose de la visionneuse (vide = celui
    # de ce conteneur), nombre de captures simultanées et taille maximale d'un fichier.
    capture_image: str = "lscr.io/linuxserver/wireshark:4.6.9"
    capture_volume: str = "sonic-lab_captures"
    capture_dir: Path = Path("/captures")
    capture_network: str = ""
    capture_max_sessions: int = 3
    capture_max_mb: int = 500

    @property
    def topology_path(self) -> Path:
        return self.lab_dir / self.topology_file

    @property
    def configs_dir(self) -> Path:
        return self.lab_dir / "configs"

    @property
    def backups_dir(self) -> Path:
        return self.lab_dir / "backups"

    @property
    def runner_status_path(self) -> Path:
        return self.lab_dir / ".runner-status"

    @property
    def clab_dir(self) -> Path:
        """Dossier créé par containerlab au déploiement (topology-data.json, certificats...)."""
        return self.lab_dir / f"clab-{self.lab_name}"

    @property
    def gitops_state_path(self) -> Path:
        return self.lab_dir / ".gitops" / "state.json"

    @property
    def gitops_project_path(self) -> str:
        if self.gitops_project:
            return self.gitops_project
        return f"{self.gitlab_group}/lab-infra" if self.gitlab_group else ""

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user and self.auth_password)

    @property
    def exposed(self) -> bool:
        """Interface joignable au-delà de cette machine : publiée hors de la boucle locale (WEB_BIND),
        ou appelée par un nom non local (WEB_ALLOWED_HOSTS : reverse proxy, adresse du réseau)."""
        if self.web_bind.strip() and not is_loopback(self.web_bind):
            return True
        return any(not is_loopback(h) for h in self.allowed_hosts)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            lab_name=env("LAB_NAME", cls.lab_name),
            lab_dir=Path(env("LAB_DIR", str(cls.lab_dir))),
            topology_file=env("TOPOLOGY_FILE", cls.topology_file),
            compose_project=env("COMPOSE_PROJECT", cls.compose_project),
            runner_service=env("RUNNER_SERVICE", cls.runner_service),
            docker_url=env("DOCKER_URL") or None,
            status_ttl=float(env("STATUS_TTL", str(cls.status_ttl))),
            exec_timeout=float(env("EXEC_TIMEOUT", str(cls.exec_timeout))),
            auth_user=env("WEB_AUTH_USER", ""),
            auth_password=env("WEB_AUTH_PASSWORD", ""),
            web_bind=env("WEB_BIND", cls.web_bind),
            static_dir=Path(env("STATIC_DIR", str(_DEFAULT_STATIC))),
            allowed_hosts=_hosts(env("WEB_ALLOWED_HOSTS")),
            grafana_url=env("GRAFANA_URL", "").rstrip("/"),
            grafana_port=int(env("GRAFANA_PORT", str(cls.grafana_port)) or cls.grafana_port),
            grafana_internal_url=env("GRAFANA_INTERNAL_URL", cls.grafana_internal_url),
            gitops_project=env("GITOPS_PROJECT", "").strip(),
            gitlab_group=env("GITLAB_GROUP", "").strip(),
            gitlab_url=env("GITLAB_URL", cls.gitlab_url).rstrip("/"),
            gitlab_token=env("GITLAB_API_TOKEN", ""),
            capture_image=env("CAPTURE_IMAGE", cls.capture_image),
            capture_volume=env("CAPTURE_VOLUME", cls.capture_volume),
            capture_dir=Path(env("CAPTURE_DIR", str(cls.capture_dir))),
            capture_network=env("CAPTURE_NETWORK", ""),
            capture_max_sessions=int(env("CAPTURE_MAX_SESSIONS", str(cls.capture_max_sessions)) or cls.capture_max_sessions),
            capture_max_mb=int(env("CAPTURE_MAX_MB", str(cls.capture_max_mb)) or cls.capture_max_mb),
        )
