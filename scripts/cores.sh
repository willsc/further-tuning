#!/usr/bin/env bash
# Core housekeeping for an idle=poll box.
#
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

need_root() { [[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 2; }; }

case "${1:-}" in
offline)
	need_root
	[[ -n "${2:-}" ]] || { echo "usage: $0 offline KEEP_LIST" >&2; exit 2; }
	declare -A keep
	for c in $(expand "$2"); do keep[$c]=1; done
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
	sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
	exit 2
	;;
esac
