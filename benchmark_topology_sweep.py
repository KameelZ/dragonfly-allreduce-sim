#!/usr/bin/env python3
"""
benchmark_topology_sweep.py

A sweep across five general Dragonfly / Dragonfly+ configurations, spanning
the literal single-global-link edge case up through more typical multi-group,
multi-link setups:

  1. two_groups_1link   -- 2 groups, 1 global link/pair (the network's ONLY
                            global link -- the pure single-link edge case)
  2. two_groups_2link    -- same 2 groups, but 2 parallel global links/pair,
                            showing how extra link capacity relieves it
  3. three_groups_1link  -- 3 groups, 1 link/pair (a typical small Dragonfly)
  4. four_groups_2link   -- 4 groups, 2 links/pair (a general, richer setup)
  5. five_groups_1link   -- 5 groups, 1 link/pair (more groups, sparse links)

By default every pattern from benchmark_gpaard.py runs against each config
(gpaard, uniform, all_to_all, hotspot, shift, nearest_neighbor) -- not just
'shift' -- so each result file shows the real workload, the evenly-spread
control patterns, the endpoint-bottleneck case (hotspot), and both
fabric-targeted adversarial patterns (shift / nearest_neighbor) side by
side. The 'shift' pattern is the one that most directly exercises the
single-link idea: it sends every host in group g to the matching host in
group g+1, so each group's entire egress is forced onto its one link to the
next group -- in configs 1-2 that IS the whole network's traffic; in configs
3-5 it's a realistic worst-case slice of a bigger, more general topology.

For every configuration this writes its own result file with:
  - a compact summary + plotted picture of the exact Dragonfly and
    Dragonfly+ topology under test (reusing visualize_dragonfly_topo.py's
    inspection helpers),
  - one section per pattern, each with its own "why test this" explanation
    (same text as benchmark_gpaard.py's per-pattern report sections),
  - every repeat iteration's time printed individually,
  - the average time per topology,
  - the Dragonfly-vs-Dragonfly+ speedup computed from those averages only
    (never from individual iterations).

Requires root (Mininet manipulates network namespaces):

    sudo python3 benchmark_topology_sweep.py
    sudo python3 benchmark_topology_sweep.py --sizes 4K,64K,1M,8M --repeat 3
    sudo python3 benchmark_topology_sweep.py --patterns shift,nearest_neighbor --repeat 5
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

from dragonfly_topo import build_network, install_gpaard_routing
from benchmark_gpaard import (
    PATTERNS, PATTERN_INFO, PATTERN_REPORT_ORDER,
    PortAllocator, run_pattern, format_size, parse_size,
)
from visualize_dragonfly_topo import build_topology_view, classify_link, plot_topology


def _wrap(text, width=78, indent="  "):
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


# ---------------------------------------------------------------------------
# Five general configurations, from the pure single-link edge case up to
# more typical multi-group/multi-link setups (see module docstring). Kept
# deliberately small (2 routers/leaves/spines, 1 host each) across the board
# so the whole sweep stays quick to run -- only groups and global_links vary.
# ---------------------------------------------------------------------------
CONFIGS = [
    {"name": "two_groups_1link", "groups": 2, "routers": 2, "leaves": 2, "spines": 2, "hosts": 1, "global_links": 1},
    {"name": "two_groups_2link", "groups": 2, "routers": 2, "leaves": 2, "spines": 2, "hosts": 1, "global_links": 2},
    {"name": "three_groups_1link", "groups": 3, "routers": 2, "leaves": 2, "spines": 2, "hosts": 1, "global_links": 1},
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
    """One-line explanation of what makes this config's global link(s)
    interesting, for the header of its result file."""
    if cfg["groups"] == 2 and cfg["global_links"] == 1:
        return "exactly one global link exists in the WHOLE network -- pure single-link edge case"
    return (f"{cfg['groups']} groups, {cfg['global_links']} global link(s) per group pair -- "
            f"shift still forces each group's entire egress onto its one link to the next "
            f"group, but other groups/links exist elsewhere in the network")


def summarize_topology_brief(topo, view):
    """One-paragraph topology summary (label + node/link counts) -- no
    per-switch/per-host/per-link listing, since the full plot (see
    save_topology_plot) already shows the structure visually and the full
    text listing just bloats the result file for larger configs."""
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


def run_topology(topology, cfg, args, lines, timings):
    """Build one topology, print/plot it, run every (pattern, size) combo
    --repeat times, and record each iteration's time into `timings`."""
    info(f"\n*** [topology-sweep] building topology={topology} config={cfg['name']}\n")
    cleanup()
    net_args = build_topology_args(topology, cfg, args.bw, args.delay)
    net = build_network(net_args)
    topo = net.topo

    view = build_topology_view(topo)
    label = f"{cfg['name']}_{topology.replace('+', 'plus')}"
    plot_path = save_topology_plot(topo, view, args.plots_dir, label)

    lines.append("-" * 70)
    lines.append(f"TOPOLOGY UNDER TEST: {topology}")
    lines.append("-" * 70)
    lines.append(summarize_topology_brief(topo, view))
    lines.append(f"[full topology plot saved to {plot_path}]")
    lines.append("")

    net.start()
    try:
        net.staticArp()
        install_gpaard_routing(net)
        time.sleep(args.settle)
        loss = net.pingAll()
        if loss != 0.0:
            raise RuntimeError(
                f"{topology}/{cfg['name']}: pingAll reported {loss:.1f}% packet loss -- "
                "routing is broken, aborting before running iperf")

        port_allocator = PortAllocator()
        for pattern_name in args.patterns:
            steps = PATTERNS[pattern_name](topo)
            for size_bytes in args.sizes:
                iter_times = []
                for i in range(args.repeat):
                    total_time, _step_times = run_pattern(
                        net, steps, size_bytes, port_allocator, args.timeout)
                    iter_times.append(total_time)
                    info(f"*** [topology-sweep] {cfg['name']:7s} {topology:11s} {pattern_name:8s} "
                         f"size={format_size(size_bytes):>6s} iter={i + 1}/{args.repeat} -> {total_time:.3f}s\n")
                timings[(topology, pattern_name, size_bytes)] = iter_times
    finally:
        net.stop()


