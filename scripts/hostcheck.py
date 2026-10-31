#!/usr/bin/env python3
"""Vet a freshly launched host: keep it or recycle it.

Checks:
  osnoise  kernel osnoise tracer on the isolated cores; worst single interruption
  smi      SMI count delta (Intel MSR 0x34) over the same window
  rtt      TCP handshake RTT to every IP behind each venue gateway name

Every run is appended to a JSON-lines log so hosts launched for the
"host lottery" can be ranked against each other with --rank.

Exit status: 0 keep, 1 recycle, 2 error.
"""
import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import urllib.request


def imds(path):
    """Read EC2 instance metadata (IMDSv2); None when not on EC2."""
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        token = urllib.request.urlopen(req, timeout=1).read().decode()
        req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/" + path,
            headers={"X-aws-ec2-metadata-token": token})
        return urllib.request.urlopen(req, timeout=1).read().decode()
    except OSError:
        return None


def expand_cpus(spec):
    cpus = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        cpus.extend(range(int(a), int(b or a) + 1))
    return cpus


def read_smi(cpu):
    """MSR_SMI_COUNT (0x34), read straight from /dev/cpu/N/msr (Intel only)."""
    path = f"/dev/cpu/{cpu}/msr"
    if not os.path.exists(path):
        subprocess.run(["modprobe", "msr"], capture_output=True)
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            return int.from_bytes(os.pread(fd, 8, 0x34), "little") & 0xFFFFFFFF
        finally:
            os.close(fd)
    except OSError:
        return None


def tracefs():
    for d in ("/sys/kernel/tracing", "/sys/kernel/debug/tracing"):
        if os.path.exists(f"{d}/available_tracers"):
            return d
    subprocess.run(["mount", "-t", "tracefs", "nodev", "/sys/kernel/tracing"],
                   capture_output=True)
    if os.path.exists("/sys/kernel/tracing/available_tracers"):
        return "/sys/kernel/tracing"
    raise RuntimeError("tracefs not available (run as root; kernel needs CONFIG_TRACING)")


# <task>-<pid> [cpu] flags timestamp: RUNTIME NOISE %AVAIL MAX_SINGLE HW NMI IRQ SIRQ THREAD
OSNOISE_LINE = re.compile(
    r"\[(\d+)\]\s+\S+\s+[\d.]+:\s+(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)"
    r"\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$")


def run_osnoise(cpus, seconds):
    """Run the kernel osnoise tracer on `cpus` for `seconds` via tracefs
    (what `rtla osnoise top` does, without needing rtla installed).
    Returns {cpu: {...}}; times are in us."""
    t = tracefs()
    with open(f"{t}/available_tracers") as f:
        if "osnoise" not in f.read().split():
            raise RuntimeError("kernel has no osnoise tracer (CONFIG_OSNOISE_TRACER)")

    def rd(name):
        with open(f"{t}/{name}") as f:
            return f.read().strip()

    def wr(name, val):
        with open(f"{t}/{name}", "w") as f:
            f.write(str(val))

    old = {k: rd(k) for k in ("current_tracer", "tracing_on", "osnoise/cpus",
                              "osnoise/period_us", "osnoise/runtime_us")}
    if old["current_tracer"] not in ("nop", "osnoise"):
        raise RuntimeError(f"tracer '{old['current_tracer']}' is in use; "
                           f"stop it first (echo nop > {t}/current_tracer)")
    try:
        wr("current_tracer", "nop")
        wr("osnoise/cpus", cpus)
        wr("osnoise/period_us", 1000000)
        wr("osnoise/runtime_us", 1000000)       # sample 100% of each period
        wr("trace", "")                         # clear the buffer
        wr("tracing_on", 1)
        wr("current_tracer", "osnoise")
        time.sleep(seconds)
        wr("tracing_on", 0)
        trace = rd("trace")
    finally:
        wr("current_tracer", "nop")
        for k in ("osnoise/cpus", "osnoise/period_us", "osnoise/runtime_us",
                  "tracing_on"):
            try:
                wr(k, old[k])
            except OSError:
                pass

    res = {}
    for line in trace.splitlines():
        m = OSNOISE_LINE.search(line)
        if not m:
            continue
        cpu = int(m[1])
        runtime, noise, max_single = int(m[2]), int(m[3]), int(m[5])
        r = res.setdefault(cpu, {"runtime_us": 0, "noise_us": 0, "max_noise_us": 0,
                                 "max_single_us": 0, "hw": 0, "nmi": 0, "irq": 0,
                                 "softirq": 0, "thread": 0})
        r["runtime_us"] += runtime
        r["noise_us"] += noise
        r["max_noise_us"] = max(r["max_noise_us"], noise)
        r["max_single_us"] = max(r["max_single_us"], max_single)
        for k, i in (("hw", 6), ("nmi", 7), ("irq", 8), ("softirq", 9), ("thread", 10)):
            r[k] += int(m[i])
    for r in res.values():
        r["avail_pct"] = round(100 * (1 - r["noise_us"] / r["runtime_us"]), 5) \
            if r["runtime_us"] else None
    if not res:
        raise RuntimeError("osnoise produced no samples (are the cpus online?)")
    return res


def tcp_rtt_us(ip, port, samples):
    """SYN -> SYN/ACK round trips, timed around connect()."""
    rtts = []
    for _ in range(samples):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        t0 = time.perf_counter_ns()
        try:
            s.connect((ip, port))
            rtts.append((time.perf_counter_ns() - t0) / 1000)
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(0.02)
    return rtts


