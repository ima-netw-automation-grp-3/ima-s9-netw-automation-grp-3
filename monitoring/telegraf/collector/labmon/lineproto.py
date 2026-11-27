"""Encodage au format line protocol d'InfluxDB, avec des règles de typage strictes.

InfluxDB refuse qu'un même champ change de type. D'où les règles suivantes :
  - toute valeur numérique est écrite en float (jamais d'entier « i ») ;
  - un champ texte reste texte ; dans les tables génériques, une valeur non numérique est écrite dans
    « <champ>_text » (voir generic_fields), pour qu'un champ numérique ne reçoive jamais de texte ;
  - les booléens deviennent 0/1 ;
  - les tags vides sont omis, les retours à la ligne neutralisés, les textes tronqués.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

MAX_STRING = 1000
_NUMBER_RE = re.compile(r"^\s*-?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?\s*$")


@dataclass
class Point:
    measurement: str
    tags: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    ts_ns: int | None = None


def num(value: Any) -> float | None:
    """Valeur numérique (int, float, bool ou texte numérique) en float ; None sinon (NaN et infini exclus)."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str) and _NUMBER_RE.match(value):
        out = float(value)
    else:
        return None
    return out if math.isfinite(out) else None


def generic_fields(raw: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Champs d'une table générique : numérique -> <préfixe><clé> (float), sinon <préfixe><clé>_text."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None or isinstance(value, (dict, list)):
            continue
        name = prefix + str(key)
        number = num(value)
        if number is not None:
            out[name] = number
        elif str(value) != "":
            out[name + "_text"] = str(value)
    return out


def _escape_key(text: str) -> str:
    return text.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _clean(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ")


def escape_measurement(name: str) -> str:
    return _clean(name).replace(",", "\\,").replace(" ", "\\ ")


def escape_tag(value: Any) -> str:
    text = _clean(str(value)).rstrip("\\")
    return _escape_key(text)


def format_field(value: Any) -> str | None:
    """Valeur de champ encodée, ou None si elle doit être ignorée."""
    if isinstance(value, str):
        text = _clean(value)[:MAX_STRING]
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    number = num(value)
    if number is None:
        return None
    return repr(number)


def encode(point: Point) -> str | None:
    """Ligne line protocol d'un point, ou None s'il n'a aucun champ valide."""
    fields = []
    for key in sorted(point.fields):
        encoded = format_field(point.fields[key])
        if encoded is not None:
            fields.append(f"{_escape_key(_clean(str(key)))}={encoded}")
    if not fields:
        return None
    parts = [escape_measurement(point.measurement)]
    for key in sorted(point.tags):
        value = point.tags[key]
        if value is None or str(value) == "":
            continue
        parts.append(f"{_escape_key(_clean(str(key)))}={escape_tag(value)}")
    line = ",".join(parts) + " " + ",".join(fields)
    if point.ts_ns is not None:
        line += f" {int(point.ts_ns)}"
    return line


def encode_all(points: list[Point]) -> list[str]:
    return [line for line in (encode(p) for p in points) if line is not None]


def ns(ts: float) -> int:
    """Horodatage epoch (s) -> nanosecondes, arrondi à la milliseconde."""
    return int(round(ts * 1000)) * 1_000_000


def ns_us(ts: float) -> int:
    """Horodatage epoch (s) -> nanosecondes, arrondi à la microseconde (événements syslog)."""
    return int(round(ts * 1_000_000)) * 1000
