#!/usr/bin/env python3
"""
benchmark_bandwidth_sweep.py

Dragonfly vs Dragonfly+ benchmark for the g-PAARD all-reduce, sweeping
BANDWIDTH as a first-class axis (alongside message size) so the link-rate's
effect on the two topologies is visible directly in the results.

Why this file exists (what we learned analysing the earlier sweeps):
  - For the g-PAARD workload, `global_links_per_pair` is effectively inert:
    g-PAARD designates exactly one global link per group pair and the
    baseline routing is single-next-hop (no ECMP), so parallel global links
    are never used. It is kept as a per-config knob only so you can confirm
    this for yourself, not because it is expected to move the numbers.
  - Small messages measure per-step overhead (server bind + TCP setup), not
    the network. The topology difference between DF and DF+ only shows through
    in the "large message / low bandwidth" corner -- which is exactly why
    bandwidth is swept here rather than pinned at a single value.
  - DF and DF+ are structurally identical up to 3 groups and only diverge at
    >=4 groups, in the inter-group global-exchange steps. So the interesting
    configs are the larger group counts.

Only the g-PAARD pattern is run (this is the workload the project targets).
Everything else the topology-sweep did is kept: it prints/plots the exact
topology under test, honours --repeat, and honours --sizes.

For every configuration this writes its own result file with:
  - a compact summary + plotted picture of the Dragonfly and Dragonfly+
    topology under test,
  - results grouped by BANDWIDTH, then by message size,
  - every repeat iteration's time printed individually,
  - the average time per topology, and the DF-vs-DF+ speedup computed from
    those averages only (never from individual iterations).

Requires root (Mininet manipulates network namespaces):

    sudo python3 benchmark_bandwidth_sweep.py
    sudo python3 benchmark_bandwidth_sweep.py --bandwidths 10,100,1000 --sizes 1M,8M --repeat 5
    sudo python3 benchmark_bandwidth_sweep.py --bandwidths 10,50 --sizes 8M --repeat 10
"""

from mininet.clean import cleanup
from mininet.log import setLogLevel, info

import argparse
import contextlib
import io
import os
import sys
import textwrap
import time
from argparse import Namespace

import matplotlib
matplotlib.use("Agg")  # headless: never try to open a GUI window under sudo

# This file lives in experiments/; put the project root on the path so the
# top-level modules (dragonfly_topo, benchmark_gpaard, ...) import cleanly
# whether run as `python3 experiments/benchmark_bandwidth_sweep.py` or from here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dragonfly_topo import build_network, install_gpaard_routing
from benchmark_gpaard import (
    PATTERNS, PATTERN_INFO,
    PortAllocator, run_pattern, format_size, parse_size,
)
from visualize_dragonfly_topo import build_topology_view, classify_link, plot_topology

# This whole file is about one workload: g-PAARD's own all-reduce schedule.
PATTERN = "gpaard"


def _wrap(text, width=78, indent="  "):
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


# ---------------------------------------------------------------------------
# Configurations. Only groups / global_links vary (bandwidth and size are
# swept separately, per-run). Kept deliberately small (width=2, 1 host each)
# so the sweep stays quick; the group count is what makes DF and DF+ diverge.
# ---------------------------------------------------------------------------
CONFIGS = [
    {"name": "two_groups_1link", "groups": 2, "routers": 2, "leaves": 2, "spines": 2, "hosts": 1, "global_links": 1},
    {"name": "three_groups_1link", "groups": 3, "routers": 2, "leaves": 2, "spines": 2, "hosts": 1, "global_links": 1},
    {"name": "four_groups_1link", "groups": 4, "routers": 2, "leaves": 2, "spines": 2, "hosts": 1, "global_links": 1},
    {"name": "four_groups_2link", "groups": 4, "routers": 2, "leaves": 2, "spines": 2, "hosts": 1, "global_links": 2},
    {"name": "five_groups_1link", "groups": 5, "routers": 2, "leaves": 2, "spines": 2, "hosts": 1, "global_links": 1},
]


def build_topology_args(topology, cfg, bw, delay):
    return Namespace(
        topology=topology, groups=cfg["groups"], routers=cfg["routers"],
        leaves=cfg["leaves"], spines=cfg["spines"], hosts=cfg["hosts"],
        bw=bw, delay=delay, global_links=cfg["global_links"],
    )


def _topology_description(cfg):
    """One-line explanation of what makes this config interesting."""
    if cfg["groups"] <= 3:
        return (f"{cfg['groups']} groups -- DF and DF+ are structurally identical here "
                f"for g-PAARD (baseline / sanity config)")
    return (f"{cfg['groups']} groups, {cfg['global_links']} global link(s) per pair -- "
            f"DF and DF+ diverge at >=4 groups in the global-exchange steps; "
            f"extra global links do not help g-PAARD (single designated link + no ECMP)")


