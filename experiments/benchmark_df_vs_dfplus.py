#!/usr/bin/env python3
"""
benchmark_df_vs_dfplus.py

The "final comparison" harness: a focused, statistically-honest Dragonfly vs
Dragonfly+ g-PAARD benchmark, built after every earlier sweep
(benchmark_topology_sweep, benchmark_bandwidth_sweep, sweep_big_topology,
benchmark_asymmetric_groups) came back with speedup ratios bouncing noisily
around 1.0 with no consistent direction. Three concrete problems were found
in that noise and are fixed here:

1. TOTAL TIME MIXES TWO DIFFERENT THINGS.
   g-PAARD's 6 steps split into two kinds (see gpaard.py's own docstring):
     - intra-group steps 1, 3, 4, 6: collector fan-in/out + full any-to-any.
       DF's routers are an all-to-all mesh (1 hop between any two routers in
       a group); DF+'s leaves only reach each other via a spine (2 hops).
       This is the ONLY place DF and DF+ can structurally differ.
     - inter-group steps 2, 5: designated hosts exchange directly over the
       pre-existing global link -- IDENTICAL cost on both topologies.
   Every earlier sweep reported total time across all 6 steps, so half the
   schedule contributes equal noise to both arms and dilutes the one signal
   that matters. This file records step_times (already computed by
   run_pattern, previously discarded) and reports intra-group time
   separately from inter-group time, plus the total.

2. NOT ENOUGH REPEATS FOR THE OBSERVED NOISE.
   Individual iterations on an identical config have been seen to swing 2-3x
   (e.g. 2.2s to 6.2s). --repeat has topped out at 5 in every earlier sweep,
   which is not enough samples to tell a real 10-20% effect from noise.
   Default here is --repeat 20, and every reported number comes with a 95%
   confidence interval (Student's t, stdlib-only -- no scipy available in
   this environment) so a reader can see whether DF vs DF+ actually differ
   or whether the CIs overlap and the comparison is inconclusive.

3. DF's REPEATS AND DF+'s REPEATS WERE NEVER INTERLEAVED.
   Every earlier sweep ran all of DF's iterations, then (much later in wall
   clock time) all of DF+'s. Any system-level drift over that time (load,
   thermal, whatever) biases the comparison. True per-iteration interleaving
   would mean two live Mininet networks at once, doubling the resource
   budget that already caused one sweep to grind to a near-halt (see #4) --
   not worth it at this scale. The cheaper mitigation used here: alternate
   WHICH topology is measured first across bandwidth values (zigzag), so a
   monotonic drift across the whole run affects both arms roughly equally
   rather than always penalizing whichever topology runs second.

4. RESOURCE CEILING.
   Step 3/4 (any-to-any) message count is O(hosts-per-group^2). A previous
   run at hosts-per-group=24 generated ~2,200 concurrent iperf flows and
   overwhelmed the VM (thousands of child processes, runs effectively
   stalled). The default here (--width 4 --hosts 2, 8 hosts/group) is the
   configuration already validated to run cleanly in
   results_bigtopo_groups_demo.txt. --width * --hosts above ~10 prints a
   loud warning.

Only the g-PAARD pattern runs. groups defaults to 4,8 -- the known
divergence boundary (DF and DF+ are structurally identical below 4 groups).
bandwidths/sizes default to the "large message / low bandwidth" corner
earlier analysis already flagged as where the topology difference should be
visible at all (small messages / high bandwidth measure iperf/TCP overhead,
not the network).

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/benchmark_df_vs_dfplus.py
    sudo python3 experiments/benchmark_df_vs_dfplus.py --groups 8 --repeat 30 --sizes 4M
"""

from mininet.clean import cleanup
from mininet.log import setLogLevel, info

import argparse
import math
import os
import statistics
import sys
import time
from argparse import Namespace

import matplotlib
matplotlib.use("Agg")  # headless: never try to open a GUI window under sudo

# This file lives in experiments/; put the project root on the path so the
# top-level modules (dragonfly_topo, benchmark_gpaard, ...) import cleanly
# whether run as `python3 experiments/benchmark_df_vs_dfplus.py` or from here.
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

