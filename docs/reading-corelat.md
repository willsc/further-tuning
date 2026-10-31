# Reading `corelat` output

```
  cpu  l3_id c2c_ns_from_base  chase_ns clock_GHz  note
    3      0             31.1      12.9      5.04
    1      0             47.2      14.0      5.05
# base=0 l3_id=0 chase=16 MiB (hugetlb-2M)
```

Rows are already sorted. Pick the core with the lowest `c2c_ns_from_base`.
Break near-ties on the lowest `chase_ns`, then the highest `clock_GHz`. Ignore
any difference smaller than the spread you see between two runs, so run it
twice.

## Columns

- **`c2c_ns_from_base`**: the one-way time for a cache line to travel from the
  poller (`-b`, the base CPU) to that core, measured by ping-pong. You pay it on
  every packet, so it matters most. On EPYC, cores in the poller's CCD (same
  `l3_id`) are far lower than the rest, so only consider those. On Xeon the gaps
  are smaller and reflect where each core sits on the mesh.
- **`chase_ns`**: dependent-load latency over `-s` bytes (default 16 MiB: past
  L2, inside L3). It matters whenever the hot thread's data, such as the order
  book or instrument tables, doesn't fit in L2. With `-s 2g` it measures DRAM
  latency instead.
- **`clock_GHz`**: the core's sustained clock under load. Everything the hot
  thread computes scales with it, and it's where the firmware's favoured-core
  ranking should show up.
- **`l3_id`**: the L3 (CCD on EPYC) the core belongs to. **`note`** flags the
  base core's SMT sibling.

## For a close call, estimate the cost per tick

```
cost (ns) ≈ c2c + L3_loads_per_tick × chase_ns + cycles_per_tick ÷ clock_GHz
```

`perf stat` on the hot thread gives you `cycles` per tick. Its L3-hit load
event (`mem_load_retired.l3_hit` on Intel, the equivalent on AMD) gives you the
L3 loads.

## Things to watch for

- If the footer says `(thp pages)` instead of `hugetlb-2M`, TLB misses may be
  inflating `chase_ns`. Reserve 2 MB huge pages and re-run.
- A core with a clearly lower clock or higher `chase_ns` than its neighbours
  usually has something else running on it. Check before ruling it out.
- The poller can move too. If the best-ranked core sits in a different CCD,
  put the poller on that CCD and re-run with it as `-b`.

Then confirm the winner with `wire2app`. It measures the real NIC-to-app path
on each core, so it has the final say.