def summarize_topology_brief(topo, view):
    """One-paragraph topology summary (label + node/link counts). The full
    plot (see save_topology_plot) shows the structure visually."""
    kind_counts = {"host": 0, "local": 0, "global": 0}
    for n1, n2 in view["links"]:
        kind_counts[classify_link(n1, n2, view["host_set"], view["switch_group"])] += 1
    return (
        f"{topo.topology_label()}\n"
        f"switches={len(view['switches'])} hosts={len(view['hosts'])} "
        f"links={len(view['links'])} "
        f"(host={kind_counts['host']}, local={kind_counts['local']}, global={kind_counts['global']})"
    )


def save_topology_plot(topo, view, out_dir, label):
    os.makedirs(out_dir, exist_ok=True)
    filename = os.path.join(out_dir, f"{label}.png")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        plot_topology(topo, view, filename=filename)
    return filename


def plot_config_topologies(cfg, args, lines):
    """Build each topology once (bandwidth-independent structure) purely to
    print + plot it, before the timed runs. Returns nothing; appends to lines."""
    for topology in ("dragonfly", "dragonfly+"):
        cleanup()
        # bandwidth is irrelevant to the drawn structure; use the first one.
        net_args = build_topology_args(topology, cfg, args.bandwidths[0], args.delay)
        topo = build_network(net_args).topo
        view = build_topology_view(topo)
        label = f"{cfg['name']}_{topology.replace('+', 'plus')}"
        plot_path = save_topology_plot(topo, view, args.plots_dir, label)

        lines.append("-" * 70)
        lines.append(f"TOPOLOGY UNDER TEST: {topology}")
        lines.append("-" * 70)
        lines.append(summarize_topology_brief(topo, view))
        lines.append(f"[full topology plot saved to {plot_path}]")
        lines.append("")


def run_topology_at_bw(topology, cfg, bw, args, timings):
    """Build one topology at bandwidth `bw`, run the g-PAARD pattern for every
    size --repeat times, and record each iteration's time into `timings`
    keyed by (topology, bw, size)."""
    info(f"\n*** [bw-sweep] building topology={topology} config={cfg['name']} bw={bw}Mbit\n")
    cleanup()
    net_args = build_topology_args(topology, cfg, bw, args.delay)
    net = build_network(net_args)
    topo = net.topo

    net.start()
    try:
        net.staticArp()
        install_gpaard_routing(net)
        time.sleep(args.settle)
        loss = net.pingAll()
        if loss != 0.0:
            raise RuntimeError(
                f"{topology}/{cfg['name']}/bw={bw}: pingAll reported {loss:.1f}% packet "
                "loss -- routing is broken, aborting before running iperf")

        steps = PATTERNS[PATTERN](topo)
        port_allocator = PortAllocator()
        for size_bytes in args.sizes:
            iter_times = []
            for i in range(args.repeat):
                total_time, _step_times = run_pattern(
                    net, steps, size_bytes, port_allocator, args.timeout)
                iter_times.append(total_time)
                info(f"*** [bw-sweep] {cfg['name']:7s} {topology:11s} bw={bw:>5g} "
                     f"size={format_size(size_bytes):>6s} iter={i + 1}/{args.repeat} "
                     f"-> {total_time:.3f}s\n")
            timings[(topology, bw, size_bytes)] = iter_times
    finally:
        net.stop()


