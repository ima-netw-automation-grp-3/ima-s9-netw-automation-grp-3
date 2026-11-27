# Sondes ping exécutées DANS un nœud du lab (envoyé par lab_collector : sh -c <source> probe N I D cibles…).
#   $1 nombre de paquets, $2 intervalle (s), $3 délai maximal (s), puis les adresses cibles.
# Toutes les cibles sont pinguées en parallèle ; chaque ligne de sortie est préfixée par sa cible.
# Compatible ping busybox (clients Alpine) et iputils (SONiC).
count=$1
interval=$2
deadline=$3
shift 3
for target in "$@"; do
    (ping -c "$count" -i "$interval" -W 1 -w "$deadline" "$target" 2>&1 | sed "s|^|$target |") &
done
wait
