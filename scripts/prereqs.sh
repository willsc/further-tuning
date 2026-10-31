#!/usr/bin/env bash
# Check (and optionally install) everything the tools and scripts need.
#
#   prereqs.sh            report what is present / missing
#   prereqs.sh install    one-shot host setup: install packages (apt or dnf),
#                         load msr, reserve huge pages, build
#                         bin/, then re-check
#
#   HUGEPAGES=N           2 MB pages to reserve on install (default 2048 = 4 GiB)
#
# Exit status: 0 all required items present, 1 something required missing.
set -uo pipefail

kver=$(uname -r)
missing=0

ok()   { printf '  [ok]   %-22s %s\n' "$1" "${2:-}"; }
bad()  { printf '  [MISS] %-22s %s\n' "$1" "${2:-}"; missing=1; }
opt()  { printf '  [opt]  %-22s %s\n' "$1" "${2:-}"; }

# Ubuntu ships rtla/turbostat as wrappers that fail unless the
# linux-tools package for the *running* kernel is installed.
tool_works() {
	command -v "$1" >/dev/null 2>&1 || return 1
	! "$1" --help 2>&1 | grep -q "not found for kernel"
}

install() {
	[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0 install" >&2; exit 2; }
	if command -v apt-get >/dev/null; then
		apt-get update
		DEBIAN_FRONTEND=noninteractive apt-get install -y \
			build-essential libc6-dev linux-libc-dev msr-tools ethtool iproute2 python3 curl \
			linux-tools-common
		# Kernel-specific tools (turbostat, optional).
		if ! DEBIAN_FRONTEND=noninteractive apt-get install -y "linux-tools-$kver"; then
			echo "warning: linux-tools-$kver not in the archive; trying generic meta packages" >&2
		fi
		case "$kver" in
		*-aws)   apt-get install -y linux-tools-aws || true ;;
		*-azure) apt-get install -y linux-tools-azure || true ;;
		*-gcp)   apt-get install -y linux-tools-gcp || true ;;
		*)       apt-get install -y linux-tools-generic || true ;;
		esac
		apt-get install -y accel-config 2>/dev/null || true   # optional (DSA)
	elif command -v dnf >/dev/null; then
		dnf install -y gcc make glibc-devel kernel-headers msr-tools ethtool iproute python3 curl \
			kernel-tools || true
		dnf install -y accel-config 2>/dev/null || true
	else
		echo "no apt-get or dnf; install the packages in RUNNING.md by hand" >&2
		exit 2
	fi
	modprobe msr 2>/dev/null || true
	hp=${HUGEPAGES:-2048}
	if (( $(cat /proc/sys/vm/nr_hugepages) < hp )); then
		echo "$hp" > /proc/sys/vm/nr_hugepages
	fi
	echo "huge pages: $(awk '/HugePages_Total/ {print $2}' /proc/meminfo) reserved"
	make -C "$(dirname "$0")/.." || missing=1
	echo
}

[[ "${1:-}" == install ]] && install

echo "kernel $kver"
echo "build:"
command -v gcc  >/dev/null && ok gcc  || bad gcc  "build-essential"
command -v make >/dev/null && ok make || bad make "build-essential"
[[ -f /usr/include/errno.h ]] && ok "libc headers" \
	|| bad "libc headers" "libc6-dev (build-essential) / glibc-devel"
[[ -f /usr/include/linux/ptp_clock.h ]] && ok "kernel UAPI headers" \
	|| bad "kernel UAPI headers" "linux-libc-dev / kernel-headers"

echo "cores.sh (prefetch toggle):"
if grep -q -m1 GenuineIntel /proc/cpuinfo; then
	command -v rdmsr >/dev/null && ok rdmsr || bad rdmsr "msr-tools (cores.sh prefetch toggle)"
	[[ -e /dev/cpu/0/msr ]] && ok "msr module" || bad "msr module" "modprobe msr"
else
	opt rdmsr "not Intel: prefetch toggle unavailable"
fi

echo "reports (tune.sh, report.py):"
command -v python3 >/dev/null && ok python3 || bad python3 "python3"

echo "other tools:"
tool_works turbostat && ok turbostat || opt turbostat "linux-tools-$kver (turbo headroom check)"
command -v ethtool  >/dev/null && ok ethtool || bad ethtool
command -v devlink  >/dev/null && ok devlink || bad devlink "iproute2 (ENA PHC enable)"
command -v accel-config >/dev/null && ok accel-config || opt accel-config "optional, DSA rows in llcpollute"

echo "host state:"
hp=$(awk '/HugePages_Free/ {print $2}' /proc/meminfo)
(( hp > 0 )) && ok "huge pages" "$hp free" \
	|| opt "huge pages" "0 free: echo 2048 > /proc/sys/vm/nr_hugepages"
ls /dev/ptp* >/dev/null 2>&1 && ok PHC "$(ls /dev/ptp* | tr '\n' ' ')" \
	|| opt PHC "none: see RUNNING.md 3.3 (needed by wire2app)"
[[ -x "$(dirname "$0")/../bin/corelat" ]] && ok "built binaries" \
	|| bad "built binaries" "run make"

echo
if (( missing )); then
	echo "missing prerequisites; fix with: sudo $0 install"
else
	echo "all required prerequisites present"
fi
exit $missing
