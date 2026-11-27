"""Scripts exécutés à l'intérieur des nœuds du lab (envoyés par docker exec)."""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent
_cache: dict[str, tuple[float, str]] = {}


def source(name: str) -> str:
    """Contenu d'un script distant, relu si le fichier a changé."""
    path = _DIR / name
    mtime = path.stat().st_mtime
    hit = _cache.get(name)
    if hit is None or hit[0] != mtime:
        hit = _cache[name] = (mtime, path.read_text(encoding="utf-8"))
    return hit[1]
