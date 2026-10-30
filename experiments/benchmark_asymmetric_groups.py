#!/usr/bin/env python3
"""
benchmark_asymmetric_groups.py

Dragonfly vs Dragonfly+ g-PAARD benchmark where groups do NOT all have the
same number of end hosts. Every earlier sweep (benchmark_topology_sweep,
benchmark_bandwidth_sweep, sweep_big_topology) scales hosts-per-switch
UNIFORMLY across every group -- real clusters are rarely that tidy (groups
get built out, upgraded, or thinned over time at different rates). This file
asks: does per-group host imbalance change the DF-vs-DF+ picture?

Why not just call `random.randint()` per group and move on: a fresh uniform
draw per group mostly produces "mildly lumpy, basically symmetric" topologies
-- it rarely explores the cases that would actually explain a result, and
it gives no story to attach to an outcome ("why was this run slower?"). So
instead of pure randomness, this sweeps a small set of named, HYPOTHESIS-
carrying SHAPES (`SHAPES` below), each describing one real-world way group
sizes diverge. Only the randomized shapes draw from an RNG -- and even then,
only to pick *where* the asymmetry lands (which group, how much jitter),
never *whether* it exists. Each randomized shape is repeated over several
--seeds so the reported number is an average over placement, not one draw.

Group RADIX (routers/group for DF, leaves=spines/group for DF+) is kept
UNIFORM across groups in every shape; only hosts-per-router / hosts-per-leaf
varies per group. This isolates "how many compute nodes does this group
have" from "how big is this group's switch fabric" -- otherwise an observed
effect could come from either knob and the result would not isolate
anything.

Shapes:
  uniform          -- control: every group has --base-hosts hosts. No
                       asymmetry at all; every other shape is judged against
                       this baseline.
  mild_jitter       -- +/-1 host per group. The noise floor: realistic minor
                       hardware variance, nothing structural.
  one_fat_group     -- one random group gets --fat-factor x the hosts, every
                       other group is at baseline. Models a single dense
                       rack / bottleneck group. Hypothesis: g-PAARD's steps
                       3/4 any-to-any inside that one group is O(hosts^2),
                       so the WHOLE run (every group is barriered step by
                       step) is paced by that one group's local fabric --
                       this should be the shape where DF's flat 1-hop mesh
                       vs DF+'s 2-hop leaf-spine fabric matters most.
  one_thin_group    -- the mirror image: one random group is starved down to
                       1 host, everyone else at baseline. Tests whether a
                       near-empty group (trivial local step, nothing to hide
                       global-exchange latency behind) changes the picture.
  linear_ramp       -- deterministic, group sizes rise from ~half to ~double
                       --base-hosts across the group index. Models gradual,
                       incremental cluster growth (older/smaller groups next
                       to newer/bigger ones).
  bimodal           -- deterministic, alternating fat/thin groups. Models a
                       two-tier cluster (e.g. compute groups interleaved with
                       lighter edge/IO groups).
  power_law         -- Zipf-weighted sizes (rank r gets weight 1/r) with the
                       rank<->group assignment shuffled per seed. Models a
                       realistic skewed fleet: a couple of big groups, a
                       long tail of small ones, position randomized.

Only the g-PAARD pattern is run (the workload this project targets). Reuses
the same building blocks as the other sweeps: topology build + routing from
dragonfly_topo, the g-PAARD schedule + iperf runner from benchmark_gpaard,
and the topology summary/plot helpers from benchmark_bandwidth_sweep.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/benchmark_asymmetric_groups.py
    sudo python3 experiments/benchmark_asymmetric_groups.py --shapes one_fat_group,power_law --seeds 5
    sudo python3 experiments/benchmark_asymmetric_groups.py --groups 6 --base-hosts 3 --sizes 1M,4M --repeat 5
"""

from mininet.clean import cleanup
from mininet.log import setLogLevel, info

import argparse
import os
import random
import sys
import time
from argparse import Namespace

import matplotlib
matplotlib.use("Agg")  # headless: never try to open a GUI window under sudo

# This file lives in experiments/; put the project root on the path so the
# top-level modules (dragonfly_topo, benchmark_gpaard, ...) import cleanly
# whether run as `python3 experiments/benchmark_asymmetric_groups.py` or from here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dragonfly_topo import build_network, install_gpaard_routing
from benchmark_gpaard import (
    PATTERNS, PATTERN_INFO,
    PortAllocator, run_pattern, format_size, parse_size,
)
from visualize_dragonfly_topo import build_topology_view
# Reuse the summary + plot helpers already written for the bandwidth sweep.
from benchmark_bandwidth_sweep import summarize_topology_brief, save_topology_plot, _wrap

