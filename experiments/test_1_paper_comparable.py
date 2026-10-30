#!/usr/bin/env python3
"""
test_1_paper_comparable.py

DF_vs_DF_Plus_testplan.md, Section 1: the headline, paper-comparable sweep.

Topology: width=4 (routers=leaves=spines), hosts/router=2 -> 8 hosts/group,
groups=4 -> 32 nodes total. This is exactly Ma et al.'s (NPC 2021/LNCS 13152,
the g-PAARD paper) canonical illustrative config DF(2,4,2) (p=2 hosts/router,
a=4 routers/group), and also our own empirically-validated resource-safe
topology size (see DF_vs_DF_Plus_testplan.md finding #3 and #8) -- one
config, two reasons to use it.

Message sizes 1K-1M mirror the paper's own ~1KB-1MB log-scale sweep (their
absolute node-count scale tests go up to 1056 nodes on a cycle-accurate
simulator, not reproducible in real-packet Mininet on one VM -- see
test_2_scale_sweep.py for the Mininet-safe stand-in for that axis instead).

Bandwidth is fixed at one value: the paper's simulator works in flits/cycles,
not Mbit/s, so there is no paper value to mirror here -- 100 Mbit/s is this
project's own free choice, stated explicitly rather than left implicit.

repeat=30 and 95% CIs (not bare averages) because every earlier ad-hoc sweep
in this project topped out at repeat<=5 against 2-3x iteration-to-iteration
swings on an identical config -- see DF_vs_DF_Plus_testplan.md finding #5.

Reports, per message size: total time, the intra-group/inter-group split
(only steps 1,3,4,6 can structurally differ between DF and DF+ -- see
sweep_common.classify_step_times), and the per-flow FCT distribution
(mean/p50/p95/max) -- the Flow Completion Time metric required by the
course spec that no earlier script in this project ever collected.

Also runs "gpaard_hierarchical" (optimization idea #2: leaf-local
pre-aggregation, test_8_hierarchical_pattern.py) alongside the flat "gpaard"
pattern at every size, to check whether idea #2's gap-closing result (found
at a single bw=10Mbit/4M condition) generalizes across this sweep's full
size range, or only helps at the large-message end -- see
DF_vs_DF_Plus_testplan.md's "Next steps" section. The intra/inter split only
applies to "gpaard" (classify_step_times only recognizes the flat pattern's
exact 6-step shape); gpaard_hierarchical reports total/FCT/utilization only.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_1_paper_comparable.py
"""

import os
import sys
import time

import matplotlib
matplotlib.use("Agg")  # headless: never try to open a GUI window under sudo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mininet.log import setLogLevel, info
from benchmark_gpaard import format_size, PATTERN_INFO

from sweep_common import (
    run_topology_instance, mean_ci95, format_mean_ci, speedup_and_significance,
    fct_summary, check_hosts_per_group_safety,
    make_utilization_sampler, extract_util_series,
)

WIDTH = 4
HOSTS = 2
GROUPS = 4                 # paper's DF(2,4,2): 4 groups, 32 nodes
BANDWIDTH = 100            # Mbit/s -- this project's own choice, not from the paper
SIZES = [1024, 4096, 16384, 65536, 262144, 1048576]  # 1K .. 1M, mirrors the paper's sweep
REPEAT = 40  # bumped from the first run's 30: that run found a significant intra-only
             # effect at 1M (speedup=0.927); more repeats tightens that CI and gives
             # borderline sizes (256K was 0.983/0.989) a better chance of resolving too
DELAY = "1ms"
GLOBAL_LINKS = 1
SETTLE = 1
TIMEOUT = 60
PLOTS_DIR = "paper_comparable_topology_plots"
OUT_PATH = "results_test1_paper_comparable.txt"

PATTERNS_TO_TEST = ["gpaard", "gpaard_hierarchical"]


