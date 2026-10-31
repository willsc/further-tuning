#!/usr/bin/env python3
"""Turn tune.sh results into viewable HTML reports with charts.

    scripts/report.py [RUN_DIR ...]      build RUN_DIR/report.html for each run
                                         (default: every run under results/)
                                         and results/index.html comparing runs
    scripts/report.py --serve [PORT]     build, then serve results/ on
                                         127.0.0.1:PORT (default 8080)

View from your laptop through an SSH tunnel:
    ssh -L 8080:127.0.0.1:8080 user@host   then open http://localhost:8080/

A run directory holds whatever was captured: layout.env, corelat.txt,
llcpollute.txt, corelat_after.txt, wire2app.txt, meta.env. Missing files
just leave their section out. Reports are single self-contained files (no
network access needed), so they can also be copied off with scp.
"""
import html
import http.server
import json
import os
import socketserver
import sys
from functools import partial

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def parse_env(text):
    env = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            env[k.strip()] = v.strip()
    return env


def num(s):
    try:
        return float(s)
    except ValueError:
        return None


def parse_table(text, ncols):
    """Rows of whitespace-separated fields; returns (rows, footer_lines)."""
    rows, footer = [], []
    if not text:
        return rows, footer
    lines = text.splitlines()
    for line in lines[1:]:
        if line.startswith("#"):
            footer.append(line[1:].strip())
            continue
        f = line.split()
        if len(f) >= ncols:
            rows.append(f)
    return rows, footer


def parse_corelat(text):
    rows, footer = parse_table(text, 5)
    out = []
    for f in rows:
        if not f[0].isdigit():
            continue
        out.append({"cpu": int(f[0]), "l3": int(f[1]), "c2c": num(f[2]),
                    "chase": num(f[3]), "ghz": num(f[4]),
                    "note": " ".join(f[5:])})
    return out, " ".join(footer)


def parse_llc(text):
    rows, footer = parse_table(text, 5)
    out = []
    for f in rows:
        if num(f[1]) is None:
            continue
        out.append({"method": f[0], "p50": num(f[1]), "p99": num(f[2]),
                    "max": num(f[3]), "gbs": num(f[4])})
    return out, " ".join(footer)


def parse_w2a(text):
    rows, footer = parse_table(text, 8)
    out = []
    for f in rows:
        if not f[0].isdigit():
            continue
        out.append({"cpu": int(f[0]), "pkts": int(f[1]), "p50": num(f[2]),
                    "p99": num(f[3]), "p999": num(f[4]), "max": num(f[5]),
                    "w2k": num(f[6]), "k2a": num(f[7])})
    return out, " ".join(footer)


def load_run(d):
    env = parse_env(read(os.path.join(d, "layout.env")))
    meta = parse_env(read(os.path.join(d, "meta.env")))
    corelat, cl_foot = parse_corelat(read(os.path.join(d, "corelat.txt")))
    after, af_foot = parse_corelat(read(os.path.join(d, "corelat_after.txt")))
    llc, llc_foot = parse_llc(read(os.path.join(d, "llcpollute.txt")))
    w2a, w2a_foot = parse_w2a(read(os.path.join(d, "wire2app.txt")))
    poller_l3 = None
    for part in cl_foot.split():
        if part.startswith("l3_id="):
            poller_l3 = int(part.split("=")[1])
    return {
        "name": os.path.basename(os.path.normpath(d)),
        "env": env, "meta": meta,
        "corelat": corelat, "corelat_footer": cl_foot, "poller_l3": poller_l3,
        "after": after, "after_footer": af_foot,
        "llc": llc, "llc_footer": llc_foot,
        "w2a": w2a, "w2a_footer": w2a_foot,
    }


