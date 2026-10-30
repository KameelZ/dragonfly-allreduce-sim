#!/usr/bin/env python3
"""
test_8_hierarchical_pattern.py

Optimization idea #2 from DF_vs_DF_Plus_testplan.md's proposal section:
leaf-local pre-aggregation. Benchmarks the new "gpaard_hierarchical" pattern
(gpaard.py's build_gpaard_hierarchical_schedule, registered in
benchmark_gpaard.PATTERNS) against the original "gpaard" pattern, on BOTH
dragonfly and dragonfly+, at the same bw=10Mbit/4M condition
test_7_spine_oversubscription.py used -- the condition with the largest
measured DF+ disadvantage (0.769-0.776 in test_3, replicated at 0.767-0.771
across every spine ratio in test_7).

Unlike idea #1 (spine oversubscription, which test_7 showed does literally
nothing -- this project's routing has no ECMP, so extra path capacity is
never used), idea #2 targets message VOLUME directly: the flat intra-group
any-to-all in g-PAARD's steps 3/4 is replaced by a 2-level hierarchy
(combine within each leaf/router, exchange among one representative per
leaf, broadcast back) that cuts cross-leaf message count 4x at this
project's standard config (224 -> 96 total messages, 192 -> 48 of the
cross-leaf/expensive kind in DF+) -- verified by test_gpaard.py's
test_hierarchical_schedule_reduces_cross_leaf_messages, and correctness
-verified across 40 topology configs (curated + randomized, both
topologies) before this file was written.

Applied to BOTH topologies identically (not just DF+) -- anything else
would be comparing an optimized DF+ against an unoptimized DF, which
wouldn't isolate whether the optimization helps DF+ specifically or just
makes everything faster. What matters is whether the GAP (not the absolute
times) narrows.

4 variants from 2 topology builds (gpaard + gpaard_hierarchical run
together within each topology's build, reusing run_topology_instance's
existing multi-pattern support):
  - dragonfly,  gpaard               (baseline)
  - dragonfly,  gpaard_hierarchical  (does the optimization help DF too?)
  - dragonfly+, gpaard               (baseline, = test_3/test_7's 1x-spines number)
  - dragonfly+, gpaard_hierarchical  (the actual target: does this close the gap?)

KNOWN LIMITATION: same as test_7 -- only 2 topology builds at a single
bw/size point, no secondary sweep axis to alternate measurement order
across. Fine for a triage signal; a follow-up claim for a final report
should use a broader, alternated grid.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_8_hierarchical_pattern.py
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
GROUPS = 4
BANDWIDTH = 10            # Mbit/s -- same condition as test_7, largest known DF+ disadvantage
SIZE_BYTES = 4194304      # 4M -- same reason
GLOBAL_LINKS = 1
REPEAT = 30
DELAY = "1ms"
SETTLE = 1
TIMEOUT = 60
PLOTS_DIR = "hierarchical_pattern_plots"
OUT_PATH = "results_test8_hierarchical_pattern.txt"

PATTERNS_TO_TEST = ["gpaard", "gpaard_hierarchical"]
CONFIG_KEY = "hierarchical_compare"  # constant is safe here: (topology, pattern) already
                                     # differs across all 4 cells -- verified no collision
                                     # before writing the report logic (see module history).


def main():
    if os.geteuid() != 0:
        print("test_8_hierarchical_pattern.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 8 (optimization #2): leaf-local pre-aggregation (gpaard_hierarchical)")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={GROUPS} width={WIDTH} hosts/router={HOSTS} ({WIDTH * HOSTS} hosts/group) "
                 f"bw={BANDWIDTH}Mbit size={format_size(SIZE_BYTES)} delay={DELAY} "
                 f"global_links={GLOBAL_LINKS}  repeat={REPEAT}")
    lines.append("")
    lines.append("Does replacing g-PAARD's flat O(hosts^2) intra-group any-to-all with a "
                 "leaf-local-combine + cross-leaf-exchange + broadcast-back hierarchy narrow "
                 "the gap test_3/test_7 found at this exact bw/size point? Applied identically "
                 "to both topologies -- what matters is whether the GAP narrows, not just "
                 "whether either topology gets faster on its own.")
    lines.append("")
    lines.append(PATTERN_INFO["gpaard"])
    lines.append("")
    lines.append(PATTERN_INFO["gpaard_hierarchical"])
    lines.append("")

    timings = {}
    for topology in ("dragonfly", "dragonfly+"):
        label = f"hierarchical_compare_{topology.replace('+', 'plus')}"
        run_topology_instance(
            topology, groups=GROUPS, width=WIDTH, hosts=HOSTS, bw=BANDWIDTH,
            delay=DELAY, global_links=GLOBAL_LINKS, patterns=PATTERNS_TO_TEST,
            sizes=[SIZE_BYTES], repeat=REPEAT, settle=SETTLE, timeout=TIMEOUT,
            label=label, plots_dir=PLOTS_DIR, lines=lines, timings=timings,
            config_key=CONFIG_KEY,
            utilization_sampler_factory=make_utilization_sampler,
        )

    lines.append("=" * 70)
    lines.append(f"RESULTS: mean [95% CI], n={REPEAT}")
    lines.append("=" * 70)

    stats = {}
    for topology in ("dragonfly", "dragonfly+"):
        for pattern in PATTERNS_TO_TEST:
            data = timings[(topology, CONFIG_KEY, pattern, SIZE_BYTES)]
            t_mean, t_hw = mean_ci95(data["total"])
            stats[(topology, pattern)] = (t_mean, t_hw)
            lines.append(f"  {topology:11s} {pattern:19s} total={format_mean_ci(t_mean, t_hw):>22s}")
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
    lines.append("")

    lines.append("-" * 70)
    lines.append("COMPARISONS (speedup = avg A / avg B, >1 = A faster)")
    lines.append("-" * 70)

    sp, sig = speedup_and_significance(*stats[("dragonfly", "gpaard")], *stats[("dragonfly", "gpaard_hierarchical")])
    lines.append(f"  dragonfly: gpaard vs gpaard_hierarchical: {sp:.3f}  significant={sig}   "
                 "(does the optimization help DF, which has no hop-cost asymmetry to exploit?)")

    sp, sig = speedup_and_significance(*stats[("dragonfly+", "gpaard")], *stats[("dragonfly+", "gpaard_hierarchical")])
    lines.append(f"  dragonfly+: gpaard vs gpaard_hierarchical: {sp:.3f}  significant={sig}   "
                 "(does the optimization help DF+ -- the actual target)")
    lines.append("")

    gap_before, sig_before = speedup_and_significance(
        *stats[("dragonfly", "gpaard")], *stats[("dragonfly+", "gpaard")])
    gap_after, sig_after = speedup_and_significance(
        *stats[("dragonfly", "gpaard_hierarchical")], *stats[("dragonfly+", "gpaard_hierarchical")])
    lines.append(f"  GAP before (dragonfly/dragonfly+, both on flat gpaard):          "
                 f"{gap_before:.3f}  significant={sig_before}   (compare to test_3/test_7's 0.769-0.776)")
    lines.append(f"  GAP after  (dragonfly/dragonfly+, both on gpaard_hierarchical):  "
                 f"{gap_after:.3f}  significant={sig_after}")
    lines.append("")
    lines.append("Interpretation guide: if GAP after is closer to 1.0 than GAP before (and "
                 "dragonfly+'s own before/after comparison shows a significant speedup), the "
                 "optimization is working as intended -- cutting cross-leaf message volume "
                 "narrows the structural disadvantage test_6 measured precisely. If GAP after "
                 "is about the same as GAP before, the bottleneck is elsewhere (e.g. the fixed "
                 "propagation delay itself, independent of message count).")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test8] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
