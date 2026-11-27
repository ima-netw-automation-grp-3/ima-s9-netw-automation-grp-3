#!/usr/bin/env python3
"""Vérifie les tableaux de bord Grafana du lab.

Chaque requête de chaque panneau est exécutée par l'API de Grafana (/api/ds/query), avec les variables
du tableau de bord résolues comme le ferait l'interface. Les erreurs (InfluxQL invalide, mesure
inconnue…) font échouer le script ; les panneaux sans données sont listés en avertissement.

Lancement (make smoke le fait), dans un conteneur du projet qui voit Grafana :
    docker compose exec -T web python - < scripts/check_dashboards.py
Variables d'environnement :
    GRAFANA_INTERNAL_URL, puis GRAFANA_URL
                   URL de Grafana (défaut : http://grafana:3000). Dans le conteneur web,
                   GRAFANA_INTERNAL_URL est déjà définie ; depuis l'hôte :
                   GRAFANA_URL=http://127.0.0.1:3000 python3 scripts/check_dashboards.py
    GRAFANA_AUTH   utilisateur:mot de passe si l'accès anonyme est désactivé
    FOLDER         uid du dossier des tableaux de bord (défaut : sonic-lab)
    RANGE          période interrogée (défaut : now-15m)
    STRICT         1 : un panneau sans données fait aussi échouer le script
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

URL = (os.environ.get("GRAFANA_INTERNAL_URL") or os.environ.get("GRAFANA_URL") or "http://grafana:3000").rstrip("/")
AUTH = os.environ.get("GRAFANA_AUTH", "")
FOLDER = os.environ.get("FOLDER", "sonic-lab")
RANGE = os.environ.get("RANGE", "now-15m")
STRICT = os.environ.get("STRICT") == "1"
# Panneaux vides tant qu'aucun test iperf3 n'a été lancé ou qu'aucun événement n'a eu lieu ; CRM :
# SONiC ne publie ses statistiques que 5 minutes après le démarrage du routeur.
OPTIONAL = re.compile(r"iperf|Tests iperf3|Retransmissions|événements|Événements|LLDP|CRM", re.IGNORECASE)
# Tableaux dont tous les panneaux peuvent être vides : les flux sFlow n'existent qu'une fois sFlow
# déployé sur les routeurs (CD Ansible) et du trafic passé dans la période (make traffic).
OPTIONAL_DASHBOARDS = {"lab-flows"}
_VAR_RE = re.compile(r"\$\{(\w+)(?::\w+)?\}|\$(\w+)")
_BUILTIN = {"timeFilter", "__interval", "__interval_ms", "interval", "__timeFilter", "col"}


def request(method: str, path: str, body: Any = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if AUTH:
        req.add_header("Authorization", "Basic " + base64.b64encode(AUTH.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {"message": f"HTTP {exc.code} : {payload[:300]}"}


def run_query(datasource: dict[str, Any], query: str, fmt: str = "time_series") -> tuple[str | None, list[dict[str, Any]]]:
    body = {
        "queries": [
            {
                "refId": "A",
                "datasource": datasource,
                "query": query,
                "rawQuery": True,
                "resultFormat": fmt,
                "intervalMs": 10000,
                "maxDataPoints": 300,
            }
        ],
        "from": RANGE,
        "to": "now",
    }
    res = request("POST", "/api/ds/query", body)
    result = ((res or {}).get("results") or {}).get("A")
    if result is None:
        return (res or {}).get("message") or "réponse inattendue", []
    return result.get("error"), result.get("frames") or []


def frame_values(frames: list[dict[str, Any]]) -> list[str]:
    """Valeurs texte d'un résultat de variable (SHOW TAG VALUES)."""
    out: list[str] = []
    for frame in frames:
        for column in (frame.get("data") or {}).get("values") or []:
            if column and all(isinstance(v, str) for v in column):
                out = list(column)  # la dernière colonne texte porte les valeurs
    return out


def has_data(frames: list[dict[str, Any]]) -> bool:
    for frame in frames:
        values = (frame.get("data") or {}).get("values") or []
        fields = (frame.get("schema") or {}).get("fields") or []
        for field, column in zip(fields, values):
            if field.get("type") == "time" or field.get("name") == "Time":
                continue
            if any(v is not None for v in column or []):
                return True
    return False


def escape(value: str) -> str:
    return re.sub(r"([\\/^$.|?*+()\[\]{}])", r"\\\1", value)


def resolve(text: str, values: dict[str, list[str]]) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        if name in _BUILTIN or name.startswith("tag_") or name not in values:
            return m.group(0)
        vals = [escape(v) for v in values[name]]
        return vals[0] if len(vals) == 1 else "(" + "|".join(vals) + ")"

    return _VAR_RE.sub(repl, text)


def variables(dash: dict[str, Any]) -> tuple[dict[str, list[str]], list[str]]:
    values: dict[str, list[str]] = {}
    problems = []
    for var in (dash.get("templating") or {}).get("list") or []:
        name = var.get("name")
        if var.get("type") == "custom":
            opts = [o.strip().split(" : ")[-1] for o in (var.get("query") or "").split(",") if o.strip()]
            values[name] = opts[:1] or [""]
            continue
        if var.get("type") != "query":
            continue
        query = var.get("query")
        if isinstance(query, dict):
            query = query.get("query", "")
        error, frames = run_query(var.get("datasource") or {}, resolve(query or "", values), "table")
        found = frame_values(frames)
        if error or not found:
            problems.append(f"variable ${name} : {error or 'aucune valeur'}")
            values[name] = ["__aucune_valeur__"]
            continue
        if var.get("multi") or var.get("includeAll"):
            values[name] = found
        else:
            values[name] = found[:1]
    return values, problems


def panels_of(dash: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for p in dash.get("panels") or []:
        out.append(p)
        out.extend(p.get("panels") or [])  # lignes repliées
    return [p for p in out if p.get("type") != "row" and p.get("targets")]


def main() -> int:
    found = request("GET", "/api/search?" + urllib.parse.urlencode({"type": "dash-db", "folderUIDs": FOLDER}))
    if not isinstance(found, list) or not found:
        print(f"aucun tableau de bord dans le dossier « {FOLDER} » ({URL}) : {found}")
        return 1
    errors = empty = total = 0
    for item in sorted(found, key=lambda d: d.get("title", "")):
        dash = (request("GET", f"/api/dashboards/uid/{item['uid']}") or {}).get("dashboard") or {}
        values, problems = variables(dash)
        lines = [f"  ✘ {p}" for p in problems]
        errors += len(problems)
        ok = 0
        for panel in panels_of(dash):
            for target in panel["targets"]:
                total += 1
                ds = target.get("datasource") or panel.get("datasource") or {}
                query = resolve(target.get("query") or "", values)
                error, frames = run_query(ds, query, target.get("resultFormat") or "time_series")
                label = f"« {panel.get('title')} » ({target.get('refId')})"
                if error:
                    errors += 1
                    lines.append(f"  ✘ {label} : {error}")
                elif not has_data(frames):
                    if OPTIONAL.search(panel.get("title") or "") or item["uid"] in OPTIONAL_DASHBOARDS:
                        ok += 1
                    else:
                        empty += 1
                        lines.append(f"  ⚠ {label} : aucune donnée sur {RANGE}")
                else:
                    ok += 1
        print(f"{dash.get('title')} [{item['uid']}] : {ok} requête(s) OK")
        for line in lines:
            print(line)
    print(f"\n{total} requête(s), {errors} erreur(s), {empty} sans donnée")
    return 1 if errors or (STRICT and empty) else 0


if __name__ == "__main__":
    sys.exit(main())