def main():
    if os.geteuid() != 0:
        print("test_1_paper_comparable.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 1: paper-comparable sweep (DF(2,4,2)-equivalent topology)")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={GROUPS} width={WIDTH} hosts/router={HOSTS} "
                 f"({WIDTH * HOSTS} hosts/group, 32 nodes total -- matches Ma et al.'s DF(2,4,2)) "
                 f"bw={BANDWIDTH}Mbit (this project's own choice; the paper works in "
                 f"flits/cycles, not Mbit/s) delay={DELAY} global_links={GLOBAL_LINKS}")
    lines.append(f"message sizes: {', '.join(format_size(s) for s in SIZES)} "
                 f"(mirrors the paper's ~1KB-1MB sweep)")
    lines.append(f"patterns: {', '.join(PATTERNS_TO_TEST)}  repeat: {REPEAT}")
    lines.append("")
    lines.append(PATTERN_INFO["gpaard"])
    lines.append("")
    lines.append(PATTERN_INFO["gpaard_hierarchical"])
    lines.append("")

    timings = {}
    # One build per (size, topology) rather than one build per topology
    # covering every size: this file has no secondary sweep axis (unlike
    # test_2's groups or test_3's bandwidth) to alternate measurement order
    # on, so splitting by size creates one. Alternating which topology goes
    # first per size means any monotonic system-level drift over the ~100min
    # run affects both arms roughly equally, instead of dragonfly+ always
    # being measured in one block strictly after dragonfly's (as it was
    # before this fix) -- the adds ~10 extra topology builds (~10-20s each)
    # on top of the existing total runtime, negligible next to repeat=40's
    # iperf time.
    for size_idx, size_bytes in enumerate(SIZES):
        order = ("dragonfly", "dragonfly+") if size_idx % 2 == 0 else ("dragonfly+", "dragonfly")
        for topology in order:
            label = f"paper_comparable_{topology.replace('+', 'plus')}_size{format_size(size_bytes)}"
            run_topology_instance(
                topology, groups=GROUPS, width=WIDTH, hosts=HOSTS, bw=BANDWIDTH,
                delay=DELAY, global_links=GLOBAL_LINKS, patterns=PATTERNS_TO_TEST,
                sizes=[size_bytes], repeat=REPEAT, settle=SETTLE, timeout=TIMEOUT,
                label=label, plots_dir=PLOTS_DIR, lines=lines, timings=timings,
                config_key="paper_comparable",
                utilization_sampler_factory=make_utilization_sampler,
            )

    lines.append("=" * 70)
    lines.append(f"RESULTS: mean [95% CI], n={REPEAT}")
    lines.append("intra-group time (steps 1,3,4,6) is where DF vs DF+ CAN structurally "
                 "differ; inter-group time (steps 2,5) is identical by construction and "
                 "shown only as a sanity check. FCT = per-flow (per-message) completion "
                 "time distribution across the whole pattern.")
    lines.append("=" * 70)

    summary_rows = []
    for size_bytes in SIZES:
        lines.append("-" * 70)
        lines.append(f"size={format_size(size_bytes)}")
        lines.append("-" * 70)
        stats = {}
        for pattern in PATTERNS_TO_TEST:
            for topology in ("dragonfly", "dragonfly+"):
                data = timings[(topology, "paper_comparable", pattern, size_bytes)]
                t_mean, t_hw = mean_ci95(data["total"])
                stats[(pattern, topology)] = (t_mean, t_hw)
                line = f"  {topology:11s} {pattern:19s} total={format_mean_ci(t_mean, t_hw):>22s}"
                if pattern == "gpaard":
                    i_mean, i_hw = mean_ci95(data["intra"])
                    e_mean, e_hw = mean_ci95(data["inter"])
                    line += (f"  intra={format_mean_ci(i_mean, i_hw):>22s}  "
                             f"inter={format_mean_ci(e_mean, e_hw):>22s}")
                lines.append(line)
                fct_mean, fct_p50, fct_p95, fct_max, fct_n = fct_summary(data["fct"])
                lines.append(f"  {'':11s} {'':19s} FCT: mean={fct_mean:.4f}s p50={fct_p50:.4f}s "
                             f"p95={fct_p95:.4f}s max={fct_max:.4f}s (n={fct_n} messages)")

                utils = data["utilization"] or []
                for link_label in ("global_0_1", "local_group0"):
                    series = extract_util_series(utils, link_label, "achieved_mbit")
                    if series:
                        u_mean, u_hw = mean_ci95(series)
                        lines.append(f"  {'':11s} {'':19s} {link_label:13s} achieved="
                                     f"{format_mean_ci(u_mean, u_hw, unit='Mbit/s')}")

        gap_flat, sig_flat = speedup_and_significance(
            *stats[("gpaard", "dragonfly")], *stats[("gpaard", "dragonfly+")])
        gap_hier, sig_hier = speedup_and_significance(
            *stats[("gpaard_hierarchical", "dragonfly")], *stats[("gpaard_hierarchical", "dragonfly+")])
        lines.append(f"  GAP flat  (dragonfly/dragonfly+, gpaard):              "
                     f"{gap_flat:.3f}  significant={sig_flat}")
        lines.append(f"  GAP hier  (dragonfly/dragonfly+, gpaard_hierarchical): "
                     f"{gap_hier:.3f}  significant={sig_hier}")
        summary_rows.append((size_bytes, gap_flat, sig_flat, gap_hier, sig_hier))
        lines.append("")

    lines.append("=" * 70)
    lines.append("SUMMARY (gap = avg dragonfly / avg dragonfly+, >1 = DF+ faster; "
                 "closer to 1.0 = gap closed)")
    lines.append("=" * 70)
    lines.append(f"  {'size':>8s} {'gap_flat':>10s} {'sig':>5s} "
                 f"{'gap_hier':>10s} {'sig':>5s}")
    for size_bytes, gap_flat, sig_flat, gap_hier, sig_hier in summary_rows:
        lines.append(f"  {format_size(size_bytes):>8s} {gap_flat:>10.3f} {str(sig_flat):>5s} "
                     f"{gap_hier:>10.3f} {str(sig_hier):>5s}")
    lines.append("")
    lines.append("Interpretation: if gap_hier is consistently closer to 1.0 than gap_flat "
                 "across every size, idea #2's gap-closing result (found at bw=10Mbit/4M in "
                 "test_8) generalizes across this sweep's full size range. If gap_hier diverges "
                 "from 1.0 at small sizes, the hierarchy's extra steps (10 vs 6, each with fixed "
                 "per-step overhead) are outweighing its cross-leaf message reduction there.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test1] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
