#!/usr/bin/env python3
"""
test_12_hierarchical_plus_multipath.py

Optimization idea #6: idea #2 (hierarchical reduce schedule) combined with
idea #5 (ECMP multipath across spines). Same 4-variant spine-ratio design as
test_11_multipath_spines.py, same worst-case bw=10Mbit/4M condition, but
using gpaard_hierarchical instead of flat gpaard.

Why this combination specifically: idea #2 alone (test_8) already closes
the DF-vs-DF+ gap to parity (0.767 -> 1.005, not significant) by cutting
cross-leaf message VOLUME (O(hosts^2) -> O(leaves^2) + O(hosts)). Idea #5
alone (test_11) attacks the remaining volume's PATH diversity -- spreading
it across spines instead of funneling it through one. Neither change
touches the other's mechanism, so there's no reason to expect them to
conflict, and a real chance they compound: less traffic, spread across more
paths, is the closest this project can get to removing DF+'s contention
disadvantage entirely.

This is the most promising remaining path to an outright DF+ win (not just
parity) attempted in this project: idea #5 alone was predicted NOT to fully
overtake DF, because test_6's fixed extra-hop latency cost is structural,
not congestion-driven, and multipath alone can't remove it. Idea #2 reduces
how many messages pay that fixed cost; idea #5 ensures the ones that remain
don't queue behind each other. If DF+ is going to beat DF anywhere in this
project, this combined condition is where it would show up first.

No dependency on test_11's result -- same underlying routing fix, different
message pattern, can run in either order or in parallel sections of the
same overnight pass.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_12_hierarchical_plus_multipath.py
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
BANDWIDTH = 10            # Mbit/s -- same worst-case condition as test_7/test_8/test_11
SIZE_BYTES = 4194304      # 4M -- same reason
GLOBAL_LINKS = 1
REPEAT = 30
DELAY = "1ms"
SETTLE = 1
TIMEOUT = 60
PLOTS_DIR = "hierarchical_plus_multipath_topology_plots"
OUT_PATH = "results_test12_hierarchical_plus_multipath.txt"

# Same 4-variant dose-response design as test_7/test_11, pattern swapped to
# gpaard_hierarchical -- the only difference from test_11.
VARIANTS = [
    ("dragonfly", "dragonfly", None),
    ("dragonfly+_1x_spines", "dragonfly+", WIDTH),
    ("dragonfly+_2x_spines", "dragonfly+", WIDTH * 2),
    ("dragonfly+_4x_spines", "dragonfly+", WIDTH * 4),
]


def main():
    if os.geteuid() != 0:
        print("test_12_hierarchical_plus_multipath.py must run as root (Mininet needs to "
              "manipulate network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 12 (optimization idea #6): hierarchical schedule + multipath spines")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={GROUPS} width={WIDTH} hosts/router={HOSTS} ({WIDTH * HOSTS} hosts/group) "
                 f"bw={BANDWIDTH}Mbit size={format_size(SIZE_BYTES)} delay={DELAY} "
                 f"global_links={GLOBAL_LINKS}  repeat={REPEAT}")
    lines.append("")
    lines.append("Idea #2 (hierarchical schedule, test_8) already closes the gap to parity by "
                 "cutting cross-leaf message volume. Idea #5 (multipath, test_11) spreads "
                 "whatever volume remains across spines instead of funneling it through one. "
                 "This tests whether combining both -- less traffic, spread across more paths "
                 "-- is enough to push DF+ past parity into an outright win, which neither "
                 "change alone was expected to achieve on its own (idea #5 alone cannot remove "
                 "test_6's fixed extra-hop latency cost; idea #2 alone still funnels its "
                 "reduced traffic through a single spine without this fix).")
    lines.append("")
    lines.append(PATTERN_INFO["gpaard_hierarchical"])
    lines.append("")

    timings = {}
    for variant_label, topology, spines in VARIANTS:
        run_topology_instance(
            topology, groups=GROUPS, width=WIDTH, hosts=HOSTS, bw=BANDWIDTH,
            delay=DELAY, global_links=GLOBAL_LINKS, patterns=["gpaard_hierarchical"],
            sizes=[SIZE_BYTES], repeat=REPEAT, settle=SETTLE, timeout=TIMEOUT,
            label=variant_label, plots_dir=PLOTS_DIR, lines=lines, timings=timings,
            config_key=variant_label, spines=spines,
            utilization_sampler_factory=make_utilization_sampler,
        )

    lines.append("=" * 70)
    lines.append(f"RESULTS: mean [95% CI], n={REPEAT}")
    lines.append("=" * 70)

    stats = {}
    for variant_label, topology, _spines in VARIANTS:
        data = timings[(topology, variant_label, "gpaard_hierarchical", SIZE_BYTES)]
        t_mean, t_hw = mean_ci95(data["total"])
        stats[variant_label] = {"total": (t_mean, t_hw)}
        lines.append(f"  {variant_label:22s} total={format_mean_ci(t_mean, t_hw):>22s}")
        fct_mean, fct_p50, fct_p95, fct_max, fct_n = fct_summary(data["fct"])
        lines.append(f"  {'':22s} FCT: mean={fct_mean:.4f}s p50={fct_p50:.4f}s "
                     f"p95={fct_p95:.4f}s max={fct_max:.4f}s (n={fct_n} messages)")
        utils = data["utilization"] or []
        for link_label in ("global_0_1", "local_group0"):
            series = extract_util_series(utils, link_label, "achieved_mbit")
            if series:
                u_mean, u_hw = mean_ci95(series)
                lines.append(f"  {'':22s} {link_label:13s} achieved="
                             f"{format_mean_ci(u_mean, u_hw, unit='Mbit/s')}")
    lines.append("")

    lines.append("-" * 70)
    lines.append("COMPARISONS (speedup = avg A / avg B, >1 = A faster)")
    lines.append("-" * 70)

    dfp_variants = [v for v in VARIANTS if v[1] == "dragonfly+"]

    lines.append("dragonfly vs each DF+ spine ratio (>1 = DF+ faster -- the win condition):")
    for variant_label, _topology, _spines in dfp_variants:
        sp, sig = speedup_and_significance(
            *stats["dragonfly"]["total"], *stats[variant_label]["total"])
        lines.append(f"  dragonfly vs {variant_label} (total): {sp:.3f}  significant={sig}")
    lines.append("")

    lines.append("Dose-response (consecutive spine-ratio steps, >1 = more spines is FASTER):")
    for (label_a, _, _), (label_b, _, _) in zip(dfp_variants, dfp_variants[1:]):
        sp_tot, sig_tot = speedup_and_significance(
            *stats[label_a]["total"], *stats[label_b]["total"])
        lines.append(f"  {label_a} vs {label_b}: total={sp_tot:.3f} significant={sig_tot}")
    lines.append("")

    if len(dfp_variants) >= 2:
        first_label, last_label = dfp_variants[0][0], dfp_variants[-1][0]
        sp, sig = speedup_and_significance(
            *stats[first_label]["total"], *stats[last_label]["total"])
        lines.append(f"Full range, {first_label} vs {last_label} (total): {sp:.3f}  significant={sig}")
        lines.append("")

    lines.append("Interpretation guide: a speedup > 1.0 AND significant=True for any DF+ "
                 "variant above would be this project's first genuine DF+-beats-DF result, not "
                 "just parity. Compare against test_8's hierarchical-only result (gap=1.005, "
                 "not significant) and test_11's multipath-only result to see which combination "
                 "(if any) crosses that line, and whether the dose-response (1x->2x->4x) keeps "
                 "improving or plateaus.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test12] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
