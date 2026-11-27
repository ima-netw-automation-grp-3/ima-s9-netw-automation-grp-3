"""Collecteur de métriques du lab SONiC pour Telegraf (inputs.execd).

Découvre les nœuds containerlab, exécute un collecteur dans chacun (docker exec), mesure la latence
entre nœuds et écrit le tout au format line protocol d'InfluxDB.
"""
