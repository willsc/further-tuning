# further-tuning

Latency tooling for a tuned trading host (EC2 metal, Intel Xeon or AMD EPYC).
It assumes the usual work is already done: core isolation, C/P-states, uncore,
CAT, IRQ pinning, DPDK. It also assumes the instance itself is good. These
tools cover the next layer: picking the hot core, keeping a logger from
evicting it, and measuring the real NIC-to-app path.

| Tool | What it answers |
|---|---|
| `bin/corelat` | Which core should the hot thread run on, relative to the poller? |
| `bin/llcpollute` | Does the logger's copy loop evict the hot core's L3 data, and does `nt_copy()` or DSA fix it? |
| `bin/wire2app` | NIC hardware RX stamp → app latency on each candidate core (has the final say). |
| `src/fastcopy.h` | `nt_copy()` and `dsa_memmove()` for the logger, header-only. |
| `scripts/prereqs.sh` | Check prerequisites, or `install` for one-shot host setup (also `make setup`). |
| `scripts/cores.sh` | Derive CPU roles from the GRUB isolated cores, offline spare cores and HT siblings, and toggle the Intel per-core prefetchers. |

**→ Step-by-step setup and usage: [RUNNING.md](RUNNING.md)**

## Setup

One command installs the packages, loads the `msr` module, reserves 2048 huge
pages (4 GiB) and builds `bin/`:

```sh
git clone https://github.com/willsc/further-tuning.git && cd further-tuning
make setup                 # = sudo scripts/prereqs.sh install (apt or dnf)
scripts/prereqs.sh         # re-check at any time; [MISS] lines say what to fix
```

Use `HUGEPAGES=N make setup` to reserve a different number of pages. Huge pages
don't survive a reboot, so rerun `make setup` after each boot, or add it to your
boot scripts. Each tool's footer says `hugetlb-2M` when it got them, or
`thp pages` when it didn't.

To build only (needs gcc and the libc and Linux UAPI headers; x86-64 only):

```sh
make
```

## Workflow

CPU roles come from the cores isolated on the GRUB command line (`isolcpus=` /
`nohz_full=`); `cores.sh layout` derives housekeeping, poller, hot candidates
and logger from them.

```sh
# 0. Load the CPU roles from the isolated cores into this shell
scripts/cores.sh layout                          # show them
eval "$(scripts/cores.sh layout --env)"          # POLLER HOT LOGGER HOUSEKEEPING IFACE

# 1. Rank hot-core candidates against the poller, then note the winner
bin/corelat -b $POLLER -c $HOT
BEST=3                                           # top row's cpu

# 2. Will the logger hurt the hot core? (logger shares the poller's L3)
sudo bin/llcpollute -v $BEST -c $LOGGER -l 8m -s 1g

# 3. Offline everything else so the busy cores get turbo headroom
sudo scripts/cores.sh offline auto $BEST         # keeps housekeeping + poller + logger + BEST
sudo turbostat --quiet --show CPU,Busy%,Bzy_MHz sleep 5

# 4. Confirm on the real path, with market data or test traffic flowing
bin/wire2app -S 10.0.0.5:9000 -r 20000                         # on another host
sudo bin/wire2app -i $IFACE -p 9000 -c $BEST -H ${HOUSEKEEPING%%[-,]*}   # on this host
```

## Docs

- [docs/reading-corelat.md](docs/reading-corelat.md): picking the hot core from the output.
- [docs/reading-llcpollute.md](docs/reading-llcpollute.md): reading the copy-method table.
- [docs/ena.md](docs/ena.md): ENA queues, LLQ, PHC and hardware timestamps; enabling them for `wire2app`.
- [docs/techniques.md](docs/techniques.md): less common techniques on the box, on the network, and beyond the box.

## Notes and limits

- `corelat` measures `clock_GHz` with a dependent `imul` chain, not adds. Newer
  Intel cores fold dependent add-immediate chains at rename, which made an add
  loop report ~25 GHz.
- `llcpollute` only shows `dsa` and `dsa-llc` rows when a user-mode DSA work
  queue exists (`/dev/dsa/wqX.Y`, set up with `accel-config`, IOMMU in SVA
  mode). The DSA path compiles but has not been run on DSA hardware yet.
- On a client Intel chip (Core Ultra 285H) `nt` evicted the victim *more* than
  `memcpy`, at any prefetch distance. Hardware prefetchers still pull the
  source in. Don't assume `nt` helps; measure it on the target box.
- `wire2app` needs RX hardware timestamps and a PHC on the interface. Without
  them it still reports `kern2app`. The PHC offset is sampled 5×/s
  (`PTP_SYS_OFFSET_EXTENDED`, falling back to `PTP_SYS_OFFSET`), under ENA's
  125 reads/s limit. It has not yet been run against ENA hardware stamps.