# pattern_gpaard() returns schedule["reduce_scatter"] + schedule["all_gather"]
# = [step1, step2, step3, step4, step5, step6], indices 0..5. Per gpaard.py's
# own docstring: steps 1/3/4/6 are intra-group (where DF and DF+ can differ),
# steps 2/5 are inter-group over the pre-existing global link (identical
# cost on both topologies by construction).
INTRA_STEP_IDX = (0, 2, 3, 5)
INTER_STEP_IDX = (1, 4)

# Two-sided 95% Student's t critical values, indexed by degrees of freedom
# (= n - 1). No scipy in this environment; this stdlib-only table covers the
# repeat counts this script actually uses. Falls back to the normal
# approximation (1.96) beyond df=30, where t is already within ~0.04 of it.
_T_CRIT_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def t_critical_95(df):
    if df < 1:
        return float("nan")
    return _T_CRIT_95.get(df, 1.96)


def mean_ci95(samples):
    """(mean, half_width) for a 95% CI. half_width is NaN for n<2."""
    n = len(samples)
    mean = sum(samples) / n
    if n < 2:
        return mean, float("nan")
    stdev = statistics.stdev(samples)
    half_width = t_critical_95(n - 1) * stdev / math.sqrt(n)
    return mean, half_width


def ci_overlap(mean_a, hw_a, mean_b, hw_b):
    """True if the two 95% CIs overlap (i.e. difference is NOT clearly
    significant at this informal level of rigor)."""
    if math.isnan(hw_a) or math.isnan(hw_b):
        return True  # can't claim significance without a real interval
    lo_a, hi_a = mean_a - hw_a, mean_a + hw_a
    lo_b, hi_b = mean_b - hw_b, mean_b + hw_b
    return lo_a <= hi_b and lo_b <= hi_a


def classify_step_times(step_times):
    intra = sum(step_times[i] for i in INTRA_STEP_IDX)
    inter = sum(step_times[i] for i in INTER_STEP_IDX)
    return intra, inter


def build_topology_args(topology, cfg, bw):
    return Namespace(
        topology=topology, groups=cfg["groups"], routers=cfg["width"],
        leaves=cfg["width"], spines=cfg["width"], hosts=cfg["hosts"],
        bw=bw, delay=cfg["delay"], global_links=cfg["global_links"],
    )


def run_topology_at_bw(topology, cfg, bw, args, lines, timings):
    """Build one topology at bandwidth `bw`, run g-PAARD for every size
    --repeat times, recording (total, intra, inter) time per iteration."""
    info(f"\n*** [df-vs-dfplus] building topology={topology} groups={cfg['groups']} bw={bw}Mbit\n")
    cleanup()
    net = build_network(build_topology_args(topology, cfg, bw))
    topo = net.topo

    view = build_topology_view(topo)
    label = f"groups{cfg['groups']}_bw{bw:g}_{topology.replace('+', 'plus')}"
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
                f"{topology}/groups={cfg['groups']}/bw={bw}: pingAll reported "
                f"{loss:.1f}% packet loss -- routing is broken, aborting before running iperf")

        steps = PATTERNS[PATTERN](topo)
        port_allocator = PortAllocator()
        for size_bytes in args.sizes:
            totals, intras, inters = [], [], []
            for i in range(args.repeat):
                total_time, step_times = run_pattern(
                    net, steps, size_bytes, port_allocator, args.timeout)
                intra, inter = classify_step_times(step_times)
                totals.append(total_time)
                intras.append(intra)
                inters.append(inter)
                info(f"*** [df-vs-dfplus] groups={cfg['groups']:2d} bw={bw:>5g} "
                     f"{topology:11s} size={format_size(size_bytes):>6s} "
                     f"iter={i + 1}/{args.repeat} -> total={total_time:.3f}s "
                     f"(intra={intra:.3f}s inter={inter:.3f}s)\n")
            timings[(topology, cfg["groups"], bw, size_bytes)] = {
                "total": totals, "intra": intras, "inter": inters,
            }
    finally:
        net.stop()


