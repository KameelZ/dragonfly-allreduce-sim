#!/usr/bin/env python3
"""
test_10_adaptive_calibration.py

DF_vs_DF_Plus_testplan.md: calibration run for the adaptive pattern
selector (benchmark_gpaard.choose_gpaard_pattern_name).

test_9_adaptive_selector.py's retrospective check found the selector's
pick matches the empirically faster schedule at all 32 conditions this
project already had live Mininet data for -- but every one of those
conditions sits safely outside the gap between the two known boundary
points (bw=100Mbit/1M -> transfer_time=0.0839s, a loss for
gpaard_hierarchical; bw=10Mbit/4M -> transfer_time=3.3554s, a win).
ADAPTIVE_TRANSFER_TIME_THRESHOLD_S=0.53 (the geometric mean) is a reasoned
placement INSIDE that gap, not a calibrated one -- no real data exists
there yet.

This run gets real data INSIDE that gap: same topology/bandwidth as
test_1 (groups=4, width=4, hosts=2, bw=100Mbit -- the already-validated
safe config), but sizes pushed past test_1's 1M ceiling specifically to
cross the predicted threshold (at bw=100Mbit, transfer_time_s=0.53 happens
at size=0.53*100e6/8 = ~6.3M bytes). Capped at 8M (not pushed further) to
keep message sizes within a range already comfortable for this project's
validated topology/iperf setup:

  2M -> transfer_time=0.168s  (predicted: below threshold, flat wins)
  4M -> transfer_time=0.336s  (predicted: below threshold, flat wins)
  8M -> transfer_time=0.671s  (predicted: above threshold, hierarchical wins)

If the empirical winner actually flips between 4M and 8M at this
bandwidth, that's real evidence the threshold is in the right place (not
just "unconstrained by any known data" as it was before this run). If it
flips somewhere else (or doesn't flip at all within this range), this run
is exactly what's needed to move the threshold to match real data instead
of a reasoned-but-unverified guess.

repeat=20 (lower than test_1's 40): this is a calibration/secondary check,
not the primary statistical claim the project rests on (that's test_1/
test_2/test_8's n=30-40 data) -- bounded wall-clock time matters more here
than the tightest possible CI.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_10_adaptive_calibration.py
"""

import os
import sys
import time

import matplotlib
matplotlib.use("Agg")  # headless: never try to open a GUI window under sudo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mininet.log import setLogLevel, info
from benchmark_gpaard import format_size, PATTERN_INFO, choose_gpaard_pattern_name, ADAPTIVE_TRANSFER_TIME_THRESHOLD_S

from sweep_common import (
    run_topology_instance, mean_ci95, format_mean_ci, speedup_and_significance,
    fct_summary, check_hosts_per_group_safety,
    make_utilization_sampler, extract_util_series,
)

WIDTH = 4
HOSTS = 2
GROUPS = 4
BANDWIDTH = 100            # Mbit/s -- same as test_1, so only size is new
SIZES = [2097152, 4194304, 8388608]  # 2M, 4M, 8M -- straddles the
                                      # predicted ~6.3M crossover at bw=100Mbit
REPEAT = 20
DELAY = "1ms"
GLOBAL_LINKS = 1
SETTLE = 1
TIMEOUT = 120  # bumped from test_1's 60: these sizes are up to 8x test_1's largest
PLOTS_DIR = "adaptive_calibration_topology_plots"
OUT_PATH = "results_test10_adaptive_calibration.txt"

PATTERNS_TO_TEST = ["gpaard", "gpaard_hierarchical"]


