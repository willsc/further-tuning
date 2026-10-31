# Reading `llcpollute` output

```
method    victim_p50_ns  victim_p99_ns  victim_max_ns  copier_GB/s
idle               11.5           12.3           72.2         0.00
memcpy             13.0           33.5           48.4         4.27
nt                209.5          223.2          226.2        16.12
dsa                 ...
dsa-llc             ...
```

Read every row against the `idle` row. `idle` is your hot core's normal L3
latency while nothing is copying. Each later row shows how much one copy
method pushed that latency up.

## Columns

- **`victim_p50_ns`, `victim_p99_ns`, `victim_max_ns`**: the hot core's average
  time per load, measured in batches of 10,000 loads. If its data is still in
  L3 you see L3-level latency. If the copier evicted it, loads go to DRAM and
  the number climbs. p50 shows steady eviction and p99 shows the bursts that
  cost you fills. Don't over-read max, because a single interrupt can set it.
- **`copier_GB/s`**: how fast that method copied. The logger isn't
  latency-critical, so this only has to keep up with your log volume.

## Patterns

| You see | It means |
|---|---|
| `memcpy` well above idle, `dsa` and `nt` close to idle | Ordinary copies evict your hot data; DSA or streaming stores fix it. Use `dsa_memmove()` in the logger. |
| `dsa-llc` climbing like `memcpy` while `dsa` stays flat | Confirms the cache-control flag is what helps. |
| `nt` doing as well as `dsa` | You don't need DSA on that box. `nt_copy()` is simpler and also works on AMD. |
| Every row close to idle | The two cores don't share an L3 (different sockets or, on c8a, different CCDs), or the working set fits in L2. On c8a that's the result you want: keep the logger on another CCD. |
| Every copying row up by a similar amount, including `nt` and `dsa` | The cause is memory bandwidth, not eviction. Slow the copier down, or cap it with resctrl's memory bandwidth allocation. |

Without a DSA work queue you only get the `idle`, `memcpy` and `nt` rows.

## For a valid test

- Put the copier (`-c`) on the same socket (Intel) or CCD (AMD) as the hot
  core (`-v`).
- Set `-l` to roughly your hot thread's real working set. It must be bigger
  than L2, which is 2 MB per core on Xeon and 1 MB on Zen 5.
- Keep `-s` well above the L3 size, so the copier really churns it.

## Using the copies in the logger

```c
#include "fastcopy.h"

nt_copy(dst, src, len);                 /* Intel and AMD */

struct dsa_wq wq;
dsa_open(&wq, "/dev/dsa/wq0.0");
dsa_memmove(&wq, dst, src, len, 0);     /* llc=0: write to memory, not L3 */
```
