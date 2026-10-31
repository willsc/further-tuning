#!/usr/bin/env bash
# Core housekeeping for an idle=poll box.
#
#   cores.sh layout [--env]       derive CPU roles from the cores isolated at boot
#                                 (isolcpus= / nohz_full= on the GRUB command line);
#                                 --env prints shell variables for eval
#   cores.sh offline auto [HOT]   keep housekeeping + poller + logger + HOT (default:
#                                 every hot candidate), offline the rest
#   cores.sh offline KEEP_LIST    offline every online CPU not in KEEP_LIST
#                                 (this includes HT siblings of hot cores unless kept)
#   cores.sh online               bring every CPU back online
#   cores.sh prefetch off|on CPU  Intel only: L1/L2 prefetchers via MSR 0x1A4
#   cores.sh status               online CPUs and per-CPU prefetcher MSR
#
# Check the turbo headroom afterwards with:
#   turbostat --quiet --show CPU,Busy%,Bzy_MHz,CPU%c6 sleep 5
set -euo pipefail

sys=/sys/devices/system/cpu

expand() {
	local out=() part a b
	IFS=, read -ra parts <<<"$1"
	for part in "${parts[@]}"; do
		a=${part%-*}; b=${part#*-}
		for ((c = a; c <= b; c++)); do out+=("$c"); done
	done
	echo "${out[@]}"
}

# 3,4,5,7 -> 3-5,7
compress() {
	local out="" start="" prev="" c
	for c in $(tr ' ,' '\n\n' <<<"$*" | grep -E '^[0-9]+$' | sort -n | uniq); do
		if [[ -n $prev && $c -eq $((prev + 1)) ]]; then prev=$c; continue; fi
		[[ -n $start ]] && out+="${out:+,}$start$([[ $prev != "$start" ]] && echo "-$prev")"
		start=$c; prev=$c
	done
	[[ -n $start ]] && out+="${out:+,}$start$([[ $prev != "$start" ]] && echo "-$prev")"
	echo "$out"
}

# Cores isolated at boot. ISOLATED=list overrides (for testing).
isolated() {
	local iso="${ISOLATED:-}" arg
	[[ -z $iso && -r $sys/isolated ]] && iso=$(<"$sys/isolated")
	if [[ -z $iso ]]; then
		for arg in $(</proc/cmdline); do
			case $arg in
			isolcpus=*)  iso=${arg#isolcpus=} ;;
			nohz_full=*) [[ -z $iso ]] && iso=${arg#nohz_full=} ;;
			esac
		done
		# isolcpus=managed_irq,domain,2-7 -> 2-7
		iso=$(tr ',' '\n' <<<"$iso" | grep -E '^[0-9]' | paste -sd, -)
	fi
	echo "$iso"
}

l3_id()   { cat "$sys/cpu$1/cache/index3/id" 2>/dev/null || echo "?"; }
core_key() { echo "$(<"$sys/cpu$1/topology/physical_package_id"):$(<"$sys/cpu$1/topology/core_id")"; }
siblings() { expand "$(<"$sys/cpu$1/topology/thread_siblings_list")"; }

# Sets HOUSEKEEPING POLLER HOT LOGGER ISO NIC_NODE IFACE
layout() {
	local iso c n on nodecpus=() cands=() hot=() seen=() k
	iso=$(isolated)
	if [[ -z $iso ]]; then
		echo "no isolated cores found in $sys/isolated or /proc/cmdline." >&2
		echo "add e.g. isolcpus=2-7 nohz_full=2-7 rcu_nocbs=2-7 to GRUB_CMDLINE_LINUX in" >&2
		echo "/etc/default/grub, run update-grub, reboot (or set ISOLATED=2-7 to override)." >&2
		exit 2
	fi
	ISO=$(compress "$(expand "$iso")")
	declare -A isset=()
	for c in $(expand "$iso"); do isset[$c]=1; done
	HOUSEKEEPING=""
	on=$(expand "$(<"$sys/online")")
	for c in $on; do [[ -z ${isset[$c]:-} ]] && HOUSEKEEPING+=" $c"; done
	HOUSEKEEPING=$(compress "$HOUSEKEEPING")

	# Prefer isolated cores on the NIC's NUMA node.
	IFACE=${IFACE:-$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')}
	NIC_NODE=$(cat "/sys/class/net/$IFACE/device/numa_node" 2>/dev/null || echo -1)
	for c in $(expand "$iso"); do
		[[ -d $sys/cpu$c/topology ]] || continue      # offline or absent
		n=$(ls -d "$sys/cpu$c"/node* 2>/dev/null | head -1); n=${n##*node}
		if (( NIC_NODE < 0 )) || [[ $n == "$NIC_NODE" ]]; then nodecpus+=("$c"); fi
	done
	(( ${#nodecpus[@]} )) || for c in $(expand "$iso"); do
		[[ -d $sys/cpu$c/topology ]] && nodecpus+=("$c")
	done
	(( ${#nodecpus[@]} )) || { echo "isolated cores $ISO are all offline; run: $0 online" >&2; exit 2; }

	# One hardware thread per physical core; HT siblings are left unused.
	declare -A used=()
	for c in "${nodecpus[@]}"; do
		k=$(core_key "$c")
		[[ -n ${used[$k]:-} ]] && continue
		used[$k]=1; cands+=("$c")
	done
	POLLER=${cands[0]}
	LOGGER=""
	if (( ${#cands[@]} >= 3 )); then
		# Last candidate sharing the poller's L3 (same socket / CCD).
		for ((n = ${#cands[@]} - 1; n > 0; n--)); do
			if [[ $(l3_id "${cands[n]}") == "$(l3_id "$POLLER")" ]]; then
				LOGGER=${cands[n]}; break
			fi
		done
	fi
	for c in "${cands[@]:1}"; do [[ $c != "$LOGGER" ]] && hot+=("$c"); done
	HOT=$(compress "${hot[*]}")
	[[ -n $HOT ]] || { echo "need at least 2 isolated physical cores (poller + hot), have: $ISO" >&2; exit 2; }
}

need_root() { [[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 2; }; }

case "${1:-}" in
layout)
	layout
	if [[ ${2:-} == --env ]]; then
		printf 'ISOLATED=%s\nHOUSEKEEPING=%s\nPOLLER=%s\nHOT=%s\nLOGGER=%s\nIFACE=%s\n' \
			"$ISO" "$HOUSEKEEPING" "$POLLER" "$HOT" "$LOGGER" "$IFACE"
		exit 0
	fi
	echo "isolated at boot:  $ISO"
	echo "housekeeping:      ${HOUSEKEEPING:-none}   (OS, IRQs, PHC sampler)"
	echo "poller:            $POLLER   (NIC queue${IFACE:+, $IFACE NUMA node $NIC_NODE})"
	echo "hot candidates:    $HOT   (rank these with corelat)"
	echo "logger / copier:   ${LOGGER:-none}   (shares the poller's L3)"
	echo
	echo "use in a shell:    eval \"\$($0 layout --env)\""
	;;
offline)
	need_root
	[[ -n "${2:-}" ]] || { echo "usage: $0 offline auto [HOT] | KEEP_LIST" >&2; exit 2; }
	keeplist=$2
	if [[ $2 == auto ]]; then
		layout
		keeplist="$HOUSEKEEPING,$POLLER,${3:-$HOT}${LOGGER:+,$LOGGER}"
		echo "keeping $(compress "$(expand "${keeplist#,}")")  (housekeeping $HOUSEKEEPING, poller $POLLER, hot ${3:-$HOT}${LOGGER:+, logger $LOGGER})"
	fi
	declare -A keep
	for c in $(expand "${keeplist#,}"); do keep[$c]=1; done
	[[ -n "${keep[0]:-}" ]] || echo "note: cpu0 usually cannot be offlined; keeping it" >&2
	for d in "$sys"/cpu[0-9]*; do
		c=${d##*cpu}
		[[ -n "${keep[$c]:-}" || ! -f $d/online ]] && continue
		if [[ $(<"$d/online") == 1 ]]; then
			echo 0 >"$d/online" && echo "offlined cpu$c"
		fi
	done
	echo "online now: $(<"$sys/online")"
	;;
online)
	need_root
	for d in "$sys"/cpu[0-9]*; do
		[[ -f $d/online && $(<"$d/online") == 0 ]] && echo 1 >"$d/online" && echo "onlined ${d##*/}"
	done
	echo "online now: $(<"$sys/online")"
	;;
prefetch)
	need_root
	[[ ${2:-} == on || ${2:-} == off ]] && [[ -n "${3:-}" ]] ||
		{ echo "usage: $0 prefetch off|on CPU" >&2; exit 2; }
	grep -q GenuineIntel /proc/cpuinfo || { echo "MSR 0x1A4 is Intel-only" >&2; exit 2; }
	modprobe msr
	# Bits 0-3 disable L2 HW, L2 adjacent-line, DCU (L1) and DCU IP prefetchers.
	val=$([[ $2 == off ]] && echo 0xf || echo 0x0)
	for c in $(expand "$3"); do
		wrmsr -p "$c" 0x1a4 "$val"
		echo "cpu$c 0x1a4 = 0x$(rdmsr -p "$c" 0x1a4)"
	done
	echo "A/B test this: it can help or hurt a small hot loop."
	;;
status)
	echo "online: $(<"$sys/online")"
	echo "offline: $(<"$sys/offline")"
	if grep -q GenuineIntel /proc/cpuinfo && command -v rdmsr >/dev/null && [[ $EUID -eq 0 ]]; then
		modprobe msr
		for c in $(expand "$(<"$sys/online")"); do
			echo "cpu$c prefetch msr 0x1a4 = 0x$(rdmsr -p "$c" 0x1a4)"
		done
	fi
	;;
*)
	sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
	exit 2
	;;
esac
