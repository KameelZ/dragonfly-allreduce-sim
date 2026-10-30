#!/usr/bin/env python3
"""
test_7_spine_oversubscription.py

Optimization idea #1 from DF_vs_DF_Plus_testplan.md's proposal section:
spine oversubscription. Zero new topology/algorithm code -- DragonflyPlusTopo
already accepts spines_per_group independently of leaves_per_group (wired
through as sweep_common.build_topology_args'/run_topology_instance's new
`spines` parameter), so this tests whether giving DF+ MORE parallel paths
through its fabric (same hop count, more capacity to spread contention
across) narrows the gap found in test_3_mechanism_isolating.py.

This is a FOCUSED TRIAGE experiment, not a full rigorous sweep: one bw/size
condition (bw=10Mbit, 4M -- the single condition where test_3 found the
largest DF+ disadvantage, 0.769-0.776), four variants (a 1x/2x/4x spine-ratio
dose-response, not just a single before/after point), n=30. The point is a
fast, cheap yes/no/how-much signal on whether contention is the dominant
driver of DF+'s slowdown, to decide whether investing in optimization idea
#2 (leaf-local pre-aggregation, real new algorithm code) is worth it:
  - if the gap keeps shrinking as spine count grows -> contention is a real
    driver, #2 (which directly reduces cross-leaf message volume) is worth
    building.
  - if the spine-ratio variants are statistically indistinguishable from
    each other -> the cost is dominated by the fixed propagation delay
    (confirmed precisely by test_6), which more capacity can't fix, and
    effort should go toward #3 (reducing hop count itself) instead.

Four variants, same groups=4/width=4/hosts=2 (8 hosts/group) base config as
test_3/test_6:
  - dragonfly            : baseline, unchanged.
  - dragonfly+ 1x spines : spines_per_group == leaves_per_group == width
                           (the canonical ratio every other test in this
                           plan uses) -- a replication check against
                           test_3's existing finding at this exact
                           bw/size point.
  - dragonfly+ 2x spines : spines_per_group = 2 * width, leaves_per_group
                           unchanged -- the oversubscribed condition.
  - dragonfly+ 4x spines : spines_per_group = 4 * width -- a second step on
                           the dose-response curve; a real, growing effect
                           across 1x->2x->4x is much stronger evidence for
                           contention than a single 1x-vs-2x data point.

KNOWN LIMITATION: with 4 variants (not 2) at a single bw/size point, there
is no natural secondary sweep axis to alternate measurement order across
(the fix applied to test_1/test_2/test_4 doesn't directly transfer here).
Variants run in a fixed order. This is acceptable for a fast triage signal;
if this experiment motivates building optimization #2, that follow-up
should use full alternation across a broader grid, matching test_3's rigor,
not reuse this file's numbers as a final claim.

BUG FIX NOTE: the first version of this file gave every dragonfly+ variant
the same config_key, so timings' (topology, config_key, pattern, size) dict
key collided between the 1x and 2x spine variants (both topology=
"dragonfly+") and the 2x run silently overwrote the 1x run's data -- the
report showed the 2x-spines numbers twice, under two different labels, with
a meaningless "1x vs 2x: no difference" comparison. Fixed by keying on
variant_label (unique per variant by construction) instead.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_7_spine_oversubscription.py
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
BANDWIDTH = 10            # Mbit/s -- test_3's condition with the largest DF+ disadvantage
SIZE_BYTES = 4194304      # 4M -- same reason
GLOBAL_LINKS = 1
REPEAT = 30
DELAY = "1ms"
SETTLE = 1
TIMEOUT = 60
PLOTS_DIR = "spine_oversubscription_plots"
OUT_PATH = "results_test7_spine_oversubscription.txt"

# (label, topology, spines_override) -- spines_override is None for
# dragonfly (no spine tier) and for the 1x replication (defaults to width).
# 1x/2x/4x gives a dose-response curve instead of a single data point:
# if the gap keeps shrinking as spine count grows, that's much stronger
# evidence for contention than just "2x helped a bit" on its own.
VARIANTS = [
    ("dragonfly", "dragonfly", None),
    ("dragonfly+_1x_spines", "dragonfly+", WIDTH),
    ("dragonfly+_2x_spines", "dragonfly+", WIDTH * 2),
    ("dragonfly+_4x_spines", "dragonfly+", WIDTH * 4),
]


def main():
    if os.geteuid() != 0:
        print("test_7_spine_oversubscription.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 7 (optimization triage): spine oversubscription")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={GROUPS} width={WIDTH} hosts/router={HOSTS} ({WIDTH * HOSTS} hosts/group) "
                 f"bw={BANDWIDTH}Mbit size={format_size(SIZE_BYTES)} delay={DELAY} "
                 f"global_links={GLOBAL_LINKS}  repeat={REPEAT}")
    lines.append("")
    lines.append("Does giving DF+ more parallel spine paths (same hop count, more capacity) "
                 "narrow the gap test_3_mechanism_isolating.py found at this exact bw/size "
                 "point (0.769-0.776 there)? Fast triage signal for whether optimization idea "
                 "#2 (leaf-local pre-aggregation) is worth building -- see "
                 "DF_vs_DF_Plus_testplan.md's optimization proposal section.")
    lines.append("")
    lines.append(PATTERN_INFO["gpaard"])
    lines.append("")

    timings = {}
    for variant_label, topology, spines in VARIANTS:
        # config_key must be unique PER VARIANT, not just per topology: two
        # of these variants share topology="dragonfly+" (1x and 2x spines),
        # and timings is keyed by (topology, config_key, pattern, size) --
        # reusing one constant config_key for all three made the 2x-spines
        # run silently overwrite the 1x-spines run's dict entry (both share
        # the same topology string), so the first version of this file
        # reported the 2x-spines data twice under two different labels.
        # Using variant_label itself as config_key makes every key distinct
        # by construction.
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

    dfp_variants = [v for v in VARIANTS if v[1] == "dragonfly+"]  # (label, topology, spines)

    lines.append("dragonfly vs each DF+ spine ratio:")
    for variant_label, _topology, _spines in dfp_variants:
        sp, sig = speedup_and_significance(
            *stats["dragonfly"]["total"], *stats[variant_label]["total"])
        note = (" (replication check against test_3's 0.769-0.776 at this exact bw/size)"
               if variant_label == "dragonfly+_1x_spines" else "")
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

    lines.append("Interpretation guide: if the dose-response comparisons above show a "
                 "consistent, significant speedup as spine count grows, contention is a real "
                 "driver and optimization idea #2 (leaf-local pre-aggregation) is worth "
                 "building. If the spine-ratio variants are statistically indistinguishable "
                 "from each other, the gap is likely dominated by the fixed propagation delay "
                 "test_6 measured precisely, which more spine capacity can't fix.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test7] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
