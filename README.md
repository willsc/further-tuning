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
| `scripts/tune.sh` | One-shot: layout from the GRUB isolated cores, pick the hot core, check the logger, offline the rest, write a report. |
| `scripts/report.py` | HTML reports with charts from `tune.sh` results, and a page comparing runs; `--serve` to view over SSH. |
| `scripts/prereqs.sh` | Check prerequisites, or `install` for one-shot host setup (also `make setup`). |
| `scripts/cores.sh` | Derive CPU roles from the GRUB isolated cores, offline spare cores and HT siblings, and toggle the Intel per-core prefetchers. |

**→ Step-by-step setup and usage: [RUNNING.md](RUNNING.md)**

## Quick start

```sh
git clone https://github.com/willsc/further-tuning.git && cd further-tuning
make setup                          # packages, msr, huge pages, build (rerun after each boot)
scripts/cores.sh layout             # check the CPU roles taken from your GRUB isolated cores
sudo scripts/tune.sh                # measure, pick the hot core, offline the rest, write a report
scripts/report.py --serve 8080      # view: ssh -L 8080:127.0.0.1:8080 <user>@<host>, open localhost:8080
```

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

CPU roles come from the cores isolated on the GRUB command line (`isolcpus=`,
or `nohz_full=` when that's absent). If nothing is isolated yet, add for example
`isolcpus=2-7 nohz_full=2-7 rcu_nocbs=2-7` to `GRUB_CMDLINE_LINUX` in
`/etc/default/grub`, run `sudo update-grub` and reboot. Isolate cores that share
an L3 with each other: a hot core on another L3 (another CCD on AMD) is about
10× slower to reach from the poller.

`cores.sh layout` assigns the roles:

| Role | Rule |
|---|---|
| Housekeeping | Every online CPU that isn't isolated or an HT sibling of an isolated core |
| Poller | First isolated core on the NIC's NUMA node |
| Logger | Last isolated core sharing the poller's L3 (3+ isolated cores) |
| Hot candidates | The remaining isolated cores, one thread per physical core |

Then one command does the whole sequence:

```sh
sudo scripts/tune.sh [--no-offline] [--no-llc] [--wire2app PORT]
```

It:

1. Derives the roles above.
2. Ranks the hot candidates with `corelat` and picks the lowest-latency core
   whose clock is within 5% of the fastest. That skips slow cores, such as
   E-cores on hybrid CPUs or a throttled core.
3. Checks the logger core against it with `llcpollute`.
4. Offlines every other core, including HT siblings of isolated cores.
5. Re-measures the hot core's clock.
6. With `--wire2app PORT`, measures NIC-to-app latency on the hot core (needs
   traffic arriving on that UDP port).
7. Writes the results and an HTML report.

It also reserves huge pages if they were lost on reboot, and writes `tune.env`
(`source tune.env` for `POLLER`, `BEST`, `LOGGER`, ... in your shell). Use
`--no-offline` to only measure; `sudo scripts/cores.sh online` undoes step 4.

## Results and reports

Each run saves its raw output and an HTML report with charts to
`results/<time>-<host>/`; `results/latest` points at the newest, and
`results/index.html` compares every run. The report shows the chosen hot core,
core-to-core latency for every candidate, logger impact per copy method, the
clock after offlining and, if captured, NIC-to-app latency, each with a table
view. Without NIC hardware timestamps (PHC not enabled), it shows kernel-to-app
latency instead. Reports are single self-contained files with no network
dependencies.

View them from your laptop over SSH:

```sh
scripts/report.py --serve 8080                 # on the host
ssh -L 8080:127.0.0.1:8080 <user>@<host>       # on your laptop, then open http://localhost:8080/
```

Or copy them off: `scp -r <host>:further-tuning/results .`. Rebuild every
report after editing or adding files with `scripts/report.py`.

Add the real NIC-to-app path to the run and the report, with market data or
test traffic flowing:

```sh
bin/wire2app -S 10.0.0.5:9000 -r 20000          # on another host
sudo scripts/tune.sh --wire2app 9000            # on this host
```

The same steps by hand are in [RUNNING.md](RUNNING.md#5-run-order).

## Example run

A real `tune.sh` run on an Intel Core Ultra 9 285H (16 CPUs, no hyperthreads),
with `ISOLATED=2-7` standing in for GRUB isolation and loopback test traffic
for `--wire2app`:

| Step | Result |
|---|---|
| Layout | poller 2, hot candidates 3–6, logger 7, housekeeping 0–1 and 8–15 |
| Hot core | cpu 5, 39.8 ns from the poller (others 41–49 ns) |
| Logger impact (victim p99) | idle 15.8 ns, `memcpy` 42.3 ns, `nt` 207.6 ns |
| After offlining 3, 4, 6 | clock 4.98 → 5.06 GHz |
| Kernel to app p50 | 1,412 ns (loopback, so no NIC hardware stamps) |

On this chip `nt` hurt the hot core far more than `memcpy`, so measure it on
your own hardware rather than assuming. On an earlier pass the
lowest-latency core was a 3.35 GHz E-core; the clock rule now skips it.

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
- The steps check the clock with `corelat`'s `clock_GHz`, not `turbostat`:
  turbostat aborts in its RAPL code on some many-core CPUs.
- `wire2app` needs RX hardware timestamps and a PHC on the interface. Without
  them it still reports `kern2app`. The PHC offset is sampled 5×/s
  (`PTP_SYS_OFFSET_EXTENDED`, falling back to `PTP_SYS_OFFSET`), under ENA's
  125 reads/s limit. It has not yet been run against ENA hardware stamps.
