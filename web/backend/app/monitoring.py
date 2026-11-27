"""Métrologie (stack TIG) : où trouver Grafana et ses tableaux de bord, et s'il répond."""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from typing import Any

from .cache import TTLCache
from .settings import Settings

# uid des tableaux de bord provisionnés (monitoring/grafana/dashboards/*.json)
DASHBOARDS = {
    "overview": "lab-overview",
    "router": "lab-router",
    "client": "lab-client",
    "interfaces": "lab-interfaces",
    "probes": "lab-probes",
    "infra": "lab-infra",
    "flows": "lab-flows",
}


class Monitoring:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache = TTLCache(15.0)

    def probe(self) -> bool:
        """Grafana répond-il (GET /api/health depuis le conteneur de l'interface) ?"""
        url = self.settings.grafana_internal_url.rstrip("/") + "/api/health"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - URL de configuration
                return resp.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    async def info(self) -> dict[str, Any]:
        available = await self._cache.get("grafana", lambda: asyncio.to_thread(self.probe))
        return {
            "grafana": {
                "url": self.settings.grafana_url or None,
                "port": self.settings.grafana_port,
                "available": available,
            },
            "dashboards": DASHBOARDS,
        }
