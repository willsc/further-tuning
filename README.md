# further-tuning

Latency tooling for a tuned trading host (EC2 metal, Intel Xeon or AMD EPYC).
It assumes the usual work is already done: core isolation, C/P-states, uncore,
CAT, IRQ pinning, DPDK. These tools cover the next layer: picking the hot core,
keeping a logger from evicting it, measuring the real NIC-to-app path, and
deciding whether a new host is worth keeping.

| Tool | What it answers |
|---|---|
| `bin/corelat` | Which core should the hot thread run on, relative to the poller? |
| `bin/llcpollute` | Does the logger's copy loop evict the hot core's L3 data, and does `nt_copy()` or DSA fix it? |
| `bin/wire2app` | NIC hardware RX stamp → app latency on each candidate core (has the final say). |
| `src/fastcopy.h` | `nt_copy()` and `dsa_memmove()` for the logger, header-only. |
| `scripts/hostcheck.py` | Run the osnoise, SMI and gateway-RTT checks on a fresh host, then decide keep or recycle, and rank hosts. |
| `scripts/cores.sh` | Offline spare cores and HT siblings, and toggle the Intel per-core prefetchers. |

**→ Step-by-step setup and usage: [RUNNING.md](RUNNING.md)**

## Build

```sh
make            # gcc, Linux UAPI headers; x86-64 only
```

Reserve 2 MB huge pages first so the cache measurements aren't inflated by
TLB misses (each tool's footer says `hugetlb-2M` or `thp pages`):

```sh
echo 2048 | sudo tee /proc/sys/vm/nr_hugepages
```

## Workflow on a new host

```sh
# 1. Is the host worth keeping? (60 s of osnoise on the isolated cores + gateway RTT)
sudo scripts/hostcheck.py --cpus 2-7 --gateway fix.venue.example:443 --max-rtt-us 80
scripts/hostcheck.py --rank            # compare every host launched so far

# 2. Offline cores you don't use so the busy ones get turbo headroom
sudo scripts/cores.sh offline 0-1,2,3,4   # housekeeping + poller + hot cores
sudo turbostat --quiet --show CPU,Busy%,Bzy_MHz sleep 5

# 3. Rank hot-core candidates against the poller (base) core
sudo bin/corelat -b 2 -c 3-7

# 4. Will the logger hurt the hot core? Copier on the same socket/CCD.
sudo bin/llcpollute -v 3 -c 6 -l 8m -s 1g

# 5. Confirm on the real path, with market data or test traffic flowing
bin/wire2app -S 10.0.0.5:9000 -r 20000          # on another host
sudo bin/wire2app -i ens5 -p 9000 -c 3-5 -H 1   # on this host
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
