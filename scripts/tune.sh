#!/usr/bin/env bash
# One-shot core tuning, driven by the cores isolated in GRUB:
#
#   1. derive roles (cores.sh layout): housekeeping, poller, hot candidates, logger
#   2. corelat ranks the hot candidates against the poller; the top row wins
#   3. llcpollute checks the logger core against the winner
#   4. offline everything except housekeeping + poller + winner + logger
#   5. corelat again on the winner to confirm the clock after offlining
#
# Results go to tune.env (source it to reuse POLLER, BEST, LOGGER, ...).
#
#   sudo scripts/tune.sh [--no-offline] [--no-llc]
#
# Measure the real NIC-to-app path afterwards with wire2app (needs traffic).
set -euo pipefail
cd "$(dirname "$0")/.."

offline=1 llc=1
for a in "$@"; do
	case $a in
	--no-offline) offline=0 ;;
	--no-llc)     llc=0 ;;
	*) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
	esac
done
[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0" >&2; exit 2; }
[[ -x bin/corelat && -x bin/llcpollute ]] || make

# Huge pages don't survive a reboot; llcpollute -s 1g needs two 1 GiB buffers
# plus the victim set (~1030 pages), so make sure enough are free.
need=1100
free=$(awk '/HugePages_Free/ {print $2}' /proc/meminfo)
if (( free < need )); then
	echo $(( $(</proc/sys/vm/nr_hugepages) + need - free )) >/proc/sys/vm/nr_hugepages
	echo "reserved huge pages: $(awk '/HugePages_Free/ {print $2}' /proc/meminfo) free"
fi

echo "== 1. layout (from GRUB isolated cores)"
scripts/cores.sh layout | sed '/^$/,$d'
eval "$(scripts/cores.sh layout --env)"

echo
echo "== 2. corelat: hot candidates $HOT vs poller $POLLER"
out=$(bin/corelat -b "$POLLER" -c "$HOT")
echo "$out"
BEST=$(awk '$1 ~ /^[0-9]+$/ {print $1; exit}' <<<"$out")
[[ -n $BEST ]] || { echo "could not read a winner from corelat" >&2; exit 2; }
read -r best_l3 best_clk < <(awk -v c="$BEST" '$1 == c {print $2, $5; exit}' <<<"$out")
echo "-> hot core: $BEST (l3_id $best_l3, ${best_clk} GHz)"

if [[ -n $LOGGER ]]; then
	lg_l3=$(cat "/sys/devices/system/cpu/cpu$LOGGER/cache/index3/id" 2>/dev/null || echo "?")
	[[ $lg_l3 == "$best_l3" ]] ||
		echo "warning: logger $LOGGER (l3_id $lg_l3) does not share an L3 with hot core $BEST; llcpollute will show little" >&2
fi

if (( llc )) && [[ -n $LOGGER ]]; then
	echo
	echo "== 3. llcpollute: victim $BEST, copier $LOGGER (~25 s)"
	bin/llcpollute -v "$BEST" -c "$LOGGER" -l 8m -s 1g -t 5
elif (( llc )); then
	echo; echo "== 3. llcpollute: skipped (no logger core; isolate 3+ physical cores)"
fi

if (( offline )); then
	echo
	echo "== 4. offline spare cores"
	scripts/cores.sh offline auto "$BEST" | grep -v '^offlined'
	echo
	echo "== 5. clock on hot core $BEST after offlining (was ${best_clk} GHz)"
	bin/corelat -b "$POLLER" -c "$BEST" | awk 'NR == 1 || $1 ~ /^[0-9]+$/'
	echo "(undo with: sudo scripts/cores.sh online)"
fi

cat >tune.env <<EOF
ISOLATED=$ISOLATED
HOUSEKEEPING=$HOUSEKEEPING
POLLER=$POLLER
HOT=$HOT
BEST=$BEST
LOGGER=$LOGGER
SIBLINGS=$SIBLINGS
IFACE=$IFACE
EOF

echo
echo "saved to tune.env. Next, with traffic arriving on $IFACE:"
echo "  source tune.env"
echo "  sudo bin/wire2app -i \$IFACE -p <port> -c \$BEST -H ${HOUSEKEEPING%%[-,]*}"
