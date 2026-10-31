#!/usr/bin/env bash
# One-shot core tuning, driven by the cores isolated in GRUB:
#
#   1. derive roles (cores.sh layout): housekeeping, poller, hot candidates, logger
#   2. corelat ranks the hot candidates against the poller; the top row wins
#   3. llcpollute checks the logger core against the winner
#   4. offline everything except housekeeping + poller + winner + logger
#   5. corelat again on the winner to confirm the clock after offlining
#   6. optional: wire2app on the winner (needs traffic arriving on the port)
#   7. HTML report with charts (scripts/report.py)
#
# Raw output and the report go to results/<time>-<host>/ (results/latest
# points at the newest); tune.env holds POLLER, BEST, LOGGER, ... to source.
#
#   sudo scripts/tune.sh [--no-offline] [--no-llc] [--wire2app PORT]
set -euo pipefail
cd "$(dirname "$0")/.."

offline=1 llc=1 w2a_port=""
while (( $# )); do
	case $1 in
	--no-offline) offline=0 ;;
	--no-llc)     llc=0 ;;
	--wire2app)   w2a_port=${2:?--wire2app needs a UDP port}; shift ;;
	*) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
	esac
	shift
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

eval "$(scripts/cores.sh layout --env)"     # fail early if nothing is isolated

run="results/$(date +%Y%m%d-%H%M%S)-$(hostname -s)"
mkdir -p "$run"
ln -sfn "$(basename "$run")" results/latest
token=$(curl -s -m 1 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
	http://169.254.169.254/latest/api/token 2>/dev/null || true)
itype=$(curl -s -m 1 -H "X-aws-ec2-metadata-token: $token" \
	http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || true)
{
	echo "HOST=$(hostname -s)"
	echo "TIME=$(date '+%Y-%m-%d %H:%M:%S %Z')"
	echo "KERNEL=$(uname -r)"
	echo "CPU_MODEL=$(awk -F': ' '/^model name/ {print $2; exit}' /proc/cpuinfo)"
	echo "INSTANCE_TYPE=$itype"
} >"$run/meta.env"

echo "== 1. layout (from GRUB isolated cores)"
scripts/cores.sh layout | sed '/^$/,$d'
eval "$(scripts/cores.sh layout --env)"

echo
echo "== 2. corelat: hot candidates $HOT vs poller $POLLER"
out=$(bin/corelat -b "$POLLER" -c "$HOT")
echo "$out" | tee "$run/corelat.txt"
# Lowest c2c among cores within 5% of the fastest clock, so a slow core
# (E-core on hybrid parts, or a throttled one) never wins on c2c alone.
BEST=$(awk '$1 ~ /^[0-9]+$/ { cpu[++n] = $1; ghz[n] = $5; if ($5 > max) max = $5 }
	END { for (i = 1; i <= n; i++) if (ghz[i] >= 0.95 * max) { print cpu[i]; exit } }' <<<"$out")
top=$(awk '$1 ~ /^[0-9]+$/ {print $1; exit}' <<<"$out")
[[ $BEST == "$top" ]] || echo "note: cpu $top has the lowest c2c but a clock >5% below the fastest core; using cpu $BEST"
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
	bin/llcpollute -v "$BEST" -c "$LOGGER" -l 8m -s 1g -t 5 | tee "$run/llcpollute.txt"
elif (( llc )); then
	echo; echo "== 3. llcpollute: skipped (no logger core; isolate 3+ physical cores)"
fi

if (( offline )); then
	echo
	echo "== 4. offline spare cores"
	scripts/cores.sh offline auto "$BEST" | grep -v '^offlined'
	echo
	echo "== 5. clock on hot core $BEST after offlining (was ${best_clk} GHz)"
	bin/corelat -b "$POLLER" -c "$BEST" | tee "$run/corelat_after.txt" | awk 'NR == 1 || $1 ~ /^[0-9]+$/'
	echo "(undo with: sudo scripts/cores.sh online)"
fi

if [[ -n $w2a_port ]]; then
	echo
	echo "== 6. wire2app on hot core $BEST, $IFACE port $w2a_port (needs traffic; up to 30 s)"
	bin/wire2app -i "$IFACE" -p "$w2a_port" -c "$BEST" -H "${HOUSEKEEPING%%[-,]*}" -n 20000 |
		tee "$run/wire2app.txt"
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

cp tune.env "$run/layout.env"

echo
echo "== 7. report"
python3 scripts/report.py "$run"
echo
echo "results: $run/  (raw output + report.html; all runs: results/index.html)"
echo "view:    scripts/report.py --serve 8080, then on your laptop:"
echo "         ssh -L 8080:127.0.0.1:8080 <user>@<this-host>  and open http://localhost:8080/"
[[ -n $w2a_port ]] || echo "add NIC-to-app latency with traffic flowing: sudo scripts/tune.sh --wire2app PORT"