def run_config(cfg, args):
    lines = []
    lines.append(f"g-PAARD bandwidth sweep -- config: {cfg['name']}")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={cfg['groups']} global_links_per_pair={cfg['global_links']} "
                 f"({_topology_description(cfg)})")
    lines.append(f"routers={cfg['routers']} leaves={cfg['leaves']} "
                 f"spines={cfg['spines']} hosts={cfg['hosts']} delay={args.delay}")
    lines.append(f"pattern: {PATTERN} (only)")
    lines.append(f"bandwidths (Mbit/s): {', '.join('%g' % b for b in args.bandwidths)}")
    lines.append(f"message sizes: {', '.join(format_size(s) for s in args.sizes)}")
    lines.append(f"repeat: {args.repeat}")
    lines.append("")

    # 1. Draw + describe the topologies once (structure is bandwidth-independent).
    plot_config_topologies(cfg, args, lines)

    # 2. Timed runs: outer loop over topology, inner over bandwidth, then size.
    timings = {}
    for topology in ("dragonfly", "dragonfly+"):
        for bw in args.bandwidths:
            run_topology_at_bw(topology, cfg, bw, args, timings)

    # 3. Results, grouped by BANDWIDTH so its effect is the primary axis.
    lines.append("=" * 70)
    lines.append("RESULTS: g-PAARD, grouped by BANDWIDTH then message size")
    lines.append("(every iteration, then the average, then speedup from the averages)")
    lines.append("=" * 70)
    blurb = PATTERN_INFO.get(PATTERN)
    if blurb:
        lines.append(_wrap(blurb))
        lines.append("")

    for bw in args.bandwidths:
        lines.append("-" * 70)
        lines.append(f"BANDWIDTH: {bw:g} Mbit/s")
        lines.append("-" * 70)
        for size_bytes in args.sizes:
            avgs = {}
            for topology in ("dragonfly", "dragonfly+"):
                iters = timings[(topology, bw, size_bytes)]
                iter_str = ", ".join(f"iter{i + 1}={t:.3f}s" for i, t in enumerate(iters))
                avg = sum(iters) / len(iters)
                avgs[topology] = avg
                lines.append(f"  {topology:11s} {format_size(size_bytes):>6s}: "
                             f"[{iter_str}]  avg={avg:.3f}s")
            df_avg, dfp_avg = avgs["dragonfly"], avgs["dragonfly+"]
            if dfp_avg > 0:
                lines.append(f"  speedup ({format_size(size_bytes)} @ {bw:g}Mbit, "
                             f"avg dragonfly / avg dragonfly+): {df_avg / dfp_avg:.3f}")
            lines.append("")

    # 4. Compact bandwidth x size speedup matrix, for a quick at-a-glance read.
    lines.append("=" * 70)
    lines.append("SPEEDUP MATRIX (avg dragonfly / avg dragonfly+)  [>1 = DF+ faster]")
    lines.append("=" * 70)
    header = "  bw\\size  " + "".join(f"{format_size(s):>10s}" for s in args.sizes)
    lines.append(header)
    for bw in args.bandwidths:
        row = f"  {bw:>7g}  "
        for size_bytes in args.sizes:
            df_avg = sum(timings[("dragonfly", bw, size_bytes)]) / args.repeat
            dfp_avg = sum(timings[("dragonfly+", bw, size_bytes)]) / args.repeat
            ratio = df_avg / dfp_avg if dfp_avg > 0 else float("nan")
            row += f"{ratio:>10.3f}"
        lines.append(row)
    lines.append("")

    out_path = f"{args.out_prefix}_{cfg['name']}.txt"
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [bw-sweep] results written to {out_path}\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Dragonfly vs Dragonfly+ g-PAARD benchmark sweeping bandwidth "
                    "(and message size), keeping the topology plot, --repeat and "
                    "--sizes from the topology sweep. Only the g-PAARD pattern runs.")
    parser.add_argument("--bandwidths", type=str, default="10,100,1000",
                        help="comma-separated link bandwidths in Mbit/s, e.g. '10,100,1000' "
                             "(default: 10,100,1000)")
    parser.add_argument("--sizes", type=str, default="64K,1M,8M",
                        help="comma-separated message sizes, e.g. '64K,1M,8M' (default: 64K,1M,8M)")
    parser.add_argument("--delay", type=str, default="1ms", help="per-link delay (default: 1ms)")
    parser.add_argument("--settle", type=int, default=1, help="seconds to wait after installing flows (default: 1)")
    parser.add_argument("--repeat", type=int, default=3, help="iterations per (bandwidth, size) (default: 3)")
    parser.add_argument("--timeout", type=int, default=60, help="per-message iperf timeout in seconds (default: 60)")
    parser.add_argument("--out-prefix", type=str, default="results_bw_sweep",
                        help="result files are written to <prefix>_<config>.txt (default: results_bw_sweep)")
    parser.add_argument("--plots-dir", type=str, default="bandwidth_topology_plots",
                        help="directory for the topology plot images (default: bandwidth_topology_plots)")
    args = parser.parse_args(argv)

    args.bandwidths = [float(b) for b in args.bandwidths.split(",") if b.strip()]
    if not args.bandwidths:
        parser.error("need at least one bandwidth in --bandwidths")
    args.sizes = [parse_size(s) for s in args.sizes.split(",") if s.strip()]
    if not args.sizes:
        parser.error("need at least one size in --sizes")
    return args


def main():
    args = parse_args()  # handles --help before the root check below

    if os.geteuid() != 0:
        print("benchmark_bandwidth_sweep.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    setLogLevel("info")

    for cfg in CONFIGS:
        run_config(cfg, args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