def summary(run):
    """Headline numbers for tiles and the index page."""
    env, s = run["env"], {}
    best = env.get("BEST")
    s["best"] = best
    row = next((r for r in run["corelat"] if str(r["cpu"]) == best), None)
    if row:
        s["c2c"], s["ghz_before"], s["chase"] = row["c2c"], row["ghz"], row["chase"]
    arow = next((r for r in run["after"] if str(r["cpu"]) == best), None)
    if arow:
        s["ghz_after"] = arow["ghz"]
    idle = next((r for r in run["llc"] if r["method"] == "idle"), None)
    others = [r for r in run["llc"] if r["method"] != "idle"]
    if idle and others:
        worst = max(others, key=lambda r: r["p99"])
        s["llc_idle_p99"], s["llc_worst"] = idle["p99"], worst
    if run["w2a"]:
        ok = [r for r in run["w2a"] if r["p50"] is not None]
        if ok:
            s["w2a_best"] = min(ok, key=lambda r: r["p50"])
    return s


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --other: #c3c2b7;
  --good: #006300; --bad: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --other: #4a4a46;
    --good: #0ca30c; --bad: #e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --other: #4a4a46;
  --good: #0ca30c; --bad: #e66767;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 24px 16px 64px; }
header { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
h1 { font-size: 22px; margin: 0; }
h2 { font-size: 16px; margin: 0 0 2px; }
.sub { color: var(--ink-2); margin: 0 0 12px; }
.muted { color: var(--muted); }
a { color: var(--s1); }
button.theme { background: var(--surface); color: var(--ink-2); border: 1px solid var(--ring);
  border-radius: 6px; padding: 4px 10px; font: inherit; cursor: pointer; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }
.tile, .card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; }
.tile { padding: 14px 16px; }
.tile .label { color: var(--ink-2); font-size: 13px; }
.tile .value { font-size: 26px; font-weight: 600; margin-top: 2px; }
.tile .delta { font-size: 13px; color: var(--ink-2); }
.up { color: var(--good); } .down { color: var(--bad); }
.card { padding: 16px; margin: 16px 0; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; color: var(--ink-2); font-size: 13px; margin: 4px 0 8px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
.chart { position: relative; width: 100%; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.chart text { fill: var(--ink-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.chart .tick { fill: var(--muted); font-size: 11px; }
.tip { position: absolute; pointer-events: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); white-space: nowrap; display: none; z-index: 2; }
.tip b { display: block; margin-bottom: 2px; }
.tip i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--ink-2); font-size: 13px; }
.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 4px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--ink-2); font-weight: 600; }
tr.hl td { font-weight: 600; }
dl.roles { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 8px 0 0; }
dl.roles dt { color: var(--ink-2); } dl.roles dd { margin: 0; font-variant-numeric: tabular-nums; }
code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
</style>
</head>
<body>
<main>
__BODY__
</main>
<div class="tip" id="tip"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  var root = document.documentElement;
  var btn = document.getElementById("theme");
  if (btn) btn.onclick = function () {
    var dark = root.dataset.theme ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
  };
  var D = JSON.parse(document.getElementById("data").textContent);
  var tip = document.getElementById("tip");
  var NS = "http://www.w3.org/2000/svg";
  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function fmt(v, d) { return v == null ? "–" : v.toLocaleString(undefined, {maximumFractionDigits: d == null ? 1 : d}); }
  function niceMax(v) {
    if (v <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
  }
  // Rounded data-end, square at the baseline (x0).
  function barPath(x0, x1, y, h) {
    var r = Math.min(4, (x1 - x0) / 2, h / 2);
    if (x1 - x0 < 0.5) return "";
    return "M" + x0 + "," + y + "H" + (x1 - r) + "Q" + x1 + "," + y + " " + x1 + "," + (y + r) +
      "V" + (y + h - r) + "Q" + x1 + "," + (y + h) + " " + (x1 - r) + "," + (y + h) + "H" + x0 + "Z";
  }
  function showTip(ev, html, host) {
    tip.innerHTML = html; tip.style.display = "block";
    var r = host.getBoundingClientRect(), w = tip.offsetWidth;
    var x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
    if (x + w > r.width) x = ev.clientX - r.left - w - 14;
    host.appendChild(tip);
    tip.style.left = Math.max(0, x) + "px"; tip.style.top = y + "px";
  }
  function hideTip() { tip.style.display = "none"; }

  // Horizontal grouped bars. spec: {cats, series:[{name,color,values}], unit,
  // colorOf(ci,si) optional, label(ci) -> text at tip, tipExtra(ci)}
  function hbar(host, spec) {
    function draw() {
      host.innerHTML = "";
      var W = Math.max(280, host.clientWidth), padR = 72;
      var longest = Math.max.apply(null, spec.cats.map(function (c) { return String(c).length; }));
      var labelW = Math.min(Math.round(W * 0.45), Math.max(56, longest * 7 + 14));
      var ns = spec.series.length, barH = ns === 1 ? 16 : Math.min(12, 24), gap = 2;
      var groupH = ns * barH + (ns - 1) * gap, rowH = groupH + 10, top = 6;
      var H = top + spec.cats.length * rowH + 24;
      var svg = el("svg", {viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": spec.aria || ""}, host);
      var max = 0;
      spec.series.forEach(function (s) { s.values.forEach(function (v) { if (v != null && v > max) max = v; }); });
      var step = niceMax(max / 4); max = step * Math.ceil(max / step - 1e-9);
      var nt = Math.round(max / step);
      var x0 = labelW, x1 = W - padR, sx = function (v) { return x0 + (x1 - x0) * v / max; };
      for (var t = 0; t <= nt; t++) {
        var v = step * t, x = sx(v);
        el("line", {x1: x, x2: x, y1: top - 4, y2: H - 22, stroke: t ? "var(--grid)" : "var(--axis)", "stroke-width": 1}, svg);
        var tx = el("text", {x: x, y: H - 6, "text-anchor": "middle", "class": "tick"}, svg);
        tx.textContent = fmt(v, max < 10 ? 1 : 0) + (t === nt && spec.unit ? " " + spec.unit : "");
      }
      spec.cats.forEach(function (c, ci) {
        var gy = top + ci * rowH;
        var lab = el("text", {x: labelW - 8, y: gy + groupH / 2 + 4, "text-anchor": "end"}, svg);
        lab.textContent = c;
        spec.series.forEach(function (s, si) {
          var v = s.values[ci];
          if (v == null) return;
          var y = gy + si * (barH + gap);
          var color = spec.colorOf ? spec.colorOf(ci, si) : s.color;
          el("path", {d: barPath(x0, sx(v), y, barH), fill: color}, svg);
        });
        if (spec.label) {
          var txt = spec.label(ci);
          if (txt) {
            var vmax = Math.max.apply(null, spec.series.map(function (s) { return s.values[ci] || 0; }));
            var lt = el("text", {x: sx(vmax) + 6, y: gy + groupH / 2 + 4}, svg);
            lt.textContent = txt; lt.style.fill = "var(--ink)"; lt.style.fontWeight = 600;
          }
        }
        var hit = el("rect", {x: 0, y: gy - 4, width: W, height: rowH, fill: "transparent"}, svg);
        hit.addEventListener("mousemove", function (ev) {
          var h = "<b>" + (spec.tipTitle ? spec.tipTitle(ci) : c) + "</b>";
          spec.series.forEach(function (s, si) {
            var color = spec.colorOf ? spec.colorOf(ci, si) : s.color;
            h += "<div><i style='background:" + color + "'></i>" + s.name + ": " +
              fmt(s.values[ci], s.digits) + (s.unit || spec.unit ? " " + (s.unit || spec.unit) : "") + "</div>";
          });
          if (spec.tipExtra) h += spec.tipExtra(ci);
          showTip(ev, h, host);
        });
        hit.addEventListener("mouseleave", hideTip);
      });
    }
    draw();
    var last = host.clientWidth;
    addEventListener("resize", function () { if (host.clientWidth !== last) { last = host.clientWidth; draw(); } });
  }

  var best = D.env.BEST != null ? +D.env.BEST : null;
  var c = document.getElementById("chart-corelat");
  if (c && D.corelat.length) {
    var rows = D.corelat;
    hbar(c, {
      aria: "Core-to-core latency from the poller, per candidate core",
      cats: rows.map(function (r) { return "cpu " + r.cpu; }),
      series: [{name: "c2c from poller", values: rows.map(function (r) { return r.c2c; }), unit: "ns"}],
      colorOf: function (ci) { return rows[ci].l3 === D.poller_l3 ? "var(--s1)" : "var(--other)"; },
      label: function (ci) {
        if (rows[ci].cpu === best) return fmt(rows[ci].c2c) + " ns  ← hot core";
        if (ci === rows.length - 1) return fmt(rows[ci].c2c) + " ns";
        return "";
      },
      tipExtra: function (ci) {
        var r = rows[ci];
        return "<div class='muted'>l3_id " + r.l3 + " · chase " + fmt(r.chase) + " ns · " +
          fmt(r.ghz, 2) + " GHz" + (r.note ? " · " + r.note : "") + "</div>";
      }
    });
  }
  var l = document.getElementById("chart-llc");
  if (l && D.llc.length) {
    hbar(l, {
      aria: "Victim latency per copy method",
      cats: D.llc.map(function (r) { return r.method; }),
      series: [
        {name: "p50", color: "var(--s1)", values: D.llc.map(function (r) { return r.p50; })},
        {name: "p99", color: "var(--s2)", values: D.llc.map(function (r) { return r.p99; })},
        {name: "max", color: "var(--s3)", values: D.llc.map(function (r) { return r.max; })}
      ],
      unit: "ns",
      tipExtra: function (ci) { return "<div class='muted'>copier " + fmt(D.llc[ci].gbs, 2) + " GB/s</div>"; }
    });
  }
  var g = document.getElementById("chart-gbs");
  if (g && D.llc.length) {
    var cp = D.llc.filter(function (r) { return r.method !== "idle"; });
    hbar(g, {
      aria: "Copier throughput per method",
      cats: cp.map(function (r) { return r.method; }),
      series: [{name: "copier", color: "var(--s1)", values: cp.map(function (r) { return r.gbs; }), unit: "GB/s", digits: 2}],
      unit: "GB/s"
    });
  }
  var w = document.getElementById("chart-w2a");
  if (w && D.w2a.length) {
    var wr = D.w2a.filter(function (r) { return r.p50 != null; });
    hbar(w, {
      aria: "Wire-to-app latency per core",
      cats: wr.map(function (r) { return "cpu " + r.cpu; }),
      series: [
        {name: "p50", color: "var(--s1)", values: wr.map(function (r) { return r.p50; }), digits: 0},
        {name: "p99", color: "var(--s2)", values: wr.map(function (r) { return r.p99; }), digits: 0},
        {name: "p99.9", color: "var(--s3)", values: wr.map(function (r) { return r.p999; }), digits: 0}
      ],
      unit: "ns",
      tipExtra: function (ci) {
        var r = wr[ci];
        return "<div class='muted'>" + r.pkts + " pkts · wire→kernel " + fmt(r.w2k, 0) +
          " ns · kernel→app " + fmt(r.k2a, 0) + " ns</div>";
      }
    });
  }
  var h = document.getElementById("chart-history");
  if (h && D.runs && D.runs.length) {
    var hr = D.runs.filter(function (r) { return r.c2c != null; });
    hbar(h, {
      aria: "Hot core c2c latency per run",
      cats: hr.map(function (r) { return r.name; }),
      series: [{name: "hot-core c2c", color: "var(--s1)", values: hr.map(function (r) { return r.c2c; })}],
      unit: "ns",
      tipTitle: function (ci) { return hr[ci].name; },
      tipExtra: function (ci) { return "<div class='muted'>hot core " + hr[ci].best + " · " + fmt(hr[ci].ghz, 2) + " GHz</div>"; }
    });
  }
})();
</script>
</body>
</html>
"""


def esc(s):
    return html.escape(str(s if s is not None else "–"))


def f1(v, d=1):
    return "–" if v is None else f"{v:,.{d}f}"


def table(headers, rows, hl=None):
    out = ["<div class='tablewrap'><table><thead><tr>"]
    out += [f"<th>{esc(h)}</th>" for h in headers]
    out.append("</tr></thead><tbody>")
    for i, r in enumerate(rows):
        cls = " class='hl'" if hl is not None and hl(i) else ""
        out.append(f"<tr{cls}>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def tile(label, value, delta=""):
    return (f"<div class='tile'><div class='label'>{esc(label)}</div>"
            f"<div class='value'>{value}</div><div class='delta'>{delta}</div></div>")


def render_run(run):
    env, meta, s = run["env"], run["meta"], summary(run)
    host = meta.get("HOST", run["name"])
    title = f"Tuning report {host}"
    b = [f"<header><h1>{esc(title)}</h1>"
         f"<button class='theme' id='theme' type='button'>Toggle theme</button></header>",
         f"<p class='sub'>{esc(meta.get('TIME', run['name']))} · {esc(meta.get('INSTANCE_TYPE', ''))} "
         f"{esc(meta.get('CPU_MODEL', ''))} · kernel {esc(meta.get('KERNEL', '?'))} · "
         f"<a href='../index.html'>all runs</a></p>"]

    tiles = []
    if s.get("best"):
        tiles.append(tile("Hot core", f"cpu {esc(s['best'])}",
                          f"poller cpu {esc(env.get('POLLER'))}"))
    if s.get("c2c") is not None:
        tiles.append(tile("Core-to-core from poller", f"{f1(s['c2c'])} ns",
                          f"pointer chase {f1(s.get('chase'))} ns"))
    if s.get("ghz_before") is not None:
        d = ""
        if s.get("ghz_after") is not None:
            diff = s["ghz_after"] - s["ghz_before"]
            cls = "up" if diff > 0.005 else "down" if diff < -0.005 else ""
            d = f"<span class='{cls}'>{diff:+.2f} GHz</span> after offlining"
        tiles.append(tile("Hot core clock", f"{f1(s.get('ghz_after', s['ghz_before']), 2)} GHz",
                          d or "before offlining"))
    if s.get("llc_worst"):
        wv = s["llc_worst"]
        tiles.append(tile("Victim p99, idle copier", f"{f1(s['llc_idle_p99'])} ns",
                          f"worst: {esc(wv['method'])} {f1(wv['p99'])} ns"))
    if s.get("w2a_best"):
        r = s["w2a_best"]
        tiles.append(tile("Wire to app p50", f"{f1(r['p50'], 0)} ns",
                          f"cpu {r['cpu']} · p99 {f1(r['p99'], 0)} ns"))
    if tiles:
        b.append("<div class='tiles'>" + "".join(tiles) + "</div>")

    if env:
        roles = [("Isolated at boot", env.get("ISOLATED")), ("Housekeeping", env.get("HOUSEKEEPING")),
                 ("Poller", env.get("POLLER")), ("Hot candidates", env.get("HOT")),
                 ("Hot core chosen", env.get("BEST")), ("Logger / copier", env.get("LOGGER") or "none"),
                 ("HT siblings (offlined)", env.get("SIBLINGS") or "none"), ("Interface", env.get("IFACE"))]
        b.append("<section class='card'><h2>Core layout</h2>"
                 "<p class='sub'>Derived from the cores isolated on the GRUB command line.</p><dl class='roles'>" +
                 "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in roles) + "</dl></section>")

    if run["corelat"]:
        rows = run["corelat"]
        b.append("<section class='card'><h2>Core-to-core latency from the poller</h2>"
                 "<p class='sub'>Lower is better. One cache line bounced between the poller and each "
                 "candidate core. The top row became the hot core.</p>"
                 "<div class='legend'><span><i style='background:var(--s1)'></i>Shares the poller's L3</span>"
                 "<span><i style='background:var(--other)'></i>Other L3</span></div>"
                 "<div class='chart' id='chart-corelat'></div>"
                 "<details><summary>Table view</summary>" +
                 table(["cpu", "l3_id", "c2c ns", "chase ns", "clock GHz", "note"],
                       [[r["cpu"], r["l3"], f1(r["c2c"]), f1(r["chase"]), f1(r["ghz"], 2), r["note"]] for r in rows],
                       hl=lambda i: str(rows[i]["cpu"]) == s.get("best")) +
                 f"<p class='muted'>{esc(run['corelat_footer'])}</p></details></section>")

    if run["llc"]:
        b.append("<section class='card'><h2>Does the logger evict the hot core?</h2>"
                 f"<p class='sub'>Hot core's batch latency while the copier on cpu {esc(env.get('LOGGER'))} "
                 "runs each copy method. Compare every row with <code>idle</code>.</p>"
                 "<div class='legend'><span><i style='background:var(--s1)'></i>p50</span>"
                 "<span><i style='background:var(--s2)'></i>p99</span>"
                 "<span><i style='background:var(--s3)'></i>max</span></div>"
                 "<div class='chart' id='chart-llc'></div>"
                 "<h2 style='margin-top:20px'>Copier throughput</h2>"
                 "<p class='sub'>What each method costs the logger.</p>"
                 "<div class='chart' id='chart-gbs'></div>"
                 "<details><summary>Table view</summary>" +
                 table(["method", "victim p50 ns", "victim p99 ns", "victim max ns", "copier GB/s"],
                       [[r["method"], f1(r["p50"]), f1(r["p99"]), f1(r["max"]), f1(r["gbs"], 2)] for r in run["llc"]]) +
                 f"<p class='muted'>{esc(run['llc_footer'])}</p></details></section>")

    if run["after"]:
        b.append("<section class='card'><h2>After offlining spare cores</h2>"
                 "<p class='sub'>The hot core re-measured once everything outside the layout was offlined.</p>" +
                 table(["cpu", "c2c ns", "chase ns", "clock GHz"],
                       [[r["cpu"], f1(r["c2c"]), f1(r["chase"]), f1(r["ghz"], 2)] for r in run["after"]]) +
                 f"<p class='muted'>{esc(run['after_footer'])}</p></section>")

    if run["w2a"]:
        b.append("<section class='card'><h2>Wire to app latency</h2>"
                 "<p class='sub'>NIC hardware RX timestamp to the app, per core. Lowest p50 with a tight "
                 "p99 wins; this overrides the core-to-core ranking.</p>"
                 "<div class='legend'><span><i style='background:var(--s1)'></i>p50</span>"
                 "<span><i style='background:var(--s2)'></i>p99</span>"
                 "<span><i style='background:var(--s3)'></i>p99.9</span></div>"
                 "<div class='chart' id='chart-w2a'></div>"
                 "<details><summary>Table view</summary>" +
                 table(["cpu", "pkts", "p50 ns", "p99 ns", "p99.9 ns", "max ns", "wire→kernel p50", "kernel→app p50"],
                       [[r["cpu"], r["pkts"], f1(r["p50"], 0), f1(r["p99"], 0), f1(r["p999"], 0),
                         f1(r["max"], 0), f1(r["w2k"], 0), f1(r["k2a"], 0)] for r in run["w2a"]]) +
                 f"<p class='muted'>{esc(run['w2a_footer'])}</p></details></section>")
    else:
        b.append("<section class='card'><h2>Wire to app latency</h2><p class='sub'>Not captured. With traffic "
                 "arriving, run <code>sudo scripts/tune.sh --wire2app PORT</code>, or save "
                 "<code>wire2app</code> output as <code>wire2app.txt</code> in this run's folder and rerun "
                 "<code>scripts/report.py</code>.</p></section>")

    data = {k: run[k] for k in ("env", "corelat", "llc", "w2a", "poller_l3")}
    return (PAGE.replace("__TITLE__", esc(title)).replace("__BODY__", "\n".join(b))
            .replace("__DATA__", json.dumps(data).replace("</", "<\\/")))


def render_index(runs):
    rows, data = [], []
    for run in runs:
        s, m = summary(run), run["meta"]
        rows.append(f"<tr><td><a href='{esc(run['name'])}/report.html'>{esc(run['name'])}</a></td>"
                    f"<td>{esc(m.get('INSTANCE_TYPE', ''))}</td><td>{esc(s.get('best'))}</td>"
                    f"<td>{f1(s.get('c2c'))}</td><td>{f1(s.get('ghz_after', s.get('ghz_before')), 2)}</td>"
                    f"<td>{f1(s.get('llc_idle_p99'))}</td>"
                    f"<td>{f1(s['w2a_best']['p50'], 0) if s.get('w2a_best') else '–'}</td></tr>")
        data.append({"name": run["name"], "best": s.get("best"), "c2c": s.get("c2c"),
                     "ghz": s.get("ghz_after", s.get("ghz_before"))})
    body = ("<header><h1>Tuning runs</h1><button class='theme' id='theme' type='button'>Toggle theme</button></header>"
            f"<p class='sub'>{len(runs)} run(s), newest first. Open a run for its charts.</p>"
            "<section class='card'><h2>Hot-core latency from the poller, per run</h2>"
            "<p class='sub'>Lower is better.</p><div class='chart' id='chart-history'></div></section>"
            "<section class='card'><div class='tablewrap'><table><thead><tr><th>run</th><th>instance</th>"
            "<th>hot core</th><th>c2c ns</th><th>clock GHz</th><th>victim p99 idle ns</th>"
            "<th>wire→app p50 ns</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>")
    data = {"env": {}, "corelat": [], "llc": [], "w2a": [], "poller_l3": None, "runs": data}
    return (PAGE.replace("__TITLE__", "Tuning runs").replace("__BODY__", body)
            .replace("__DATA__", json.dumps(data).replace("</", "<\\/")))


def build(dirs):
    runs = []
    for d in dirs:
        run = load_run(d)
        out = os.path.join(d, "report.html")
        with open(out, "w") as f:
            f.write(render_run(run))
        print(f"wrote {out}")
        runs.append(run)
    root = os.path.normpath(ROOT)
    every = sorted((os.path.join(root, n) for n in os.listdir(root)
                    if os.path.isdir(os.path.join(root, n)) and not os.path.islink(os.path.join(root, n))),
                   reverse=True) if os.path.isdir(root) else []
    if every:
        with open(os.path.join(root, "index.html"), "w") as f:
            f.write(render_index([load_run(d) for d in every]))
        print(f"wrote {os.path.join(root, 'index.html')}")


def main(argv):
    serve = None
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv and argv[0] == "--serve":
        serve = int(argv[1]) if len(argv) > 1 else 8080
        argv = []
    dirs = argv
    if not dirs:
        root = os.path.normpath(ROOT)
        if not os.path.isdir(root):
            print(f"no results yet in {root}: run sudo scripts/tune.sh first", file=sys.stderr)
            return 2
        dirs = [os.path.join(root, n) for n in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, n)) and not os.path.islink(os.path.join(root, n))]
    build(dirs)
    if serve:
        handler = partial(http.server.SimpleHTTPRequestHandler, directory=os.path.normpath(ROOT))
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("127.0.0.1", serve), handler) as httpd:
            print(f"serving {os.path.normpath(ROOT)} on http://127.0.0.1:{serve}/  (Ctrl-C to stop)")
            print(f"from your laptop: ssh -L {serve}:127.0.0.1:{serve} <user>@<host>, "
                  f"then open http://localhost:{serve}/")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
