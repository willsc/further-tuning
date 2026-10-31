# How to run

A step-by-step guide to setting up a host, building the tools, and running each
check in order. It assumes the instance itself is good; these steps tune and
measure it rather than decide whether to keep it. For how to *interpret* the
results, see the guides in [docs/](docs/).

- [1. Quick setup](#1-quick-setup)
- [2. Prerequisites (manual)](#2-prerequisites-manual)
- [3. Build (manual)](#3-build-manual)
- [4. Host setup](#4-host-setup)
- [5. Run order](#5-run-order)
- [6. Tool reference](#6-tool-reference)
- [7. Troubleshooting](#7-troubleshooting)

---

## 1. Quick setup

On a fresh x86-64 Linux host (EC2 metal, Intel Xeon or AMD EPYC, is the target),
as root or with `sudo`:

```sh
git clone https://github.com/willsc/further-tuning.git && cd further-tuning
make setup                 # = sudo scripts/prereqs.sh install
```

`make setup` does, in order:

1. Installs the packages listed in [section 2](#2-prerequisites-manual) with
   `apt` or `dnf`, including `linux-tools` for the running kernel (for
   `turbostat`).
2. Loads the `msr` module.
3. Reserves 2048 huge pages (4 GiB). Use `HUGEPAGES=N make setup` for a
   different number.
4. Builds `bin/`.
5. Re-checks everything and prints `[ok]`, `[MISS]` or `[opt]` per item.

Check the host again at any time without changing it:

```sh
scripts/prereqs.sh         # exit 0 = everything required present
```

`[MISS]` lines are required and say how to fix them. `[opt]` lines are optional
features.

Huge pages and the `msr` module don't survive a reboot (`tune.sh` re-reserves
huge pages itself), so rerun `make setup`
after each boot, or add it to your AMI or boot scripts. It's safe to rerun.

After `make setup`, do the manual steps in [4.3](#43-enable-the-ena-phc-and-hardware-timestamps-for-wire2app)
(PHC, for `wire2app`), optionally [4.4](#44-optional-configure-a-dsa-work-queue-intel-for-llcpollute)
(DSA), and [4.5](#45-core-layout-from-the-grub-isolated-cores) (core layout). Then go to the
[run order](#5-run-order).

---

## 2. Prerequisites (manual)

Skip this if you ran `make setup`.

**Hardware / OS**

- x86-64 Linux: EC2 metal (Intel Xeon or AMD EPYC) is the target; any x86 box works for trying the tools.
- Root (or `sudo`) for most steps.

**Packages** (Ubuntu/Debian names; on Amazon Linux use `dnf` equivalents):

```sh
sudo apt install build-essential libc6-dev linux-libc-dev msr-tools ethtool iproute2 \
                 linux-tools-common linux-tools-$(uname -r)    # turbostat
```

| Needed for | Package / tool |
|---|---|
| Building | `gcc`, `make`, libc headers (`libc6-dev`), kernel UAPI headers (`linux-libc-dev`) |
| Prefetcher toggle | `rdmsr` / `wrmsr` (`msr-tools`) and the `msr` module, Intel only |
| Optional cross-check of turbo | `turbostat` (`linux-tools-$(uname -r)`); the steps use `corelat`'s `clock_GHz` instead |
| ENA PHC enable | `devlink` (`iproute2`), `ethtool` |
| DSA rows in `llcpollute` | `accel-config` (optional, Intel Sapphire Rapids or newer) |

---

## 3. Build (manual)

Skip this if you ran `make setup`.

```sh
make
ls bin/        # corelat  llcpollute  wire2app
```

`make clean` removes `bin/`. The scripts in `scripts/` need no build.

---

## 4. Host setup

Do these after each boot (or bake them into your AMI / boot scripts).
`make setup` already does 4.1 and 4.2.

### 4.1 Reserve 2 MB huge pages

`corelat` and `llcpollute` allocate their buffers from huge pages so TLB misses
don't distort the cache numbers. Reserve enough for the largest `-s` you'll use
(1 GiB = 512 pages; `llcpollute` needs two `-s` buffers plus `-l`):

```sh
echo 2048 | sudo tee /proc/sys/vm/nr_hugepages     # 4 GiB
grep HugePages_Free /proc/meminfo
```

If none are free, the tools fall back to transparent huge pages and their
footer says `(thp pages)` instead of `hugetlb-2M`.

### 4.2 Load the MSR driver (Intel)

```sh
sudo modprobe msr
```

### 4.3 Enable the ENA PHC and hardware timestamps (for `wire2app`)

The PHC is off by default on ENA. Find the device's PCI address, turn it on,
and reload:

```sh
ethtool -i ens5 | grep bus-info                    # e.g. 0000:00:05.0
sudo devlink dev param set pci/0000:00:05.0 name enable_phc value true cmode driverinit
sudo devlink dev reload pci/0000:00:05.0
ethtool -T ens5                                    # should now show "PTP Hardware Clock: N"
ls /dev/ptp*
```

> The reload briefly takes the interface down. Do it from the console or over
> a second ENI, not over the SSH session on `ens5`.

`wire2app` asks the driver for RX hardware timestamps itself. If your driver
version needs them enabled another way, follow AWS's "hardware packet
timestamps" docs. `ethtool -T ens5` shows what's supported.

### 4.4 (Optional) Configure a DSA work queue (Intel, for `llcpollute`)

Needs the IOMMU in scalable mode. Add `intel_iommu=on,sm_on` to the kernel
command line and reboot. Then:

```sh
sudo accel-config config-wq dsa0/wq0.0 --group-id=0 --mode=dedicated \
     --type=user --name=llcpollute --priority=10 --wq-size=32
sudo accel-config config-engine dsa0/engine0.0 --group-id=0
sudo accel-config enable-device dsa0
sudo accel-config enable-wq dsa0/wq0.0
ls -l /dev/dsa/wq0.0
```

Without this, `llcpollute` simply skips the `dsa` and `dsa-llc` rows.

### 4.5 Core layout (from the GRUB isolated cores)

Everything below takes its CPU numbers from the cores you isolated on the kernel
command line (`isolcpus=`, or `nohz_full=` when `isolcpus=` is absent). If you
haven't isolated any yet, add for example this to `GRUB_CMDLINE_LINUX` in
`/etc/default/grub`, then run `sudo update-grub` and reboot:

```
isolcpus=2-7 nohz_full=2-7 rcu_nocbs=2-7
```

Then let `cores.sh` work out the roles:

```sh
scripts/cores.sh layout
```

```
isolated at boot:  2-7
housekeeping:      0-1   (OS, IRQs, PHC sampler)
poller:            2   (NIC queue, ens5 NUMA node 0)
hot candidates:    3-6   (rank these with corelat)
logger / copier:   7   (shares the poller's L3)
HT siblings:       98-103   (not isolated in GRUB; offline auto takes them offline)
```

How the roles are picked:

| Role | Rule |
|---|---|
| Housekeeping | Every online CPU that is **not** isolated and isn't an HT sibling of an isolated core. |
| Poller | First isolated core on the NIC's NUMA node (default-route interface; set `IFACE=` to override). |
| Logger / copier | Last isolated core that shares the poller's L3. Only assigned with 3+ isolated physical cores. |
| Hot candidates | The remaining isolated cores. |

Only one hardware thread per physical core is used. HT siblings of isolated
cores (whether GRUB isolated them or not) get no role, so `offline auto` takes
them offline in step 3.

Load the roles into your shell once, then the commands below work as written:

```sh
eval "$(scripts/cores.sh layout --env)"   # sets ISOLATED HOUSEKEEPING POLLER HOT LOGGER IFACE
echo "poller=$POLLER hot=$HOT logger=$LOGGER"
```

`ISOLATED=2-7 scripts/cores.sh layout` tries a layout without rebooting.

---

## 5. Run order

**Automated:** steps 1–3 (plus the clock check) in one go:

```sh
sudo scripts/tune.sh                    # --no-offline to only measure, --no-llc to skip llcpollute
sudo scripts/tune.sh --wire2app 9000    # also measure NIC-to-app (step 5) on the chosen core
source tune.env                         # POLLER BEST LOGGER HOUSEKEEPING IFACE ...
```

It picks `BEST` as the lowest-latency `corelat` row within 5% of the fastest
clock (so a slow E-core or throttled core never wins), reserves huge pages if needed, prints
each tool's output as it goes, and writes a report with charts. See
[Viewing results](#viewing-results).

### Viewing results

Each `tune.sh` run writes to `results/<time>-<host>/`:

| File | Contents |
|---|---|
| `report.html` | Charts and tables for this run (self-contained, works offline) |
| `corelat.txt`, `llcpollute.txt`, `corelat_after.txt`, `wire2app.txt` | Raw tool output |
| `layout.env`, `meta.env` | Core roles, host, kernel, CPU model, instance type |

`results/latest` links to the newest run, and `results/index.html` lists every
run with its headline numbers and a chart comparing them.

To view from your laptop:

```sh
scripts/report.py --serve 8080                 # on the host (binds 127.0.0.1 only)
ssh -L 8080:127.0.0.1:8080 <user>@<host>       # on your laptop; open http://localhost:8080/
```

Or `scp -r <host>:further-tuning/results .` and open the files locally.

To add output you ran by hand, such as `wire2app` from step 5, save it into
the run folder under the name above and rebuild:

```sh
sudo bin/wire2app ... | tee results/latest/wire2app.txt
scripts/report.py results/latest
```

**By hand:** run these in a shell where you have already run
`eval "$(scripts/cores.sh layout --env)"`.

### Step 1: Pick the hot core

```sh
bin/corelat -b $POLLER -c $HOT          # run it twice; ignore gaps smaller than run-to-run spread
bin/corelat -b $POLLER -c $HOT -s 2g    # optional: DRAM latency instead of L3
```

Takes a few seconds per core. Pick the highest row (lowest `c2c_ns_from_base`)
whose `clock_GHz` is within 5% of the fastest core's, and record it. Skip rows
with a much lower clock: they are E-cores on hybrid CPUs, or throttled.

```sh
BEST=3                                   # the chosen row's cpu
```

→ [docs/reading-corelat.md](docs/reading-corelat.md)

### Step 2: Check the logger won't evict the hot core

The copier must share the hot core's L3 (same socket on Intel, same CCD on AMD),
or the test shows nothing. `layout` already picked a logger core that does:

```sh
sudo bin/llcpollute -v $BEST -c $LOGGER -l 8m -s 1g -t 5
```

Takes about `-t` seconds per method (5 methods → ~25 s). Compare each row to
`idle`. → [docs/reading-llcpollute.md](docs/reading-llcpollute.md)

### Step 3: Offline spare cores

With `idle=poll` every online core is busy. Offlining unused cores and the HT
siblings of hot cores gives the busy ones turbo headroom. This comes after
step 1 because you need to know which hot cores to keep:

```sh
sudo scripts/cores.sh offline auto $BEST    # keep housekeeping + poller + logger + BEST
bin/corelat -b $POLLER -c $BEST             # clock_GHz should match or beat step 1
sudo scripts/cores.sh online                # undo
```

To keep more than one hot core, list them: `offline auto 3,4`. Without a list,
`offline auto` keeps every hot candidate and offlines only HT siblings and
anything else outside the layout.

### Step 4: Optional prefetcher A/B (Intel)

```sh
sudo scripts/cores.sh prefetch off $BEST    # measure your hot loop / wire2app
sudo scripts/cores.sh prefetch on $BEST     # measure again; keep whichever wins
```

### Step 5: Confirm with real NIC-to-app latency

You need traffic arriving on the interface: either the live feed, or test
traffic from a second host in the same placement group.

On the **sender** host:

```sh
bin/wire2app -S 10.0.1.23:9000 -r 20000 -z 64      # 20k pkts/s, 64-byte payload; Ctrl-C to stop
```

On **this** host (do this before step 3 if you want to compare every hot
candidate, since offlined cores can't be measured):

```sh
sudo bin/wire2app -i $IFACE -p 9000 -c $HOT -H ${HOUSEKEEPING%%[-,]*} -n 20000
```

Each core in `-c` is measured in turn (up to `-T` seconds each). The core with
the lowest `w2a_p50_ns` and a tight `w2a_p99_ns` wins. This result overrides
`corelat`'s ranking. `-H` puts the PHC sampler on the first housekeeping core.
→ [docs/ena.md](docs/ena.md)

---

## 6. Tool reference

### `bin/corelat`

| Flag | Meaning | Default |
|---|---|---|
| `-b CPU` | Poller (base) CPU everything is measured from | required |
| `-c LIST` | Candidate cores, e.g. `3-7,10` | all allowed CPUs |
| `-s SIZE` | Pointer-chase working set (`16m` = L3, `2g` = DRAM) | `16m` |

```
  cpu  l3_id c2c_ns_from_base  chase_ns clock_GHz  note
    3      0             31.1      12.9      5.04
    1      0             47.2      14.0      5.05
# base=0 l3_id=0 chase=16 MiB (hugetlb-2M)
```

### `bin/llcpollute`

| Flag | Meaning | Default |
|---|---|---|
| `-v CPU` | Hot (victim) core | required |
| `-c CPU` | Copier core, same socket/CCD as `-v` | required |
| `-l SIZE` | Victim working set, must exceed L2 | `8m` |
| `-s SIZE` | Copier buffer, well above L3 | `512m` |
| `-t SEC` | Seconds per method | `5` |
| `-m LIST` | Methods: `idle,memcpy,nt,dsa,dsa-llc` | all available |
| `-w PATH` | DSA work queue | `/dev/dsa/wq0.0` |

```
method    victim_p50_ns  victim_p99_ns  victim_max_ns  copier_GB/s
idle               11.5           12.3           72.2         0.00
memcpy             13.0           33.5           48.4         4.27
nt                209.5          223.2          226.2        16.12
```

### `bin/wire2app`

Receive mode:

| Flag | Meaning | Default |
|---|---|---|
| `-i IFACE` | Interface, e.g. `ens5` | required |
| `-p PORT` | UDP port to receive on | required |
| `-c LIST` | Candidate cores, measured one at a time | required |
| `-g GROUP` | Multicast group to join | none |
| `-n N` | Packets per core | `20000` |
| `-T SEC` | Give up on a core after this long | `30` |
| `-H CPU` | Housekeeping core for the PHC offset sampler | unpinned |
| `-B US` | `SO_BUSY_POLL` microseconds (`0` = off) | `50` |
| `-P DEV` | PHC device | from `ethtool -T` |

Send mode: `-S HOST:PORT` plus `-r PPS` (default 10000), `-z BYTES`
(default 64), `-C CPU` (pin the sender).

```
  cpu     pkts   w2a_p50_ns   w2a_p99_ns   w2a_p999_ns   w2a_max_ns  wire2kern_p50  kern2app_p50
    3    20000         6120         7480          9900        21000           4300          1820
# iface=ens5 port=9000 phc=/dev/ptp0 busy_poll=50us
```

(Illustrative numbers, not a real ENA run. `w2a` = wire to app; `wire2kern` + `kern2app` ≈ `w2a`.)

### `scripts/prereqs.sh`

```sh
scripts/prereqs.sh                       # report only; exit 0 = all required present
sudo scripts/prereqs.sh install          # one-shot setup (what `make setup` runs)
sudo HUGEPAGES=4096 scripts/prereqs.sh install   # reserve 8 GiB instead of 4
```

### `scripts/tune.sh`

```sh
sudo scripts/tune.sh [--no-offline] [--no-llc] [--wire2app PORT]
```

Runs layout → `corelat` → `llcpollute` → `offline auto BEST` → clock check →
(`--wire2app PORT`) `wire2app` → report, saving everything to `results/<time>-<host>/`
and writing `tune.env`. Uses the same `ISOLATED=` / `IFACE=` overrides as `cores.sh layout`.

### `scripts/report.py`

```sh
scripts/report.py                     # rebuild every run's report.html and results/index.html
scripts/report.py results/latest      # rebuild one run
scripts/report.py --serve [PORT]      # rebuild, then serve results/ on 127.0.0.1:PORT (default 8080)
```

### `scripts/cores.sh`

```sh
scripts/cores.sh layout [--env]           # roles from the GRUB isolated cores (ISOLATED= / IFACE= override)
sudo scripts/cores.sh offline auto [HOT]  # keep housekeeping + poller + logger + HOT
sudo scripts/cores.sh offline KEEP_LIST   # offline every CPU not in KEEP_LIST
sudo scripts/cores.sh online              # bring all CPUs back
sudo scripts/cores.sh prefetch off|on CPU # Intel MSR 0x1A4
sudo scripts/cores.sh status
```

### Using the copy helpers in your logger

```c
#include "fastcopy.h"          /* add -I path/to/further-tuning/src */

nt_copy(dst, src, len);

struct dsa_wq wq;
if (dsa_open(&wq, "/dev/dsa/wq0.0") == 0)
	dsa_memmove(&wq, dst, src, len, 0);   /* 0 = write to memory, bypass L3 */
```

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Footer says `(thp pages)` | No free huge pages. Rerun `make setup` (or [4.1](#41-reserve-2-mb-huge-pages)); huge pages are lost on reboot. |
| `make`: `errno.h: No such file or directory` | libc headers missing. Run `make setup` (or `apt install build-essential libc6-dev`). |
| `scripts/prereqs.sh` shows `[MISS]` | Run `make setup`; the line says which package or step is missing. |
| `cores.sh layout`: `no isolated cores found` | Nothing isolated at boot. See [4.5](#45-core-layout-from-the-grub-isolated-cores), or set `ISOLATED=` to try a layout. |
| `turbostat`: `get_rapl_counters: Assertion ... failed` | A turbostat bug with RAPL on some many-core CPUs. Not needed: use `bin/corelat -b $POLLER -c $BEST` and read `clock_GHz`. |
| `llcpollute` prints its usage | A variable was empty (usually `$BEST`). Use `sudo scripts/tune.sh`, or set `BEST=` from `corelat` (step 1). |
| `$POLLER` / `$HOT` empty in a command | Run `eval "$(scripts/cores.sh layout --env)"` in this shell first. |
| `cannot pin to cpu N` | CPU is offline or outside your cgroup/cpuset. Check `cat /sys/devices/system/cpu/online`. |
| `corelat` row has much lower clock / higher `chase_ns` than neighbours | Something else runs on that core. Check `top -1` / `ps -eLo psr,comm`. |
| `llcpollute`: `no DSA work queue` | Expected without DSA. See [4.4](#44-optional-configure-a-dsa-work-queue-intel-for-llcpollute). |
| `llcpollute`: `DSA descriptor failed` | WQ not enabled, wrong `--type` (must be `user`), or IOMMU not in SVA mode (`sm_on`). |
| `llcpollute`: every row ≈ idle | Copier and victim don't share an L3, or `-l` fits in L2. Move `-c` or raise `-l`. |
| `wire2app`: `could not enable RX hardware timestamps` | Run as root; check `ethtool -T IFACE` supports hardware-receive. |
| `wire2app`: `no PHC offset` | PHC not enabled. Redo [4.3](#43-enable-the-ena-phc-and-hardware-timestamps-for-wire2app). Only `kern2app` is reported until then. |
| `wire2app`: `pkts` is 0 for a core | No traffic reached the port within `-T`. Check the sender, security groups, and `ss -ulpn`. |
