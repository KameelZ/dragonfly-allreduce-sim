#!/usr/bin/env python3
"""
sweep_big_topology.py

Single-axis sweeps of the g-PAARD all-reduce on ONE fixed "big" base topology,
comparing Dragonfly vs Dragonfly+. Three sweeps, each perturbing exactly one
dimension of the SAME base config so the effect of that dimension is isolated:

  groups        (test 1) -- how DF and DF+ scale as the number of groups grows.
                            This is the axis where the two topologies actually
                            diverge (they are identical up to 3 groups).
  global_links  (test 2) -- whether extra global links per group pair help
                            g-PAARD. Swept exactly like groups. Expected result:
                            a FLAT line -- g-PAARD designates one link per pair
                            and the baseline routing is single-next-hop (no
                            ECMP), so parallel global links are never used. The
                            flat curve is the point: it motivates the routing
                            optimisation, not a tuning knob.
  hosts         (test 5) -- intra-group any-to-any cost. Steps 3/4 are a full
                            any-to-any exchange within each group (~O(hosts^2)),
                            so this stresses DF+'s 2-hop leaf->spine->leaf fabric
                            against DF's flat 1-hop mesh.

Only the g-PAARD pattern runs. Kept from the earlier sweeps: it prints/plots the
exact topology under test, honours --repeat and --sizes, and computes the
DF-vs-DF+ speedup from the per-topology averages only (never per iteration).

Reuses existing code rather than reimplementing it:
  - topology build + routing            -> dragonfly_topo
  - g-PAARD schedule + iperf runner      -> benchmark_gpaard
  - topology view for plotting           -> visualize_dragonfly_topo
  - topology summary + plot save helpers -> benchmark_bandwidth_sweep

Requires root (Mininet manipulates network namespaces):

    sudo python3 sweep_big_topology.py
    sudo python3 sweep_big_topology.py --only groups --group-values 4,6,8 --sizes 8M --repeat 5
    sudo python3 sweep_big_topology.py --only global_links --gl-values 1,2,3,4
    sudo python3 sweep_big_topology.py --base-width 4 --base-hosts 2 --base-bw 10
"""
import os
import sys

# experiments/ -> project root on the path for the top-level modules.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mininet.clean import cleanup
from mininet.log import setLogLevel, info

import argparse
import time
from argparse import Namespace

import matplotlib
matplotlib.use("Agg")  # headless: never try to open a GUI window under sudo

from dragonfly_topo import build_network, install_gpaard_routing
from benchmark_gpaard import (
    PATTERNS, PATTERN_INFO, PortAllocator, run_pattern, format_size, parse_size,
)
from visualize_dragonfly_topo import build_topology_view
# Reuse the summary + plot helpers we already wrote for the bandwidth sweep.
from benchmark_bandwidth_sweep import summarize_topology_brief, save_topology_plot, _wrap

PATTERN = "gpaard"

# Human-readable descriptions of each swept axis, for the result-file headers.
AXIS_INFO = {
    "groups": "number of Dragonfly groups (DF and DF+ are identical for <=3 groups; "
              "they diverge in the global-exchange steps from 4 groups on)",
    "global_links": "global links per group pair (swept like 'groups'); expected to be "
                    "inert for g-PAARD -- one designated link per pair + no ECMP",
    "hosts": "hosts per switch (router/leaf); steps 3/4 are intra-group any-to-any, "
             "~O(hosts^2), stressing DF+'s 2-hop leaf-spine fabric vs DF's flat mesh",
}


def make_base(args):
    """The single fixed 'big' base topology; each sweep overrides one field."""
    return {
        "groups": args.base_groups,
        "width": args.base_width,          # routers/group (DF) = leaves = spines (DF+)
        "hosts": args.base_hosts,          # hosts per router (DF) / per leaf (DF+)
        "bw": args.base_bw,                # Mbit/s
        "delay": args.delay,
        "global_links": args.base_global_links,
    }


def cfg_with(base, axis, value):
    cfg = dict(base)
    cfg[axis] = value
    return cfg


def make_net_args(topology, cfg):
    return Namespace(
        topology=topology, groups=cfg["groups"],
        routers=cfg["width"], leaves=cfg["width"], spines=cfg["width"],
        hosts=cfg["hosts"], bw=cfg["bw"], delay=cfg["delay"],
        global_links=cfg["global_links"],
    )