def check_rtt(gateways, samples):
    res = []
    for gw in gateways:
        host, _, port = gw.rpartition(":")
        try:
            ips = sorted({ai[4][0] for ai in socket.getaddrinfo(
                host, int(port), socket.AF_INET, socket.SOCK_STREAM)})
        except (OSError, ValueError) as e:
            print(f"  {gw}: cannot resolve ({e})", file=sys.stderr)
            res.append({"gateway": gw, "ip": None, "ok": 0,
                        "min_us": None, "p50_us": None})
            continue
        for ip in ips:
            rtts = tcp_rtt_us(ip, int(port), samples)
            res.append({
                "gateway": gw, "ip": ip, "ok": len(rtts),
                "min_us": round(min(rtts), 1) if rtts else None,
                "p50_us": round(statistics.median(rtts), 1) if rtts else None,
            })
    return res


def rank(log):
    rows = []
    if not os.path.exists(log):
        print(f"no results log '{log}' yet: run a check first "
              "(e.g. sudo scripts/hostcheck.py --cpus 2-7 --gateway HOST:PORT), "
              "or point --log at an existing file", file=sys.stderr)
        return 2
    with open(log) as f:
        for line in f:
            r = json.loads(line)
            best = [g["p50_us"] for g in r.get("rtt", []) if g["p50_us"] is not None]
            rows.append((min(best) if best else float("inf"), r))
    rows.sort(key=lambda x: x[0])
    print(f"{'host':<22} {'az_id':<10} {'best_rtt_p50_us':>15} {'max_single_us':>13} "
          f"{'smi':>5}  verdict")
    for best, r in rows:
        print(f"{r['host']:<22} {str(r.get('az_id')):<10} {best:>15.1f} "
              f"{str(r.get('max_single_us')):>13} {str(r.get('smi_delta')):>5}  {r['verdict']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cpus", help="isolated hot cores to check for noise, e.g. 2-5")
    ap.add_argument("--duration", type=int, default=60, help="osnoise/SMI window (s)")
    ap.add_argument("--gateway", action="append", default=[],
                    help="venue gateway host:port (repeatable); every A record is tested")
    ap.add_argument("--samples", type=int, default=20, help="TCP handshakes per IP")
    ap.add_argument("--max-single-us", type=int, default=10,
                    help="recycle if any interruption on a hot core exceeds this")
    ap.add_argument("--max-smi", type=int, default=0, help="recycle if more SMIs than this")
    ap.add_argument("--max-rtt-us", type=float,
                    help="recycle if the best gateway p50 RTT is above this")
    ap.add_argument("--log", default="hostcheck.jsonl", help="results log (JSON lines)")
    ap.add_argument("--rank", action="store_true", help="rank hosts in --log and exit")
    a = ap.parse_args()

    if a.rank:
        return rank(a.log) or 0
    if not a.cpus and not a.gateway:
        ap.error("give --cpus and/or --gateway")

    rec = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": imds("instance-id") or socket.gethostname(),
        "instance_type": imds("instance-type"),
        "az_id": imds("placement/availability-zone-id"),
    }
    reasons = []

    if a.cpus and os.geteuid():
        print("error: the noise/SMI checks need root (sudo)", file=sys.stderr)
        return 2
    if a.cpus:
        cpus = expand_cpus(a.cpus)
        smi0 = read_smi(cpus[0])
        print(f"osnoise on cpus {a.cpus} for {a.duration}s...", file=sys.stderr)
        try:
            noise = run_osnoise(a.cpus, a.duration)
        except (OSError, RuntimeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        smi1 = read_smi(cpus[0])
        rec["osnoise"] = noise
        rec["max_single_us"] = max((n["max_single_us"] for n in noise.values()), default=None)
        rec["smi_delta"] = smi1 - smi0 if smi0 is not None and smi1 is not None else None
        for cpu, n in sorted(noise.items()):
            print(f"  cpu {cpu:3d}: max_single {n['max_single_us']:6d} us  "
                  f"noise {n['noise_us']:8d} us  hw {n['hw']} nmi {n['nmi']} "
                  f"irq {n['irq']} softirq {n['softirq']} thread {n['thread']}")
            if n["max_single_us"] > a.max_single_us:
                reasons.append(f"cpu {cpu} max single noise {n['max_single_us']} us "
                               f"> {a.max_single_us}")
        if rec["smi_delta"] is None:
            print("  smi: unavailable (not Intel, or msr module missing)")
        else:
            print(f"  smi: {rec['smi_delta']} in {a.duration}s")
            if rec["smi_delta"] > a.max_smi:
                reasons.append(f"{rec['smi_delta']} SMIs > {a.max_smi}")

    if a.gateway:
        rec["rtt"] = check_rtt(a.gateway, a.samples)
        for g in rec["rtt"]:
            print(f"  {g['gateway']:<30} {str(g['ip']):<16} min {g['min_us']} us  "
                  f"p50 {g['p50_us']} us  ({g['ok']}/{a.samples} ok)")
        best = [g["p50_us"] for g in rec["rtt"] if g["p50_us"] is not None]
        if not best:
            reasons.append("no gateway reachable")
        elif a.max_rtt_us is not None and min(best) > a.max_rtt_us:
            reasons.append(f"best gateway p50 {min(best)} us > {a.max_rtt_us}")

    rec["verdict"] = "recycle" if reasons else "keep"
    rec["reasons"] = reasons
    with open(a.log, "a") as f:
        f.write(json.dumps(rec) + "\n")

    print(f"\n{rec['verdict'].upper()}" + "".join(f"\n  - {r}" for r in reasons))
    if os.path.getsize(a.log) and a.gateway:
        print(f"(compare hosts with: {sys.argv[0]} --rank --log {a.log})")
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
