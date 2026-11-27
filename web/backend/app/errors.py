"""Exceptions métier, converties en réponses HTTP par main.py."""

from __future__ import annotations


class LabError(Exception):
    """Erreur générique (HTTP 500 par défaut)."""

    status_code = 500

    def __init__(self, message: str, *, detail: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFound(LabError):
    status_code = 404


class BadRequest(LabError):
    status_code = 400


class NodeUnavailable(LabError):
    """Le conteneur du nœud n'existe pas ou n'est pas démarré."""

    status_code = 409


class OperationFailed(LabError):
    """Une commande exécutée dans un nœud a échoué."""

    status_code = 502


class Conflict(LabError):
    """Une autre opération en cours empêche celle-ci (démarrage, arrêt, redéploiement du lab)."""

    status_code = 409