def main():
    if os.geteuid() != 0:
        print("test_10_adaptive_calibration.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 10: adaptive pattern selector -- calibration run (new sizes, bw=100Mbit)")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={GROUPS} width={WIDTH} hosts/router={HOSTS} ({WIDTH * HOSTS} hosts/group) "
                 f"bw={BANDWIDTH}Mbit delay={DELAY} global_links={GLOBAL_LINKS}")
    lines.append(f"message sizes: {', '.join(format_size(s) for s in SIZES)} -- chosen to straddle "
                 f"the predicted ~6.3M crossover at bw={BANDWIDTH}Mbit "
                 f"(threshold_s={ADAPTIVE_TRANSFER_TIME_THRESHOLD_S})")
    lines.append(f"patterns: {', '.join(PATTERNS_TO_TEST)}  repeat: {REPEAT}")
    lines.append("")
    lines.append(PATTERN_INFO["gpaard"])
    lines.append("")
    lines.append(PATTERN_INFO["gpaard_hierarchical"])
    lines.append("")
    lines.append("Purpose: test_9_adaptive_selector.py's retrospective check found the "
                 "selector's pick matches the empirically faster schedule at all 32 conditions "
                 "already measured elsewhere, but none of those fell inside the uncalibrated gap "
                 "between the known loss (bw=100Mbit/1M, 0.0839s) and the known win (bw=10Mbit/4M, "
                 "3.3554s). This run gets real data INSIDE that gap, to confirm or correct the "
                 "threshold's placement rather than leave it as an unverified reasoned guess.")
    lines.append("")

    timings = {}
    for size_idx, size_bytes in enumerate(SIZES):
        order = ("dragonfly", "dragonfly+") if size_idx % 2 == 0 else ("dragonfly+", "dragonfly")
        for topology in order:
            label = f"adaptive_calibration_{topology.replace('+', 'plus')}_size{format_size(size_bytes)}"
            run_topology_instance(
                topology, groups=GROUPS, width=WIDTH, hosts=HOSTS, bw=BANDWIDTH,
                delay=DELAY, global_links=GLOBAL_LINKS, patterns=PATTERNS_TO_TEST,
                sizes=[size_bytes], repeat=REPEAT, settle=SETTLE, timeout=TIMEOUT,
                label=label, plots_dir=PLOTS_DIR, lines=lines, timings=timings,
                config_key="adaptive_calibration",
                utilization_sampler_factory=make_utilization_sampler,
            )

    lines.append("=" * 70)
    lines.append(f"RESULTS: mean [95% CI], n={REPEAT}")
    lines.append("=" * 70)

    summary_rows = []
    for size_bytes in SIZES:
        lines.append("-" * 70)
        lines.append(f"size={format_size(size_bytes)}  "
                     f"transfer_time_s={(size_bytes * 8) / (BANDWIDTH * 1e6):.4f}")
        lines.append("-" * 70)
        stats = {}
        for pattern in PATTERNS_TO_TEST:
            for topology in ("dragonfly", "dragonfly+"):
                data = timings[(topology, "adaptive_calibration", pattern, size_bytes)]
                t_mean, t_hw = mean_ci95(data["total"])
                stats[(pattern, topology)] = (t_mean, t_hw)
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

        predicted = choose_gpaard_pattern_name(size_bytes, BANDWIDTH)
        for topology in ("dragonfly", "dragonfly+"):
            flat_s = stats[("gpaard", topology)][0]
            hier_s = stats[("gpaard_hierarchical", topology)][0]
            empirically_faster = "gpaard" if flat_s < hier_s else "gpaard_hierarchical"
            matched = predicted == empirically_faster
            lines.append(f"    {topology:11s} predicted={predicted:19s} "
                         f"empirically_faster={empirically_faster:19s} "
                         f"match={'YES' if matched else 'NO'}")
            summary_rows.append((size_bytes, topology, flat_s, hier_s, predicted, empirically_faster, matched))

        gap_flat, sig_flat = speedup_and_significance(
            *stats[("gpaard", "dragonfly")], *stats[("gpaard", "dragonfly+")])
        gap_hier, sig_hier = speedup_and_significance(
            *stats[("gpaard_hierarchical", "dragonfly")], *stats[("gpaard_hierarchical", "dragonfly+")])
        lines.append(f"  GAP flat  (dragonfly/dragonfly+, gpaard):              "
                     f"{gap_flat:.3f}  significant={sig_flat}")
        lines.append(f"  GAP hier  (dragonfly/dragonfly+, gpaard_hierarchical): "
                     f"{gap_hier:.3f}  significant={sig_hier}")
        lines.append("")

    lines.append("=" * 70)
    lines.append("SUMMARY: does the selector's prediction match reality inside the "
                 "previously-uncalibrated gap?")
    lines.append("=" * 70)
    lines.append(f"  {'size':>6s} {'topology':11s} {'flat_s':>9s} {'hier_s':>9s} "
                 f"{'predicted':>19s} {'actual_faster':>19s} {'match':>6s}")
    mismatches = 0
    for size_bytes, topology, flat_s, hier_s, predicted, actual, matched in summary_rows:
        if not matched:
            mismatches += 1
        lines.append(f"  {format_size(size_bytes):>6s} {topology:11s} {flat_s:>9.3f} {hier_s:>9.3f} "
                     f"{predicted:>19s} {actual:>19s} {'YES' if matched else 'NO':>6s}")
    lines.append("")
    lines.append(f"{len(summary_rows) - mismatches}/{len(summary_rows)} predictions matched.")
    if mismatches == 0:
        lines.append("Threshold placement confirmed by real data inside the previously-"
                     "uncalibrated gap -- no change needed.")
    else:
        lines.append(f"{mismatches} mismatch(es) above: the threshold should move. Find the "
                     "actual crossover size from where flat_s/hier_s cross over in the table "
                     "above, recompute transfer_time_s at that point, and update "
                     "benchmark_gpaard.ADAPTIVE_TRANSFER_TIME_THRESHOLD_S accordingly.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test10] results written to {OUT_PATH}\n")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
