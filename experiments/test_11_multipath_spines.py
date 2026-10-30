#!/usr/bin/env python3
"""
test_11_multipath_spines.py

Optimization idea #5 from DF_vs_DF_Plus_testplan.md: ECMP multipath across
spines. This is a direct re-run of test_7_spine_oversubscription.py's exact
config (same 4 variants, same bw=10Mbit/4M worst-case condition, same
repeat=30) -- the ONLY thing that's changed is dragonfly_topo.py's routing
layer itself (_shortest_path_next_hops/select_path_next_hop), which no
longer forces every cross-leaf flow in a Dragonfly+ group through a single
fixed spine.

Why re-run test_7 instead of writing something new: test_7 already showed
that adding more spine hardware alone (1x/2x/4x spines_per_group) did
nothing -- the variants were statistically indistinguishable. We then found
the actual cause: the routing layer's shortest-path TREE picked exactly one
next hop per switch, always resolving to the same (lexicographically first)
spine regardless of how many existed, so the extra spines from idea #1 were
structurally unreachable, not merely underused. See
test_dragonfly_topo.py for the correctness tests (shortest-path, loop-free,
deterministic across process runs) that gate this change -- it was verified
there BEFORE this performance re-run, same discipline as every other
optimization in this project.

With that fixed, this is the real test of idea #1's original hypothesis:
does MORE spine capacity help once the routing can actually reach it? Three
possible outcomes:
  - 1x-spines-with-fix beats test_7's original 1x result: the routing fix
    alone helps, even with no new hardware (existing spines were already
    being wasted).
  - 2x/4x show a real, growing speedup over 1x (unlike test_7's flat
    dose-response): more spine capacity now pays off once actually used.
  - DF+ closes further toward DF but is not expected to fully overtake it:
    test_6 measured a FIXED extra-hop latency/propagation cost for DF+ that
    no amount of load-balancing removes (that's a structural, not a
    congestion, cost) -- multipath attacks the contention component of the
    gap, not that fixed tax.

Pattern is flat gpaard (NOT gpaard_hierarchical) -- this isolates idea #5's
own effect. test_12_hierarchical_plus_multipath.py tests idea #5 combined
with idea #2 (hierarchical schedule), which is the more promising path to an
outright DF+ win since it attacks both the message-volume and the
path-diversity side of the problem at once. Independent of this file's
result -- same base config, no dependency between them.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_11_multipath_spines.py
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
BANDWIDTH = 10            # Mbit/s -- same worst-case condition as test_7/test_8
SIZE_BYTES = 4194304      # 4M -- same reason
GLOBAL_LINKS = 1
REPEAT = 30
DELAY = "1ms"
SETTLE = 1
TIMEOUT = 60
PLOTS_DIR = "multipath_spines_topology_plots"
OUT_PATH = "results_test11_multipath_spines.txt"

# Identical variant set to test_7_spine_oversubscription.py -- same dose-
# response design (1x/2x/4x), so this file's numbers are directly comparable
# to test_7's, with the routing fix as the only variable that changed.
VARIANTS = [
    ("dragonfly", "dragonfly", None),
    ("dragonfly+_1x_spines", "dragonfly+", WIDTH),
    ("dragonfly+_2x_spines", "dragonfly+", WIDTH * 2),
    ("dragonfly+_4x_spines", "dragonfly+", WIDTH * 4),
]


def main():
    if os.geteuid() != 0:
        print("test_11_multipath_spines.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 11 (optimization idea #5): ECMP multipath across spines")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={GROUPS} width={WIDTH} hosts/router={HOSTS} ({WIDTH * HOSTS} hosts/group) "
                 f"bw={BANDWIDTH}Mbit size={format_size(SIZE_BYTES)} delay={DELAY} "
                 f"global_links={GLOBAL_LINKS}  repeat={REPEAT}")
    lines.append("")
    lines.append("Re-run of test_7_spine_oversubscription.py's exact config: same 4 variants, "
                 "same worst-case bw/size condition. The only thing that changed is "
                 "dragonfly_topo.py's routing layer, which previously forced every cross-leaf "
                 "flow through a single fixed spine regardless of how many existed -- the real "
                 "reason test_7 found 1x/2x/4x spines statistically indistinguishable. "
                 "Correctness of the new multipath routing (shortest-path, loop-free, "
                 "deterministic) was verified in test_dragonfly_topo.py before this run.")
    lines.append("")
    lines.append(PATTERN_INFO["gpaard"])
    lines.append("")

    timings = {}
    for variant_label, topology, spines in VARIANTS:
        run_topology_instance(
            topology, groups=GROUPS, width=WIDTH, hosts=HOSTS, bw=BANDWIDTH,
            delay=DELAY, global_links=GLOBAL_LINKS, patterns=["gpaard"],
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
        data = timings[(topology, variant_label, "gpaard", SIZE_BYTES)]
        t_mean, t_hw = mean_ci95(data["total"])
        i_mean, i_hw = mean_ci95(data["intra"])
        stats[variant_label] = {"total": (t_mean, t_hw), "intra": (i_mean, i_hw)}
        lines.append(f"  {variant_label:22s} total={format_mean_ci(t_mean, t_hw):>22s}  "
                     f"intra={format_mean_ci(i_mean, i_hw):>22s}")
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

    lines.append("dragonfly vs each DF+ spine ratio:")
    for variant_label, _topology, _spines in dfp_variants:
        sp, sig = speedup_and_significance(
            *stats["dragonfly"]["total"], *stats[variant_label]["total"])
        note = (" (compare to test_7's original 1x result -- same hardware, routing-fix-only "
                "delta)" if variant_label == "dragonfly+_1x_spines" else "")
        lines.append(f"  dragonfly vs {variant_label} (total): {sp:.3f}  significant={sig}{note}")
    lines.append("")

    lines.append("Dose-response (consecutive spine-ratio steps, >1 = more spines is FASTER):")
    for (label_a, _, _), (label_b, _, _) in zip(dfp_variants, dfp_variants[1:]):
        sp_tot, sig_tot = speedup_and_significance(
            *stats[label_a]["total"], *stats[label_b]["total"])
        sp_intra, sig_intra = speedup_and_significance(
            *stats[label_a]["intra"], *stats[label_b]["intra"])
        lines.append(f"  {label_a} vs {label_b}: total={sp_tot:.3f} significant={sig_tot}   "
                     f"intra-only={sp_intra:.3f} significant={sig_intra}")
    lines.append("")

    if len(dfp_variants) >= 2:
        first_label, last_label = dfp_variants[0][0], dfp_variants[-1][0]
        sp, sig = speedup_and_significance(
            *stats[first_label]["total"], *stats[last_label]["total"])
        lines.append(f"Full range, {first_label} vs {last_label} (total): {sp:.3f}  significant={sig}")
        lines.append("")

    lines.append("Interpretation guide: compare dragonfly+_1x_spines here against test_7's "
                 "original dragonfly+_1x_spines result (same hardware, same condition) -- any "
                 "improvement there is the routing fix alone, with zero new hardware. Then "
                 "check whether 2x/4x now show a real, growing speedup over 1x (unlike test_7's "
                 "flat dose-response) -- that would confirm spine capacity now pays off once "
                 "actually reachable. Full DF+ vs DF parity or better would be a genuine new "
                 "result; closing further without fully overtaking DF is the expected outcome "
                 "given test_6's fixed extra-hop cost, which multipath cannot remove.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test11] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
