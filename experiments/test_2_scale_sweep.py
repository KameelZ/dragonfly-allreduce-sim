#!/usr/bin/env python3
"""
test_2_scale_sweep.py

DF_vs_DF_Plus_testplan.md, Section 2: scale sweep.

Mininet-safe stand-in for the g-PAARD paper's Fig.7/Fig.9 topology-scale
tests. The paper scales up to 1056 nodes on a cycle-accurate simulator
(Booksim); that is not reproducible in real-packet Mininet on one VM, so
this sweep scales GROUP COUNT (the axis DF/DF+ actually diverge on) while
holding hosts/group fixed at the validated-safe 8 (width=4, hosts/router=2
-- see DF_vs_DF_Plus_testplan.md finding #3), rather than growing
hosts/group, which is what actually caused the earlier near-stall incident.

groups in {4, 6, 8} were already empirically run at this hosts/group in
results_bigtopo_groups_demo.txt without issue, so this re-runs that same
safe range properly (repeat=5->20, with CIs instead of bare averages).

Question this answers: does any DF-vs-DF+ gap grow or shrink with scale, the
way the paper found g-PAARD's advantage over Ring/HD grows with node count?

Also runs "gpaard_hierarchical" (optimization idea #2, test_8_hierarchical_
pattern.py) alongside flat "gpaard" at every (groups, size) cell, to check
whether idea #2's gap-closing result holds as the topology scales up to
groups=6/8, not just at the single groups=4 condition it was found at -- see
DF_vs_DF_Plus_testplan.md's "Next steps" section.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_2_scale_sweep.py
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
GROUPS_VALUES = (4, 6, 8)  # empirically validated not to stall at hosts/group=8
BANDWIDTH = 100  # Mbit/s, same as test_1 for comparability
SIZES = [4096, 65536, 1048576]  # 4K, 64K, 1M -- representative subset of test_1's sweep
REPEAT = 20
DELAY = "1ms"
GLOBAL_LINKS = 1
SETTLE = 1
TIMEOUT = 60
PLOTS_DIR = "scale_topology_plots"
OUT_PATH = "results_test2_scale.txt"

PATTERNS_TO_TEST = ["gpaard", "gpaard_hierarchical"]


def main():
    if os.geteuid() != 0:
        print("test_2_scale_sweep.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 2: scale sweep (groups, hosts/group fixed at the safe 8)")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups swept: {list(GROUPS_VALUES)}  width={WIDTH} hosts/router={HOSTS} "
                 f"({WIDTH * HOSTS} hosts/group, fixed) bw={BANDWIDTH}Mbit delay={DELAY} "
                 f"global_links={GLOBAL_LINKS}")
    lines.append(f"message sizes: {', '.join(format_size(s) for s in SIZES)}")
    lines.append(f"patterns: {', '.join(PATTERNS_TO_TEST)}  repeat: {REPEAT}")
    lines.append("")
    lines.append(PATTERN_INFO["gpaard"])
    lines.append("")
    lines.append(PATTERN_INFO["gpaard_hierarchical"])
    lines.append("")
    lines.append("Mininet-safe stand-in for the paper's node-count scale test: groups grow "
                 "(and with them, switch/link count) while hosts/group stays fixed at the "
                 "validated-safe 8, instead of growing hosts/group (which is what caused "
                 "the earlier near-stall incident at 24 hosts/group).")
    lines.append("")

    timings = {}
    for groups_idx, groups in enumerate(GROUPS_VALUES):
        # Alternate which topology is measured first across groups values (as
        # in test_3_mechanism_isolating.py) so any monotonic system-level
        # drift over the run affects both arms roughly equally, rather than
        # always penalizing dragonfly+ (which was always measured second
        # here before this fix).
        order = ("dragonfly", "dragonfly+") if groups_idx % 2 == 0 else ("dragonfly+", "dragonfly")
        for topology in order:
            label = f"groups{groups}_{topology.replace('+', 'plus')}"
            run_topology_instance(
                topology, groups=groups, width=WIDTH, hosts=HOSTS, bw=BANDWIDTH,
                delay=DELAY, global_links=GLOBAL_LINKS, patterns=PATTERNS_TO_TEST,
                sizes=SIZES, repeat=REPEAT, settle=SETTLE, timeout=TIMEOUT,
                label=label, plots_dir=PLOTS_DIR, lines=lines, timings=timings,
                config_key=("groups", groups),
                utilization_sampler_factory=make_utilization_sampler,
            )

    lines.append("=" * 70)
    lines.append(f"RESULTS: mean [95% CI], n={REPEAT}")
    lines.append("=" * 70)

    summary_rows = []
    for groups in GROUPS_VALUES:
        lines.append("-" * 70)
        lines.append(f"groups={groups}")
        lines.append("-" * 70)
        for size_bytes in SIZES:
            stats = {}
            for pattern in PATTERNS_TO_TEST:
                for topology in ("dragonfly", "dragonfly+"):
                    data = timings[(topology, ("groups", groups), pattern, size_bytes)]
                    t_mean, t_hw = mean_ci95(data["total"])
                    stats[(pattern, topology)] = (t_mean, t_hw)
                    line = (f"  {topology:11s} {pattern:19s} {format_size(size_bytes):>6s}  "
                            f"total={format_mean_ci(t_mean, t_hw):>22s}")
                    if pattern == "gpaard":
                        i_mean, i_hw = mean_ci95(data["intra"])
                        line += f"  intra={format_mean_ci(i_mean, i_hw):>22s}"
                    lines.append(line)
                    fct_mean, fct_p50, fct_p95, fct_max, fct_n = fct_summary(data["fct"])
                    lines.append(f"  {'':11s} {'':19s} {'':6s}  FCT: mean={fct_mean:.4f}s "
                                 f"p50={fct_p50:.4f}s p95={fct_p95:.4f}s max={fct_max:.4f}s "
                                 f"(n={fct_n} messages)")

                    utils = data["utilization"] or []
                    for link_label in ("global_0_1", "local_group0"):
                        series = extract_util_series(utils, link_label, "achieved_mbit")
                        if series:
                            u_mean, u_hw = mean_ci95(series)
                            lines.append(f"  {'':11s} {'':19s} {'':6s}  {link_label:13s} "
                                         f"achieved={format_mean_ci(u_mean, u_hw, unit='Mbit/s')}")

            gap_flat, sig_flat = speedup_and_significance(
                *stats[("gpaard", "dragonfly")], *stats[("gpaard", "dragonfly+")])
            gap_hier, sig_hier = speedup_and_significance(
                *stats[("gpaard_hierarchical", "dragonfly")], *stats[("gpaard_hierarchical", "dragonfly+")])
            lines.append(f"    GAP flat (gpaard):              {gap_flat:.3f}  significant={sig_flat}")
            lines.append(f"    GAP hier (gpaard_hierarchical): {gap_hier:.3f}  significant={sig_hier}")
            summary_rows.append((groups, size_bytes, gap_flat, sig_flat, gap_hier, sig_hier))
        lines.append("")

    lines.append("=" * 70)
    lines.append("SUMMARY MATRIX (gap = avg dragonfly / avg dragonfly+, >1 = DF+ faster; "
                 "closer to 1.0 = gap closed)")
    lines.append("rows = groups x size, columns = flat gpaard vs gpaard_hierarchical")
    lines.append("=" * 70)
    lines.append(f"  {'groups':>8s} {'size':>8s} {'gap_flat':>10s} {'sig':>5s} "
                 f"{'gap_hier':>10s} {'sig':>5s}")
    for groups, size_bytes, gap_flat, sig_flat, gap_hier, sig_hier in summary_rows:
        lines.append(f"  {groups:>8d} {format_size(size_bytes):>8s} {gap_flat:>10.3f} "
                     f"{str(sig_flat):>5s} {gap_hier:>10.3f} {str(sig_hier):>5s}")
    lines.append("")
    lines.append("Interpretation: if gap_hier stays consistently closer to 1.0 than gap_flat as "
                 "groups scale up to 8, idea #2's gap-closing result holds at scale, not just at "
                 "the single groups=4 condition it was found at in test_8.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test2] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