def run_config(cfg, args):
    lines = []
    lines.append(f"Adversarial shift-pattern benchmark -- config: {cfg['name']}")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={cfg['groups']} global_links_per_pair={cfg['global_links']} "
                 f"({_topology_description(cfg)})")
    lines.append(f"routers={cfg['routers']} leaves={cfg['leaves']} "
                 f"spines={cfg['spines']} hosts={cfg['hosts']} bw={args.bw}Mbit delay={args.delay}")
    lines.append(f"patterns: {', '.join(args.patterns)}")
    lines.append(f"message sizes: {', '.join(format_size(s) for s in args.sizes)}")
    lines.append(f"repeat: {args.repeat}")
    lines.append("")

    timings = {}
    for topology in ("dragonfly", "dragonfly+"):
        run_topology(topology, cfg, args, lines, timings)

    lines.append("=" * 70)
    lines.append("RESULTS (every iteration, then the average, then speedup from the averages)")
    lines.append("=" * 70)
    ordered_patterns = [p for p in PATTERN_REPORT_ORDER if p in args.patterns]
    ordered_patterns += [p for p in args.patterns if p not in ordered_patterns]
    for pattern_name in ordered_patterns:
        lines.append("-" * 70)
        lines.append(f"PATTERN: {pattern_name}")
        lines.append("-" * 70)
        blurb = PATTERN_INFO.get(pattern_name)
        if blurb:
            lines.append(_wrap(blurb))
            lines.append("")
        for size_bytes in args.sizes:
            avgs = {}
            for topology in ("dragonfly", "dragonfly+"):
                iters = timings[(topology, pattern_name, size_bytes)]
                iter_str = ", ".join(f"iter{i + 1}={t:.3f}s" for i, t in enumerate(iters))
                avg = sum(iters) / len(iters)
                avgs[topology] = avg
                lines.append(f"  {topology:11s} {format_size(size_bytes):>6s}: [{iter_str}]  avg={avg:.3f}s")
            df_avg, dfp_avg = avgs["dragonfly"], avgs["dragonfly+"]
            if dfp_avg > 0:
                lines.append(f"  speedup ({format_size(size_bytes)}, avg dragonfly / avg dragonfly+): "
                             f"{df_avg / dfp_avg:.3f}")
            lines.append("")

    out_path = f"{args.out_prefix}_{cfg['name']}.txt"
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [topology-sweep] results written to {out_path}\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Dragonfly vs Dragonfly+ benchmark across five general configurations "
                     "(from a pure single-global-link edge case up to general multi-group, "
                     "multi-link setups), covering gpaard/uniform/all_to_all/hotspot plus "
                     "the shift/nearest_neighbor adversarial patterns.")
    parser.add_argument("--patterns", type=str,
                        default="gpaard,uniform,all_to_all,hotspot,shift,nearest_neighbor",
                        help="comma-separated patterns to run: "
                             f"{', '.join(sorted(PATTERNS))} (default: all of them)")
    parser.add_argument("--sizes", type=str, default="4K,64K,1M,8M",
                        help="comma-separated message sizes, e.g. '4K,64K,1M,8M' (default: 4K,64K,1M,8M)")
    parser.add_argument("--bw", type=float, default=10, help="link bandwidth in Mbit/s (default: 10)")
    parser.add_argument("--delay", type=str, default="1ms", help="per-link delay (default: 1ms)")
    parser.add_argument("--settle", type=int, default=1, help="seconds to wait after installing flows (default: 1)")
    parser.add_argument("--repeat", type=int, default=3, help="iterations per (pattern, size) (default: 3)")
    parser.add_argument("--timeout", type=int, default=60, help="per-message iperf timeout in seconds (default: 60)")
    parser.add_argument("--out-prefix", type=str, default="results_adversarial",
                        help="result files are written to <prefix>_<config>.txt (default: results_adversarial)")
    parser.add_argument("--plots-dir", type=str, default="adversarial_topology_plots",
                        help="directory for the topology plot images (default: adversarial_topology_plots)")
    args = parser.parse_args(argv)

    args.patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]
    for p in args.patterns:
        if p not in PATTERNS:
            parser.error(f"unknown pattern '{p}', choose from: {', '.join(sorted(PATTERNS))}")
    args.sizes = [parse_size(s) for s in args.sizes.split(",") if s.strip()]
    return args


def main():
    args = parse_args()  # handles --help before the root check below

    if os.geteuid() != 0:
        print("benchmark_topology_sweep.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    setLogLevel("info")

    for cfg in CONFIGS:
        run_config(cfg, args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