def run_point(topology, cfg, args, lines, timings, value, label):
    """Build one topology (for one sweep value), plot + describe it, then run
    g-PAARD for every size --repeat times. One build serves both the plot and
    the timed run. Records iteration times into timings[(topology, value, size)]."""
    info(f"\n*** [big-sweep] building topology={topology} {label}\n")
    cleanup()
    net = build_network(make_net_args(topology, cfg))
    topo = net.topo

    # Plot + describe from the built topo (structure known before net.start()).
    view = build_topology_view(topo)
    plot_path = save_topology_plot(topo, view, args.plots_dir, label)
    lines.append("-" * 70)
    lines.append(f"TOPOLOGY UNDER TEST: {topology}   [{label}]")
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
                f"{topology}/{label}: pingAll reported {loss:.1f}% packet loss -- "
                "routing is broken, aborting before running iperf")

        steps = PATTERNS[PATTERN](topo)
        port_allocator = PortAllocator()
        for size_bytes in args.sizes:
            iter_times = []
            for i in range(args.repeat):
                total_time, _step_times = run_pattern(
                    net, steps, size_bytes, port_allocator, args.timeout)
                iter_times.append(total_time)
                info(f"*** [big-sweep] {label:28s} {topology:11s} "
                     f"size={format_size(size_bytes):>6s} iter={i + 1}/{args.repeat} "
                     f"-> {total_time:.3f}s\n")
            timings[(topology, value, size_bytes)] = iter_times
    finally:
        net.stop()


def run_sweep(axis, values, base, args):
    """Run one single-axis sweep and write its own result file."""
    lines = []
    lines.append(f"g-PAARD single-axis sweep -- varying: {axis}")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"swept axis: {axis} = {values}")
    lines.append(f"  ({AXIS_INFO[axis]})")
    fixed = {k: v for k, v in base.items() if k != axis}
    lines.append("fixed BIG base topology (everything except the swept axis):")
    lines.append(f"  groups={fixed.get('groups', '(swept)')} width={fixed['width']} "
                 f"hosts/switch={fixed.get('hosts', '(swept)')} bw={fixed['bw']}Mbit "
                 f"delay={fixed['delay']} global_links={fixed.get('global_links', '(swept)')}")
    lines.append(f"pattern: {PATTERN} (only)")
    lines.append(f"message sizes: {', '.join(format_size(s) for s in args.sizes)}")
    lines.append(f"repeat: {args.repeat}")
    lines.append("")

    timings = {}
    for value in values:
        cfg = cfg_with(base, axis, value)
        for topology in ("dragonfly", "dragonfly+"):
            label = f"{axis}_{value}_{topology.replace('+', 'plus')}"
            run_point(topology, cfg, args, lines, timings, value, label)

    # Results grouped by the swept value, then message size.
    lines.append("=" * 70)
    lines.append(f"RESULTS: g-PAARD, grouped by {axis} then message size")
    lines.append("(every iteration, then the average, then speedup from the averages)")
    lines.append("=" * 70)
    blurb = PATTERN_INFO.get(PATTERN)
    if blurb:
        lines.append(_wrap(blurb))
        lines.append("")

    for value in values:
        lines.append("-" * 70)
        lines.append(f"{axis} = {value}")
        lines.append("-" * 70)
        for size_bytes in args.sizes:
            avgs = {}
            for topology in ("dragonfly", "dragonfly+"):
                iters = timings[(topology, value, size_bytes)]
                iter_str = ", ".join(f"iter{i + 1}={t:.3f}s" for i, t in enumerate(iters))
                avg = sum(iters) / len(iters)
                avgs[topology] = avg
                lines.append(f"  {topology:11s} {format_size(size_bytes):>6s}: "
                             f"[{iter_str}]  avg={avg:.3f}s")
            df_avg, dfp_avg = avgs["dragonfly"], avgs["dragonfly+"]
            if dfp_avg > 0:
                lines.append(f"  speedup ({format_size(size_bytes)}, {axis}={value}, "
                             f"avg dragonfly / avg dragonfly+): {df_avg / dfp_avg:.3f}")
            lines.append("")

    # Compact <axis> x size speedup matrix for a quick at-a-glance read.
    lines.append("=" * 70)
    lines.append(f"SPEEDUP MATRIX (avg dragonfly / avg dragonfly+)  [>1 = DF+ faster]")
    lines.append(f"rows = {axis}, columns = message size")
    lines.append("=" * 70)
    lines.append(f"  {axis:>10s}  " + "".join(f"{format_size(s):>10s}" for s in args.sizes))
    for value in values:
        row = f"  {value:>10}  "
        for size_bytes in args.sizes:
            df_avg = sum(timings[("dragonfly", value, size_bytes)]) / args.repeat
            dfp_avg = sum(timings[("dragonfly+", value, size_bytes)]) / args.repeat
            ratio = df_avg / dfp_avg if dfp_avg > 0 else float("nan")
            row += f"{ratio:>10.3f}"
        lines.append(row)
    lines.append("")

    out_path = f"{args.out_prefix}_{axis}.txt"
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [big-sweep] results written to {out_path}\n")


