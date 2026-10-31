# The ENA side: RX/TX queues, LLQ, PHC and hardware stamps

ENA is the network device the Nitro card presents to your host over PCIe.
Metal hosts have no hypervisor, but the Nitro card still sits between you and
the network. Three parts of it matter for latency.

## RX/TX queues

ENA gives you several TX/RX queue pairs, and each pair has its own interrupt.
Incoming packets are spread across RX queues by a hash of the flow (RSS), so a
given market-data feed always lands on the same queue. The device writes each
packet into a buffer the driver posted earlier and adds a completion entry.
Whatever polls that queue (your poller core) picks the packet up and posts a
fresh buffer.

- The driver links each queue and interrupt to a NAPI ID you can query, which
  is how the threaded busy-poll setup finds the right one.
- ENA's admin and event notifications use a separate interrupt, so keep that
  one on a housekeeping core too.
- Check which queue each feed lands on with the per-queue counters in
  `ethtool -S ens5`, and pin your poller to that queue. Alternatively, give the
  hot flows their own ENI. Leave the other queues' interrupts on housekeeping
  cores.

## LLQ (low-latency queue)

Normally, to send a packet the driver writes a descriptor into host memory and
rings a doorbell. The device then has to fetch the descriptor and headers back
over PCIe before it can transmit. With LLQ, the driver pushes the descriptor
and packet header straight into the device's memory before any DMA starts. The
ENA DPDK driver docs say this saves several microseconds.

The kernel driver uses LLQ by default. Under DPDK it needs a write-combining
mapping, which mainline vfio-pci doesn't provide, and running without LLQ costs
a lot on Nitro v4 and later. Check the PMD's startup log to confirm it's on.

## PHC and hardware timestamps

The PHC is a PTP hardware clock on the Nitro card, kept accurate by the Amazon
Time Sync Service. ENA exposes it as `/dev/ptpN`.

- It's off by default. Turn it on with the `enable_phc` devlink parameter and a
  devlink reload.
- The device allows at most 125 clock reads per second.
- chrony can use it as a reference clock.

With hardware packet timestamping enabled, every inbound packet carries a
64-bit nanosecond timestamp taken in hardware, before the kernel or your app
touches it. This works on metal and is read through the normal socket API
(`SO_TIMESTAMPING`).

That timestamp is t0 in your tick path. `wire2app` measures from it, and you
can compare it with exchange timestamps to get one-way latency.

## How `wire2app` uses it

1. Finds the PHC from `ethtool -T` (`ETHTOOL_GET_TS_INFO`), or takes `-P`.
2. Asks for RX hardware stamps with `SIOCSHWTSTAMP` (`HWTSTAMP_FILTER_ALL`). If
   the driver refuses but stamps are already on, it carries on.
3. Samples the PHC↔`CLOCK_REALTIME` offset 5×/s on the `-H` housekeeping core
   (5 samples per read, tightest window wins), so 25 reads/s, under the 125/s
   cap.
4. Per packet: `t0` = hardware stamp − offset, `t1` = kernel RX stamp,
   `t2` = `clock_gettime()` after `recvmsg()`. It reports `w2a` = t2 − t0,
   split into `wire2kern` and `kern2app`.

Offset error and PHC drift between samples (≤200 ms) add a small constant
bias that is the same for every core, so per-core comparisons stay valid even
if the absolute numbers are off by that bias.