def run_groups_config(cfg, args):
    lines = []
    lines.append("=" * 70)
    lines.append(f"CONFIG: groups={cfg['groups']} width={cfg['width']} hosts/router={cfg['hosts']} "
                 f"({cfg['width'] * cfg['hosts']} hosts/group) global_links={cfg['global_links']}")
    lines.append("=" * 70)
    lines.append("")

    timings = {}
    for bw_idx, bw in enumerate(args.bandwidths):
        # Zigzag which topology is measured first, to spread any monotonic
        # system-level drift across the run roughly evenly between arms
        # instead of always penalizing whichever topology goes second.
        order = ("dragonfly", "dragonfly+") if bw_idx % 2 == 0 else ("dragonfly+", "dragonfly")
        for topology in order:
            run_topology_at_bw(topology, cfg, bw, args, lines, timings)

    lines.append("=" * 70)
    lines.append(f"RESULTS for groups={cfg['groups']}: mean [95% CI], n={args.repeat}")
    lines.append("intra-group time (steps 1,3,4,6) is where DF vs DF+ CAN structurally "
                 "differ; inter-group time (steps 2,5) is identical by construction and "
                 "shown only as a sanity check.")
    lines.append("significant? = the two topologies' 95% CIs do NOT overlap (informal, "
                 "not a formal hypothesis test)")
    lines.append("=" * 70)

    summary_rows = []  # for the final matrix
    for bw in args.bandwidths:
        lines.append("-" * 70)
        lines.append(f"BANDWIDTH: {bw:g} Mbit/s")
        lines.append("-" * 70)
        for size_bytes in args.sizes:
            lines.append(f"  size={format_size(size_bytes)}")
            stats = {}
            for topology in ("dragonfly", "dragonfly+"):
                data = timings[(topology, cfg["groups"], bw, size_bytes)]
                stats[topology] = {
                    metric: mean_ci95(data[metric]) for metric in ("total", "intra", "inter")
                }
                t_mean, t_hw = stats[topology]["total"]
                i_mean, i_hw = stats[topology]["intra"]
                e_mean, e_hw = stats[topology]["inter"]
                lines.append(f"    {topology:11s} total={t_mean:6.3f}s +/- {t_hw:5.3f}  "
                             f"intra={i_mean:6.3f}s +/- {i_hw:5.3f}  "
                             f"inter={e_mean:6.3f}s +/- {e_hw:5.3f}")

            for metric in ("total", "intra"):
                df_mean, df_hw = stats["dragonfly"][metric]
                dfp_mean, dfp_hw = stats["dragonfly+"][metric]
                speedup = df_mean / dfp_mean if dfp_mean > 0 else float("nan")
                sig = not ci_overlap(df_mean, df_hw, dfp_mean, dfp_hw)
                lines.append(f"    speedup ({metric}, avg dragonfly / avg dragonfly+): "
                             f"{speedup:.3f}  significant={sig}")
                if metric == "total":
                    row_total = (bw, size_bytes, speedup, sig)
                else:
                    row_intra_speedup, row_intra_sig = speedup, sig
            summary_rows.append((bw, size_bytes, row_total[2], row_total[3],
                                 row_intra_speedup, row_intra_sig))
            lines.append("")

    lines.append("=" * 70)
    lines.append(f"SUMMARY MATRIX for groups={cfg['groups']} "
                 "(speedup = avg dragonfly / avg dragonfly+, >1 = DF+ faster)")
    lines.append("=" * 70)
    lines.append(f"  {'bw':>8s} {'size':>6s} {'total_speedup':>14s} {'sig':>5s} "
                 f"{'intra_speedup':>14s} {'sig':>5s}")
    for bw, size_bytes, tot_sp, tot_sig, intra_sp, intra_sig in summary_rows:
        lines.append(f"  {bw:>8g} {format_size(size_bytes):>6s} {tot_sp:>14.3f} "
                     f"{str(tot_sig):>5s} {intra_sp:>14.3f} {str(intra_sig):>5s}")
    lines.append("")

    return lines


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Focused, statistically-honest Dragonfly vs Dragonfly+ g-PAARD "
                    "comparison: separates intra-group step time (where DF/DF+ can "
                    "differ) from inter-group step time (identical by construction), "
                    "reports 95% CIs instead of bare averages, and alternates "
                    "measurement order to reduce drift bias.")
    parser.add_argument("--groups", type=str, default="4,8",
                        help="comma-separated group counts to test (default: 4,8)")
    parser.add_argument("--width", type=int, default=4,
                        help="routers/group (DF) = leaves=spines/group (DF+), "
                             "uniform across groups (default: 4)")
    parser.add_argument("--hosts", type=int, default=2,
                        help="hosts per router/leaf (default: 2); width*hosts is the "
                             "per-group host count driving the O(n^2) any-to-any step, "
                             "keep this <= ~10 or the run can grind to a near-halt")
    parser.add_argument("--bandwidths", type=str, default="10,50",
                        help="comma-separated link bandwidths in Mbit/s (default: 10,50)")
    parser.add_argument("--sizes", type=str, default="1M,4M",
                        help="comma-separated message sizes (default: 1M,4M)")
    parser.add_argument("--delay", type=str, default="1ms", help="per-link delay (default: 1ms)")
    parser.add_argument("--global-links", type=int, default=1,
                        help="global links per group pair (default: 1)")
    parser.add_argument("--repeat", type=int, default=20,
                        help="iterations per (groups, bandwidth, size, topology) "
                             "(default: 20; earlier sweeps used 5, which was not "
                             "enough to separate a real effect from noise)")
    parser.add_argument("--settle", type=int, default=1,
                        help="seconds to wait after installing flows (default: 1)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="per-message iperf timeout in seconds (default: 60)")
    parser.add_argument("--out-prefix", type=str, default="results_df_vs_dfplus",
                        help="results are written to <prefix>.txt (default: results_df_vs_dfplus)")
    parser.add_argument("--plots-dir", type=str, default="df_vs_dfplus_topology_plots",
                        help="directory for topology plot images "
                             "(default: df_vs_dfplus_topology_plots)")
    args = parser.parse_args(argv)

    args.groups = [int(g) for g in args.groups.split(",") if g.strip()]
    if not args.groups:
        parser.error("need at least one group count in --groups")
    args.bandwidths = [float(b) for b in args.bandwidths.split(",") if b.strip()]
    if not args.bandwidths:
        parser.error("need at least one bandwidth in --bandwidths")
    args.sizes = [parse_size(s) for s in args.sizes.split(",") if s.strip()]
    if not args.sizes:
        parser.error("need at least one size in --sizes")

    hosts_per_group = args.width * args.hosts
    if hosts_per_group > 10:
        print(f"WARNING: width({args.width}) * hosts({args.hosts}) = {hosts_per_group} "
              f"hosts/group. Step 3/4's any-to-any exchange is O(hosts-per-group^2) "
              f"messages PER GROUP -- a previous run at 24 hosts/group generated ~2,200 "
              f"concurrent iperf flows and overwhelmed the VM. {hosts_per_group} hosts/group "
              f"means ~{hosts_per_group * (hosts_per_group - 1)} concurrent messages per "
              f"group per step. Proceed only if you know this host can take it.",
              file=sys.stderr)

    return args


def main():
    args = parse_args()  # handles --help before the root check below

    if os.geteuid() != 0:
        print("benchmark_df_vs_dfplus.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    setLogLevel("info")

    all_lines = []
    all_lines.append("g-PAARD Dragonfly vs Dragonfly+ final comparison")
    all_lines.append("=" * 70)
    all_lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    all_lines.append(f"groups={args.groups} width={args.width} hosts/router={args.hosts} "
                     f"({args.width * args.hosts} hosts/group) global_links={args.global_links} "
                     f"delay={args.delay}")
    all_lines.append(f"pattern: {PATTERN} (only)")
    all_lines.append(f"bandwidths (Mbit/s): {', '.join('%g' % b for b in args.bandwidths)}")
    all_lines.append(f"message sizes: {', '.join(format_size(s) for s in args.sizes)}")
    all_lines.append(f"repeat: {args.repeat}")
    all_lines.append("")
    blurb = PATTERN_INFO.get(PATTERN)
    if blurb:
        all_lines.append(_wrap(blurb))
        all_lines.append("")

    for groups in args.groups:
        cfg = {
            "groups": groups, "width": args.width, "hosts": args.hosts,
            "delay": args.delay, "global_links": args.global_links,
        }
        all_lines.extend(run_groups_config(cfg, args))

    out_path = f"{args.out_prefix}.txt"
    with open(out_path, "w") as f:
        f.write("\n".join(all_lines) + "\n")
    info(f"\n*** [df-vs-dfplus] results written to {out_path}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