SWEEP_VALUE_ATTR = {
    "groups": "group_values",
    "global_links": "gl_values",
    "hosts": "host_values",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="g-PAARD single-axis sweeps (groups / global_links / hosts) on one "
                    "fixed big base topology, comparing Dragonfly vs Dragonfly+. Keeps the "
                    "topology plot, --repeat and --sizes; only the g-PAARD pattern runs.")
    parser.add_argument("--only", type=str, default="groups,global_links,hosts",
                        help="comma-separated sweeps to run: groups, global_links, hosts "
                             "(default: all three)")
    # The swept values for each axis.
    parser.add_argument("--group-values", type=str, default="4,5,6,7,8",
                        help="group counts for the 'groups' sweep (default: 4,5,6,7,8)")
    parser.add_argument("--gl-values", type=str, default="1,2,3,4",
                        help="global-links-per-pair for the 'global_links' sweep (default: 1,2,3,4)")
    parser.add_argument("--host-values", type=str, default="1,2,4,8",
                        help="hosts-per-switch for the 'hosts' sweep (default: 1,2,4,8)")
    # The fixed BIG base topology (the non-swept fields come from these).
    parser.add_argument("--base-groups", type=int, default=4,
                        help="fixed group count for the global_links/hosts sweeps (default: 4)")
    parser.add_argument("--base-width", type=int, default=4,
                        help="fixed routers/leaves/spines per group (default: 4)")
    parser.add_argument("--base-hosts", type=int, default=2,
                        help="fixed hosts per switch for the groups/global_links sweeps (default: 2)")
    parser.add_argument("--base-bw", type=float, default=10,
                        help="fixed link bandwidth in Mbit/s (default: 10)")
    parser.add_argument("--base-global-links", type=int, default=1,
                        help="fixed global links per pair for the groups/hosts sweeps (default: 1)")
    # Shared knobs (kept from the earlier sweeps).
    parser.add_argument("--sizes", type=str, default="1M,8M",
                        help="comma-separated message sizes, e.g. '1M,8M' (default: 1M,8M)")
    parser.add_argument("--delay", type=str, default="1ms", help="per-link delay (default: 1ms)")
    parser.add_argument("--settle", type=int, default=1, help="seconds to wait after installing flows (default: 1)")
    parser.add_argument("--repeat", type=int, default=3, help="iterations per (value, size) (default: 3)")
    parser.add_argument("--timeout", type=int, default=60, help="per-message iperf timeout in seconds (default: 60)")
    parser.add_argument("--out-prefix", type=str, default="results_bigtopo",
                        help="result files are written to <prefix>_<axis>.txt (default: results_bigtopo)")
    parser.add_argument("--plots-dir", type=str, default="big_topology_plots",
                        help="directory for the topology plot images (default: big_topology_plots)")
    args = parser.parse_args(argv)

    args.only = [s.strip() for s in args.only.split(",") if s.strip()]
    for s in args.only:
        if s not in SWEEP_VALUE_ATTR:
            parser.error(f"unknown sweep '{s}', choose from: {', '.join(SWEEP_VALUE_ATTR)}")

    args.group_values = [int(v) for v in args.group_values.split(",") if v.strip()]
    args.gl_values = [int(v) for v in args.gl_values.split(",") if v.strip()]
    args.host_values = [int(v) for v in args.host_values.split(",") if v.strip()]
    args.sizes = [parse_size(s) for s in args.sizes.split(",") if s.strip()]
    if not args.sizes:
        parser.error("need at least one size in --sizes")
    return args


def main():
    args = parse_args()  # handles --help before the root check below

    if os.geteuid() != 0:
        print("sweep_big_topology.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    setLogLevel("info")

    base = make_base(args)
    for axis in args.only:
        values = getattr(args, SWEEP_VALUE_ATTR[axis])
        run_sweep(axis, values, base, args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
