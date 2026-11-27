# Collecteur de métriques exécuté DANS un nœud Linux du lab (envoyé par lab_collector : sh -c <source>).
# Compatible busybox (clients Alpine) et coreutils/iproute2. Sortie texte en sections « @nom » :
#   @meta     clé=valeur
#   @netdev   « if <interface> <attribut> <valeur> » et « stat <interface> <compteur> <valeur> »
#   @addr, @route4, @route6, @neigh : sorties brutes de ip (-o addr, route, -6 route, neigh)
# Aucune liste figée : toutes les interfaces de /sys/class/net sont relevées.

echo "@meta"
read -r up _ < /proc/uptime && echo "uptime=$up"
echo "hostname=$(hostname 2>/dev/null)"
echo "kernel=$(uname -r 2>/dev/null)"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "os=${PRETTY_NAME:-$NAME}"
fi
n=0
for p in /proc/[0-9]*; do n=$((n + 1)); done
echo "processes=$n"

echo "@netdev"
for d in /sys/class/net/*; do
    i=${d##*/}
    [ "$i" = lo ] && continue
    for a in operstate carrier mtu speed flags address; do
        # carrier et speed sont illisibles (EINVAL) sur une interface arrêtée
        v=$(cat "$d/$a" 2>/dev/null) && echo "if $i $a $v"
    done
    for s in "$d"/statistics/*; do
        read -r v < "$s" 2>/dev/null && echo "stat $i ${s##*/} $v"
    done
done

echo "@addr"
ip -o addr 2>/dev/null
echo "@route4"
ip route 2>/dev/null
echo "@route6"
ip -6 route 2>/dev/null
echo "@neigh"
ip neigh 2>/dev/null
echo "@end"
