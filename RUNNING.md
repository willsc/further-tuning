# How to run

A step-by-step guide to building the tools, preparing a host, and running each
check in order. For how to *interpret* the results, see the guides in
[docs/](docs/).

- [1. Prerequisites](#1-prerequisites)
- [2. Build](#2-build)
- [3. One-time host setup](#3-one-time-host-setup)
- [4. Run order on a new host](#4-run-order-on-a-new-host)
- [5. Tool reference](#5-tool-reference)
- [6. Troubleshooting](#6-troubleshooting)

---

## 1. Prerequisites

**Hardware / OS**

- x86-64 Linux: EC2 metal (Intel Xeon or AMD EPYC) is the target; any x86 box works for trying the tools.
- Root (or `sudo`) for most steps.

**Packages** (Ubuntu/Debian names; on Amazon Linux use `dnf` equivalents):

```sh
sudo apt install build-essential msr-tools ethtool iproute2 python3 \
                 linux-tools-common linux-tools-$(uname -r)    # turbostat
```

| Needed for | Package / tool |
|---|---|
| Building | `gcc`, `make`, kernel UAPI headers (`linux-libc-dev`) |
| `hostcheck.py` noise check | kernel osnoise tracer + tracefs (no `rtla` needed) |
| SMI count | `msr` kernel module, Intel only |
| Prefetcher toggle | `rdmsr` / `wrmsr` (`msr-tools`), Intel only |
| Turbo headroom check | `turbostat` |
| ENA PHC enable | `devlink` (`iproute2`) |
| DSA rows in `llcpollute` | `accel-config` (optional, Intel Sapphire Rapids or newer) |

Or do the whole setup in one go (packages, `msr` module, tracefs, 2048 huge
pages, `make`):

```sh
make setup                      # = sudo scripts/prereqs.sh install
scripts/prereqs.sh              # re-check any time
```

---

## 2. Build

```sh
git clone <this repo> further-tuning && cd further-tuning
make
ls bin/        # corelat  llcpollute  wire2app
```

`make clean` removes `bin/`. The scripts in `scripts/` need no build.

---

## 3. One-time host setup

Do these after each boot (or bake them into your AMI / boot scripts).

### 3.1 Reserve 2 MB huge pages

`corelat` and `llcpollute` allocate their buffers from huge pages so TLB misses
don't distort the cache numbers. Reserve enough for the largest `-s` you'll use
(1 GiB = 512 pages; `llcpollute` needs two `-s` buffers plus `-l`):

```sh
echo 2048 | sudo tee /proc/sys/vm/nr_hugepages     # 4 GiB
grep HugePages_Free /proc/meminfo
```

If none are free, the tools fall back to transparent huge pages and their
footer says `(thp pages)` instead of `hugetlb-2M`.

### 3.2 Load the MSR driver (Intel)

```sh
sudo modprobe msr
```

### 3.3 Enable the ENA PHC and hardware timestamps (for `wire2app`)

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

### 3.4 (Optional) Configure a DSA work queue (Intel, for `llcpollute`)

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

### 3.5 Know your core layout

Before choosing CPU numbers, look at the topology:

```sh
lscpu -e=CPU,CORE,SOCKET,NODE,CACHE      # which CPUs share a core, socket, L3
cat /sys/devices/system/cpu/isolated      # cores isolated with isolcpus=
```

The examples below use this layout. Substitute your own:

| Role | CPU |
|---|---|
| Housekeeping (OS, IRQs, PHC sampler) | 0–1 |
| Poller (NIC queue) | 2 |
| Hot-core candidates | 3–7 |
| Logger / copier | 8 |

---

## 4. Run order on a new host

### Step 1: Keep or recycle the host?

Run 60 s of noise measurement on the isolated cores, count SMIs, and time TCP
handshakes to every IP behind each venue gateway:

```sh
sudo scripts/hostcheck.py --cpus 3-7 \
     --gateway fix.venue-a.example:443 --gateway md.venue-a.example:9000 \
     --max-single-us 10 --max-rtt-us 80
echo $?          # 0 = keep, 1 = recycle, 2 = error
```

Every run is appended to `hostcheck.jsonl`. After you've checked several
hosts (copy the log lines into one file), rank them:

```sh
scripts/hostcheck.py --rank --log hostcheck.jsonl
```

Keep the fastest, terminate the rest.

### Step 2: Offline spare cores

With `idle=poll` every online core is busy. Offlining unused cores and the HT
siblings of hot cores gives the busy ones turbo headroom:

```sh
sudo scripts/cores.sh offline 0-8          # list every CPU you want KEPT
sudo turbostat --quiet --show CPU,Busy%,Bzy_MHz,CPU%c6 sleep 5
sudo scripts/cores.sh online               # undo
```

### Step 3: Pick the hot core

```sh
bin/corelat -b 2 -c 3-7          # run it twice; ignore gaps smaller than run-to-run spread
bin/corelat -b 2 -c 3-7 -s 2g    # optional: DRAM latency instead of L3
```

Takes a few seconds per core. Pick the top row (lowest `c2c_ns_from_base`,
then `chase_ns`, then highest `clock_GHz`). → [docs/reading-corelat.md](docs/reading-corelat.md)

### Step 4: Check the logger won't evict the hot core

The copier must share the hot core's L3 (same socket on Intel, same CCD on AMD),
or the test shows nothing:

```sh
sudo bin/llcpollute -v 3 -c 8 -l 8m -s 1g -t 5
```

Takes about `-t` seconds per method (5 methods → ~25 s). Compare each row to
`idle`. → [docs/reading-llcpollute.md](docs/reading-llcpollute.md)

### Step 5: Optional prefetcher A/B (Intel)

```sh
sudo scripts/cores.sh prefetch off 3     # measure your hot loop / wire2app
sudo scripts/cores.sh prefetch on 3      # measure again; keep whichever wins
```

### Step 6: Confirm with real NIC-to-app latency

You need traffic arriving on the interface: either the live feed, or test
traffic from a second host in the same placement group.

On the **sender** host:

```sh
bin/wire2app -S 10.0.1.23:9000 -r 20000 -z 64      # 20k pkts/s, 64-byte payload; Ctrl-C to stop
```

On **this** host:

```sh
sudo bin/wire2app -i ens5 -p 9000 -c 3-5 -H 1 -n 20000
```

Each core in `-c` is measured in turn (up to `-T` seconds each). The core with
the lowest `w2a_p50_ns` and a tight `w2a_p99_ns` wins. This result overrides
`corelat`'s ranking. → [docs/ena.md](docs/ena.md)

---

## 5. Tool reference

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

### `scripts/hostcheck.py`

| Flag | Meaning | Default |
|---|---|---|
| `--cpus LIST` | Isolated cores to check for noise | none |
| `--duration SEC` | osnoise / SMI window | `60` |
| `--gateway HOST:PORT` | Venue gateway; repeatable; every A record is tested | none |
| `--samples N` | TCP handshakes per IP | `20` |
| `--max-single-us N` | Recycle if any single interruption exceeds this | `10` |
| `--max-smi N` | Recycle if more SMIs than this | `0` |
| `--max-rtt-us N` | Recycle if best gateway p50 RTT exceeds this | no limit |
| `--log FILE` | JSON-lines results log | `hostcheck.jsonl` |
| `--rank` | Rank all hosts in `--log` and exit | |

Give at least one of `--cpus` / `--gateway`. The gateway must accept TCP
connections on that port.

### `scripts/cores.sh`

```sh
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

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Footer says `(thp pages)` | No free huge pages. Redo [3.1](#31-reserve-2-mb-huge-pages). |
| `cannot pin to cpu N` | CPU is offline or outside your cgroup/cpuset. Check `cat /sys/devices/system/cpu/online`. |
| `corelat` row has much lower clock / higher `chase_ns` than neighbours | Something else runs on that core. Check `top -1` / `ps -eLo psr,comm`. |
| `llcpollute`: `no DSA work queue` | Expected without DSA. See [3.4](#34-optional-configure-a-dsa-work-queue-intel-for-llcpollute). |
| `llcpollute`: `DSA descriptor failed` | WQ not enabled, wrong `--type` (must be `user`), or IOMMU not in SVA mode (`sm_on`). |
| `llcpollute`: every row ≈ idle | Copier and victim don't share an L3, or `-l` fits in L2. Move `-c` or raise `-l`. |
| `wire2app`: `could not enable RX hardware timestamps` | Run as root; check `ethtool -T IFACE` supports hardware-receive. |
| `wire2app`: `no PHC offset` | PHC not enabled. Redo [3.3](#33-enable-the-ena-phc-and-hardware-timestamps-for-wire2app). Only `kern2app` is reported until then. |
| `wire2app`: `pkts` is 0 for a core | No traffic reached the port within `-T`. Check the sender, security groups, and `ss -ulpn`. |
| `hostcheck.py`: `kernel has no osnoise tracer` | Kernel built without `CONFIG_OSNOISE_TRACER`; use a stock Ubuntu/AL2023 kernel. |
| `hostcheck.py`: `tracer '…' is in use` | Another tracer is active; `echo nop > /sys/kernel/tracing/current_tracer`. |
| `hostcheck.py`: `smi: unavailable` | AMD (no MSR 0x34), or run `modprobe msr`. |
| `hostcheck.py`: `0/20 ok` for a gateway | Port closed/filtered from this host. Use the venue's real order-entry or feed port. |