PATTERN = "gpaard"


# ---------------------------------------------------------------------------
# Shapes: each is (generator, is_randomized, blurb). generator(num_groups,
# base_hosts, rng) -> list[int] of length num_groups, one host count per
# group. Only randomized shapes consume `rng`; deterministic ones ignore it.
# ---------------------------------------------------------------------------
def _shape_uniform(num_groups, base_hosts, rng):
    return [base_hosts] * num_groups


def _shape_mild_jitter(num_groups, base_hosts, rng):
    return [max(1, base_hosts + rng.randint(-1, 1)) for _ in range(num_groups)]


def _shape_one_fat_group(num_groups, base_hosts, rng, factor=4):
    sizes = [base_hosts] * num_groups
    sizes[rng.randrange(num_groups)] = base_hosts * factor
    return sizes


def _shape_one_thin_group(num_groups, base_hosts, rng):
    sizes = [base_hosts] * num_groups
    sizes[rng.randrange(num_groups)] = 1
    return sizes


def _shape_linear_ramp(num_groups, base_hosts, rng):
    if num_groups == 1:
        return [base_hosts]
    lo, hi = max(1, base_hosts // 2), base_hosts * 2
    step = (hi - lo) / (num_groups - 1)
    return [max(1, round(lo + step * i)) for i in range(num_groups)]


def _shape_bimodal(num_groups, base_hosts, rng):
    fat, thin = base_hosts * 2, max(1, base_hosts // 2)
    return [fat if i % 2 == 0 else thin for i in range(num_groups)]


def _shape_power_law(num_groups, base_hosts, rng):
    weights = [1.0 / r for r in range(1, num_groups + 1)]
    total_weight = sum(weights)
    target_total = num_groups * base_hosts
    sizes = [max(1, round(w / total_weight * target_total)) for w in weights]
    rng.shuffle(sizes)
    return sizes


SHAPES = {
    "uniform": (_shape_uniform, False,
                "control: every group has base_hosts hosts, no asymmetry"),
    "mild_jitter": (_shape_mild_jitter, True,
                    "+/-1 host per group -- noise floor, realistic minor variance"),
    "one_fat_group": (_shape_one_fat_group, True,
                      "one random group at fat_factor x hosts, rest at baseline -- "
                      "a dense bottleneck group"),
    "one_thin_group": (_shape_one_thin_group, True,
                       "one random group starved to 1 host, rest at baseline -- "
                       "a straggler group"),
    "linear_ramp": (_shape_linear_ramp, False,
                    "sizes ramp linearly from ~half to ~double base_hosts across "
                    "the group index -- incremental cluster growth"),
    "bimodal": (_shape_bimodal, False,
                "alternating fat/thin groups -- two-tier cluster composition"),
    "power_law": (_shape_power_law, True,
                  "Zipf-weighted sizes, fat-group position shuffled per seed -- "
                  "a skewed real-world fleet"),
}


def build_topology_args(topology, cfg, hosts_list):
    return Namespace(
        topology=topology, groups=cfg["groups"], routers=cfg["width"],
        leaves=cfg["width"], spines=cfg["width"], hosts=hosts_list,
        bw=cfg["bw"], delay=cfg["delay"], global_links=cfg["global_links"],
    )


def run_instance(topology, cfg, hosts_list, label, args, lines, timings, shape, seed_idx):
    """Build one topology for one (shape, seed) host assignment, plot +
    describe it, then run g-PAARD for every size --repeat times."""
    info(f"\n*** [asym-sweep] building topology={topology} {label}\n")
    cleanup()
    net = build_network(build_topology_args(topology, cfg, hosts_list))
    topo = net.topo

    view = build_topology_view(topo)
    plot_path = save_topology_plot(topo, view, args.plots_dir, label)
    nodes_per_group = [cfg["width"] * h for h in hosts_list]
    lines.append("-" * 70)
    lines.append(f"TOPOLOGY UNDER TEST: {topology}   [{label}]")
    lines.append("-" * 70)
    lines.append(f"hosts/router (per group) = {hosts_list}")
    lines.append(f"nodes/group (= width * hosts/router) = {nodes_per_group}  "
                 f"(total nodes = {sum(nodes_per_group)})")
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
                info(f"*** [asym-sweep] {label:24s} {topology:11s} "
                     f"size={format_size(size_bytes):>6s} iter={i + 1}/{args.repeat} "
                     f"-> {total_time:.3f}s\n")
            timings[(topology, shape, seed_idx, size_bytes)] = iter_times
    finally:
        net.stop()


def run_shape(shape, cfg, args, master_rng):
    """Run every seed instance of one shape, return (lines, timings, seed_count)."""
    generator, is_randomized, blurb = SHAPES[shape]
    seed_count = args.seeds if is_randomized else 1

    lines = []
    lines.append("=" * 70)
    lines.append(f"SHAPE: {shape}  ({'randomized x%d seeds' % seed_count if is_randomized else 'deterministic'})")
    lines.append(_wrap(blurb))
    lines.append("=" * 70)
    lines.append("")

    timings = {}
    host_assignments = {}
    for seed_idx in range(seed_count):
        rng = random.Random(master_rng.random())
        if shape == "one_fat_group":
            hosts_list = generator(cfg["groups"], cfg["base_hosts"], rng, cfg["fat_factor"])
        else:
            hosts_list = generator(cfg["groups"], cfg["base_hosts"], rng)
        host_assignments[seed_idx] = hosts_list
        for topology in ("dragonfly", "dragonfly+"):
            label = f"{shape}_s{seed_idx}_{topology.replace('+', 'plus')}"
            run_instance(topology, cfg, hosts_list, label, args, lines, timings, shape, seed_idx)

    # Results for this shape: per-seed iterations/averages, then the
    # shape-level average (mean over seeds of each seed's average).
    lines.append("-" * 70)
    lines.append(f"RESULTS for shape={shape}")
    lines.append("-" * 70)
    size_seed_avgs = {size: {"dragonfly": [], "dragonfly+": []} for size in args.sizes}
    for seed_idx in range(seed_count):
        hosts_list = host_assignments[seed_idx]
        nodes_per_group = [cfg["width"] * h for h in hosts_list]
        lines.append(f"  seed {seed_idx}: hosts/router={hosts_list} "
                     f"-> nodes/group={nodes_per_group} (total={sum(nodes_per_group)})")
        for size_bytes in args.sizes:
            avgs = {}
            for topology in ("dragonfly", "dragonfly+"):
                iters = timings[(topology, shape, seed_idx, size_bytes)]
                iter_str = ", ".join(f"iter{i + 1}={t:.3f}s" for i, t in enumerate(iters))
                avg = sum(iters) / len(iters)
                avgs[topology] = avg
                size_seed_avgs[size_bytes][topology].append(avg)
                lines.append(f"    {topology:11s} {format_size(size_bytes):>6s}: "
                             f"[{iter_str}]  avg={avg:.3f}s")
            df_avg, dfp_avg = avgs["dragonfly"], avgs["dragonfly+"]
            if dfp_avg > 0:
                lines.append(f"    speedup ({format_size(size_bytes)}, seed={seed_idx}, "
                             f"avg dragonfly / avg dragonfly+): {df_avg / dfp_avg:.3f}")
        lines.append("")

    lines.append(f"  shape-level average over {seed_count} seed(s):")
    shape_speedups = {}
    for size_bytes in args.sizes:
        df_mean = sum(size_seed_avgs[size_bytes]["dragonfly"]) / seed_count
        dfp_mean = sum(size_seed_avgs[size_bytes]["dragonfly+"]) / seed_count
        speedup = df_mean / dfp_mean if dfp_mean > 0 else float("nan")
        shape_speedups[size_bytes] = speedup
        lines.append(f"    {format_size(size_bytes):>6s}: avg dragonfly={df_mean:.3f}s "
                     f"avg dragonfly+={dfp_mean:.3f}s  speedup={speedup:.3f}")
    lines.append("")

    return lines, shape_speedups


def run_all(args):
    master_rng = random.Random(args.seed)
    cfg = {
        "groups": args.groups, "width": args.width, "base_hosts": args.base_hosts,
        "bw": args.bw, "delay": args.delay, "global_links": args.global_links,
        "fat_factor": args.fat_factor,
    }

    header = []
    header.append("g-PAARD asymmetric-groups sweep")
    header.append("=" * 70)
    header.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    header.append(f"groups={args.groups} width={args.width} (routers=leaves=spines, "
                  f"uniform across groups) base_hosts={args.base_hosts} bw={args.bw}Mbit "
                  f"delay={args.delay} global_links={args.global_links}")
    header.append(f"pattern: {PATTERN} (only)")
    header.append(f"message sizes: {', '.join(format_size(s) for s in args.sizes)}")
    header.append(f"repeat: {args.repeat}  seeds (randomized shapes only): {args.seeds}  "
                  f"master seed: {args.seed}")
    header.append(f"shapes: {', '.join(args.shapes)}")
    header.append("")
    blurb = PATTERN_INFO.get(PATTERN)
    if blurb:
        header.append(_wrap(blurb))
        header.append("")

    all_lines = list(header)
    all_speedups = {}
    for shape in args.shapes:
        shape_lines, shape_speedups = run_shape(shape, cfg, args, master_rng)
        all_lines.extend(shape_lines)
        all_speedups[shape] = shape_speedups

    all_lines.append("=" * 70)
    all_lines.append("SPEEDUP MATRIX (avg dragonfly / avg dragonfly+, averaged over seeds)  [>1 = DF+ faster]")
    all_lines.append("rows = shape, columns = message size")
    all_lines.append("=" * 70)
    all_lines.append("  " + f"{'shape':<16s}" + "".join(f"{format_size(s):>10s}" for s in args.sizes))
    for shape in args.shapes:
        row = f"  {shape:<16s}"
        for size_bytes in args.sizes:
            row += f"{all_speedups[shape][size_bytes]:>10.3f}"
        all_lines.append(row)
    all_lines.append("")

    out_path = f"{args.out_prefix}.txt"
    with open(out_path, "w") as f:
        f.write("\n".join(all_lines) + "\n")
    info(f"\n*** [asym-sweep] results written to {out_path}\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Dragonfly vs Dragonfly+ g-PAARD benchmark with per-group host "
                    "count asymmetry, swept across a small set of named, hypothesis-"
                    "carrying shapes rather than pure per-group randomness.")
    parser.add_argument("--groups", type=int, default=5, help="number of groups (default: 5)")
    parser.add_argument("--width", type=int, default=2,
                        help="routers/group (DF) = leaves=spines/group (DF+), uniform "
                             "across groups (default: 2)")
    parser.add_argument("--base-hosts", type=int, default=2,
                        help="the typical per-group host count every shape scales "
                             "around (default: 2)")
    parser.add_argument("--fat-factor", type=int, default=4,
                        help="multiplier used by the one_fat_group shape (default: 4)")
    parser.add_argument("--shapes", type=str, default=",".join(SHAPES.keys()),
                        help="comma-separated shape names to run, default: all "
                             f"({', '.join(SHAPES.keys())})")
    parser.add_argument("--seeds", type=int, default=3,
                        help="random seeds averaged per randomized shape (default: 3)")
    parser.add_argument("--seed", type=int, default=0,
                        help="master RNG seed, for reproducibility (default: 0)")
    parser.add_argument("--bw", type=float, default=100, help="link bandwidth in Mbit/s (default: 100)")
    parser.add_argument("--delay", type=str, default="1ms", help="per-link delay (default: 1ms)")
    parser.add_argument("--global-links", type=int, default=1,
                        help="global links per group pair (default: 1)")
    parser.add_argument("--sizes", type=str, default="4K,1M",
                        help="comma-separated message sizes (default: 4K,1M)")
    parser.add_argument("--repeat", type=int, default=3,
                        help="iterations per (shape, seed, size) (default: 3)")
    parser.add_argument("--settle", type=int, default=1,
                        help="seconds to wait after installing flows (default: 1)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="per-message iperf timeout in seconds (default: 60)")
    parser.add_argument("--out-prefix", type=str, default="results_asymmetric_groups",
                        help="results are written to <prefix>.txt (default: results_asymmetric_groups)")
    parser.add_argument("--plots-dir", type=str, default="asymmetric_topology_plots",
                        help="directory for topology plot images (default: asymmetric_topology_plots)")
    args = parser.parse_args(argv)

    args.shapes = [s.strip() for s in args.shapes.split(",") if s.strip()]
    for s in args.shapes:
        if s not in SHAPES:
            parser.error(f"unknown shape '{s}' -- choices: {', '.join(SHAPES.keys())}")
    args.sizes = [parse_size(s) for s in args.sizes.split(",") if s.strip()]
    if not args.sizes:
        parser.error("need at least one size in --sizes")
    return args


def main():
    args = parse_args()  # handles --help before the root check below

    if os.geteuid() != 0:
        print("benchmark_asymmetric_groups.py must run as root (Mininet needs to "
              "manipulate network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    setLogLevel("info")
    run_all(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
