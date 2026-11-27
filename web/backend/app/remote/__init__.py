"""Scripts exécutés à l intérieur des nœuds (envoyés via docker exec)."""

from functools import cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent


@cache
def source(name: str) -> str:
    """Code source d un script distant (ex. "collect_sonic")."""
    return (_DIR / f"{name}.py").read_text(encoding="utf-8")
