# Less common techniques

These go beyond the usual isolation, C/P-states, uncore, CAT, IRQ pinning and
DPDK.

## On the box

- **Offline spare cores.** With `idle=poll`, every online core counts as busy.
  Offline the cores you don't use and the HT siblings of your hot cores
  (`scripts/cores.sh offline auto HOT`). AWS's docs explain that idle cores in
  deep sleep are what give one or two busy cores the thermal headroom to reach
  their highest turbo. Check the result with `corelat`'s `clock_GHz` (or turbostat).
- **Per-core prefetchers.** On Intel, MSR 0x1A4 switches the L1/L2 prefetchers
  off for one core (`wrmsr -p N 0x1a4 0xf`, or `scripts/cores.sh prefetch off N`).
  This can help or hurt a small hot loop, so A/B test it.
- **Shrink the hot code.** Use PGO plus BOLT to pack the hot paths together,
  then back `.text` with 2 MB pages (`MADV_COLLAPSE`, kernel 6.1+). This cuts
  i-cache and iTLB misses, which core isolation does nothing about.
- **Keep it warm.** Between real events, replay ticks through the full decision
  path and drop the order at the last step. That way the first real tick after
  a quiet spell doesn't hit cold caches.
- **Find leftover noise.** Run `rtla osnoise top -c <cpus>` (or the kernel's
  osnoise tracer directly) on the isolated cores to see what still interrupts
  them. Two common fixes
  are `mce=ignore_ce` (you lose corrected-ECC logging) and unbinding unused
  NVMe drives and ENIs.

## Network

- **Threaded NAPI busy polling** (new in kernel 6.19). A kernel thread spins on
  the NIC queue on its own core, so ordinary sockets can use it. Upstream tests
  measured it 1–2 µs faster than userspace-driven polling, with steady P5–P99.
  Test it on ENA before relying on it.
- **DPDK write-combining.** DPDK's docs say ENA's low-latency queue needs
  write-combining, which mainline vfio-pci lacks, and that running without LLQ
  costs a lot of performance on Nitro v4 and later. Check the PMD's startup log
  to confirm it's actually on. See [ena.md](ena.md).
- **Pre-bake orders.** Build the whole frame ahead of time and patch only price
  and size. For TLS venues, you can also precompute the next record's AES-GCM
  keystream, because TLS 1.3 nonces are predictable. Sending then takes only
  the patch, an XOR and a short GHASH. This needs your own TLS stack.

## Beyond the box

- **Host and gateway lottery.** Launch several metal hosts in the venue's AZ ID
  and measure RTT to every gateway IP behind its DNS
  (e.g. time TCP handshakes to each IP). Keep the fastest
  and recycle the rest. Nitro can add a nanosecond hardware timestamp to every
  inbound packet before the kernel sees it, and this works on bare metal too.
  That lets you separate network delay from host delay (`wire2app`).
- **Feed arbitration.** Subscribe to the same stream on 2–3 connections through
  different ENIs and gateway IPs. Act on whichever copy arrives first, and
  dedupe by sequence number.
- **Shared placement groups.** Since October 2022, exchanges have been able to
  share their cluster placement groups with HFT clients' AWS accounts. In One
  Trading's tests, VPC peering into the exchange's shared group was the
  lowest-latency way in. Ask your venues whether they offer this.

## Sources

- AWS: Processor state control
- Threaded NAPI busy poll merge; Linux 6.19 net/core log
- DPDK ENA PMD guide (24.07, 24.11, 26.07)
- Linux ENA driver docs; ENA queue/NAPI linking commit; ENA PHC commit log
- AWS: Hardware packet timestamps
- AWS: Shared placement groups for crypto; AWS: One Trading colocation
- One Trading: AWS colocation tests
