#!/usr/bin/env python3
"""
test_0_sanity_identical.py

DF_vs_DF_Plus_testplan.md, Section 0: measurement-floor sanity check.

At groups<=3, DragonflyTopo and DragonflyPlusTopo are STRUCTURALLY IDENTICAL
(they only diverge at >=4 groups, in the inter-group global-exchange steps --
see gpaard.py / dragonfly_plus_topo.py). So any "speedup" measured here is
not a real topology effect -- it is pure measurement noise/bias from the
pipeline itself (iperf startup variance, scheduling jitter, etc).

Purpose: calibrate how much spread to expect from the measurement pipeline
BEFORE trusting any >=4-groups result in test_1 / test_2 / test_3 / test_4.
If this file's speedup numbers are NOT close to 1.0 with a CI that contains
1.0, something in the pipeline itself is biased and that must be fixed
before trusting anything else in the test plan.

Cheap by design: 2 configs x 1 bandwidth x 1 size x repeat=10 x 2 topologies.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_0_sanity_identical.py
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
    check_hosts_per_group_safety,
)

WIDTH = 4
HOSTS = 2
GROUPS_VALUES = (2, 3)  # DF == DF+ structurally at these group counts
BANDWIDTH = 100  # Mbit/s
SIZE_BYTES = 65536  # 64K
REPEAT = 10
DELAY = "1ms"
GLOBAL_LINKS = 1
SETTLE = 1
TIMEOUT = 60
PLOTS_DIR = "sanity_topology_plots"
OUT_PATH = "results_test0_sanity.txt"


def main():
    if os.geteuid() != 0:
        print("test_0_sanity_identical.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 0: measurement-floor sanity check")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={list(GROUPS_VALUES)} width={WIDTH} hosts/router={HOSTS} "
                 f"({WIDTH * HOSTS} hosts/group) bw={BANDWIDTH}Mbit delay={DELAY} "
                 f"global_links={GLOBAL_LINKS}")
    lines.append(f"message size: {format_size(SIZE_BYTES)}  repeat: {REPEAT}")
    lines.append("")
    lines.append("DF and DF+ are STRUCTURALLY IDENTICAL at <=3 groups -- any speedup != 1.0 "
                 "measured below is pure measurement pipeline noise/bias, not a real "
                 "topology effect. This calibrates the pipeline's own noise floor.")
    lines.append("")

    timings = {}
    for groups in GROUPS_VALUES:
        for topology in ("dragonfly", "dragonfly+"):
            label = f"groups{groups}_{topology.replace('+', 'plus')}"
            run_topology_instance(
                topology, groups=groups, width=WIDTH, hosts=HOSTS, bw=BANDWIDTH,
                delay=DELAY, global_links=GLOBAL_LINKS, patterns=["gpaard"],
                sizes=[SIZE_BYTES], repeat=REPEAT, settle=SETTLE, timeout=TIMEOUT,
                label=label, plots_dir=PLOTS_DIR, lines=lines, timings=timings,
                config_key=("groups", groups),
            )

    lines.append("=" * 70)
    lines.append(f"RESULTS: mean [95% CI], n={REPEAT}. Expect speedup close to 1.0 with a CI "
                 "that contains 1.0, and 'significant=False' everywhere.")
    lines.append("=" * 70)
    for groups in GROUPS_VALUES:
        lines.append("-" * 70)
        lines.append(f"groups={groups} (structurally identical DF/DF+)")
        lines.append("-" * 70)
        stats = {}
        for topology in ("dragonfly", "dragonfly+"):
            data = timings[(topology, ("groups", groups), "gpaard", SIZE_BYTES)]
            mean, hw = mean_ci95(data["total"])
            stats[topology] = (mean, hw)
            lines.append(f"  {topology:11s} total={format_mean_ci(mean, hw)}")
        speedup, significant = speedup_and_significance(*stats["dragonfly"], *stats["dragonfly+"])
        lines.append(f"  speedup (avg dragonfly / avg dragonfly+): {speedup:.3f}  "
                     f"significant={significant}  "
                     f"{'<-- WARNING: pipeline bias suspected' if significant else '(expected)'}")
        lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test0] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
