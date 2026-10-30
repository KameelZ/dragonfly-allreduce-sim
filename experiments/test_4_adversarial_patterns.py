#!/usr/bin/env python3
"""
test_4_adversarial_patterns.py

DF_vs_DF_Plus_testplan.md, Section 4: adversarial-pattern sweep.

g-PAARD's own schedule always collects to a LOCAL designated host before
crossing a global link (steps 1/6), so it mostly avoids forcing traffic
through a non-co-located switch. `shift` and `nearest_neighbor` (already
implemented in benchmark_gpaard.py, committed history ran them once at
--repeat 3 in results_adversarial_*.txt, since deleted from the working
tree) do the opposite: they force traffic from hosts that are NOT already
sitting on the global-link-carrying switch, which costs DF+ extra
leaf-spine hops that g-PAARD's own schedule mostly sidesteps. The n=3 data
hinted this might show a larger, and sometimes opposite-direction, effect
than g-PAARD's own near-1.0 ratios -- worth confirming properly instead of
leaving it at n=3.

`hotspot`/`uniform`/`all_to_all` are deliberately NOT included here: prior
data showed near-zero DF/DF+ difference for those, which makes structural
sense (hotspot is an endpoint bottleneck independent of fabric topology;
uniform/all_to_all spread load symmetrically and don't isolate hop-count).

Note: intra/inter-group step classification (sweep_common.classify_step_times)
only applies to the 6-step 'gpaard' schedule -- these patterns don't have
that step structure, so only 'total' time and the per-flow FCT distribution
are reported here.

Same safe topology as test_3: groups=4, width=4, hosts/router=2 (8 hosts/group).

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_4_adversarial_patterns.py
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
BANDWIDTH = 100  # Mbit/s, same as test_1/test_2 for comparability
PATTERNS_TO_TEST = ["shift", "nearest_neighbor"]
SIZES = [4096, 65536, 1048576]  # 4K, 64K, 1M
REPEAT = 30  # bumped from the first run's 20 for consistency with test_1's tightened CIs
DELAY = "1ms"
GLOBAL_LINKS = 1
SETTLE = 1
TIMEOUT = 60
PLOTS_DIR = "adversarial_topology_plots_v2"
OUT_PATH = "results_test4_adversarial.txt"


def main():
    if os.geteuid() != 0:
        print("test_4_adversarial_patterns.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 4: adversarial-pattern sweep (shift, nearest_neighbor)")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={GROUPS} width={WIDTH} hosts/router={HOSTS} ({WIDTH * HOSTS} hosts/group) "
                 f"bw={BANDWIDTH}Mbit delay={DELAY} global_links={GLOBAL_LINKS}")
    lines.append(f"message sizes: {', '.join(format_size(s) for s in SIZES)}")
    lines.append(f"patterns: {', '.join(PATTERNS_TO_TEST)}  repeat: {REPEAT}")
    lines.append("")
    for p in PATTERNS_TO_TEST:
        lines.append(f"[{p}] {PATTERN_INFO[p]}")
        lines.append("")

    timings = {}
    # One build per (size, topology), both patterns run together within
    # each size's build. Same rationale as test_1_paper_comparable.py: no
    # secondary sweep axis existed to alternate measurement order on, so
    # splitting by size creates one, fixing the "dragonfly+ always measured
    # second" confound this section had before.
    for size_idx, size_bytes in enumerate(SIZES):
        order = ("dragonfly", "dragonfly+") if size_idx % 2 == 0 else ("dragonfly+", "dragonfly")
        for topology in order:
            label = f"adversarial_{topology.replace('+', 'plus')}_size{format_size(size_bytes)}"
            run_topology_instance(
                topology, groups=GROUPS, width=WIDTH, hosts=HOSTS, bw=BANDWIDTH,
                delay=DELAY, global_links=GLOBAL_LINKS, patterns=PATTERNS_TO_TEST,
                sizes=[size_bytes], repeat=REPEAT, settle=SETTLE, timeout=TIMEOUT,
                label=label, plots_dir=PLOTS_DIR, lines=lines, timings=timings,
                config_key="adversarial",
                utilization_sampler_factory=make_utilization_sampler,
            )

    lines.append("=" * 70)
    lines.append(f"RESULTS: mean [95% CI], n={REPEAT}. Only 'total' and FCT are meaningful "
                 "here (no intra/inter split -- these patterns don't have g-PAARD's 6-step "
                 "structure).")
    lines.append("=" * 70)

    summary_rows = []
    for pattern_name in PATTERNS_TO_TEST:
        lines.append("-" * 70)
        lines.append(f"PATTERN: {pattern_name}")
        lines.append("-" * 70)
        for size_bytes in SIZES:
            stats = {}
            for topology in ("dragonfly", "dragonfly+"):
                data = timings[(topology, "adversarial", pattern_name, size_bytes)]
                t_mean, t_hw = mean_ci95(data["total"])
                stats[topology] = (t_mean, t_hw)
                fct_mean, fct_p50, fct_p95, fct_max, fct_n = fct_summary(data["fct"])
                lines.append(f"  {topology:11s} {format_size(size_bytes):>6s}  "
                             f"total={format_mean_ci(t_mean, t_hw):>22s}  "
                             f"FCT mean={fct_mean:.4f}s p95={fct_p95:.4f}s (n={fct_n})")

                utils = data["utilization"] or []
                for link_label in ("global_0_1", "local_group0"):
                    series = extract_util_series(utils, link_label, "achieved_mbit")
                    if series:
                        u_mean, u_hw = mean_ci95(series)
                        lines.append(f"  {'':11s} {'':6s}  {link_label:13s} achieved="
                                     f"{format_mean_ci(u_mean, u_hw, unit='Mbit/s')}")
            speedup, sig = speedup_and_significance(*stats["dragonfly"], *stats["dragonfly+"])
            lines.append(f"    speedup (avg dragonfly / avg dragonfly+): {speedup:.3f}  "
                         f"significant={sig}"
                         + ("   <-- DF+ LOSES here" if (sig and speedup < 1) else ""))
            summary_rows.append((pattern_name, size_bytes, speedup, sig))
        lines.append("")

    lines.append("=" * 70)
    lines.append("SUMMARY (speedup = avg dragonfly / avg dragonfly+, >1 = DF+ faster, "
                 "<1 = DF+ loses)")
    lines.append("=" * 70)
    lines.append(f"  {'pattern':>18s} {'size':>6s} {'speedup':>10s} {'sig':>5s}")
    for pattern_name, size_bytes, speedup, sig in summary_rows:
        lines.append(f"  {pattern_name:>18s} {format_size(size_bytes):>6s} "
                     f"{speedup:>10.3f} {str(sig):>5s}")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test4] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
