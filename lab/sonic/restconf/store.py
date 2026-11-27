"""Interfaces des sources de données du serveur RESTCONF (implémentations : sonic.py, frr.py). Module pur."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .datatree import Schema


class InvalidData(Exception):
    """Données refusées (modèles YANG, GCU, vtysh -C) : 400 invalid-value pour le client."""

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = path


class OperationFailed(Exception):
    """Échec côté équipement (commande en erreur, délai dépassé) : 500 operation-failed."""


class SonicStore(Protocol):
    """CONFIG_DB vue à travers les modèles YANG sonic-*."""

    schema: Schema

    def modules(self) -> list[dict[str, Any]]:
        """Modules YANG chargés : name, revision, namespace, implemented, downloadable."""

    def schema_file(self, name: str, revision: str = "") -> Path | None:
        """Fichier .yang d'un module (téléchargement du schéma)."""

    def read(self) -> dict[str, Any]:
        """Toute la CONFIG_DB en JSON RFC 7951 ({"sonic-x:sonic-x": {…}, …})."""

    def validate(self, tree: dict[str, Any]) -> None:
        """Lève InvalidData si l'arbre complet viole les modèles YANG."""

    def to_configdb(self, tree: dict[str, Any]) -> dict[str, Any]:
        """Arbre RFC 7951 (tout ou partie des modules) → tables CONFIG_DB."""

    def apply(self, ops: list[dict[str, Any]], *, dry_run: bool = False) -> None:
        """Applique (ou simule) un JSON Patch sur la CONFIG_DB ; InvalidData si GCU le refuse."""


class FrrStore(Protocol):
    """Configuration et état FRR, ping."""

    def running(self) -> str:
        """Configuration courante, normalisée (sans les lignes gérées par SONiC)."""

    def neighbors(self) -> list[dict[str, Any]]:
        """Instances de sonic-lab:frr/state/bgp-neighbor."""

    def preview(self, text: str, mode: str) -> dict[str, Any]:
        """Validation et différences (valid, errors, diff, commands, resets), sans rien appliquer."""

    def apply(self, text: str, mode: str) -> dict[str, Any]:
        """mode replace ou merge ; InvalidData si la configuration est refusée."""

    def ping(self, destination: str, source: str | None, count: int) -> dict[str, Any]:
        """Sortie de l'opération sonic-lab:ping."""
