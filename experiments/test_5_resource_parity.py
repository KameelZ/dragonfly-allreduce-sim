#!/usr/bin/env python3
"""
test_5_resource_parity.py

DF_vs_DF_Plus_testplan.md, Section 5: resource-parity documentation.

The course spec requires Dragonfly/Dragonfly+ builds to use "roughly equal
resources -- matched switch count, aggregated bandwidth, and link count;
differences must be documented." No earlier script in this project ever
computed or stated this delta. This one does, for every topology config
used across test_0..test_4, and states the rationale for not forcing exact
parity (see DF_vs_DF_Plus_testplan.md finding #6): DF+'s standard design is
leaves=spines (a full bipartite leaf-spine fabric); shrinking the spine
count to force switch-count parity with DF would introduce a congestion
confound (all intra-group traffic funneled through fewer spines) that is
worse than the honest, documented resource-count delta.

This is a REPORT, not a timed test -- it only constructs topology objects
directly (DragonflyTopo / DragonflyPlusTopo) and counts switches/links; it
never wraps them in a live Mininet network, so unlike every other
test_N_*.py in this plan, it needs NO root privileges and runs in seconds.

    python3 experiments/test_5_resource_parity.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dragonfly_topo import DragonflyTopo
from dragonfly_plus_topo import DragonflyPlusTopo
from visualize_dragonfly_topo import build_topology_view, classify_link

# Every (groups, width, hosts, global_links) config actually used in
# test_0_sanity_identical / test_1_paper_comparable / test_2_scale_sweep /
# test_3_mechanism_isolating / test_4_adversarial_patterns. width=4, hosts=2
# throughout (the validated-safe 8 hosts/group) except the group-count axis.
CONFIGS = [
    # (groups, width, hosts, global_links, used_by)
    (2, 4, 2, 1, "test_0"),
    (3, 4, 2, 1, "test_0"),
    (4, 4, 2, 1, "test_1, test_4"),
    (6, 4, 2, 1, "test_2"),
    (8, 4, 2, 1, "test_2"),
    (4, 4, 2, 2, "test_3 (global_links=2 variant)"),
]
REFERENCE_BW = 100  # Mbit/s, illustrative only -- every link shares one bw in this project
OUT_PATH = "results_test5_resource_parity.txt"


def counts_by_kind(view):
    counts = {"host": 0, "local": 0, "global": 0}
    for n1, n2 in view["links"]:
        counts[classify_link(n1, n2, view["host_set"], view["switch_group"])] += 1
    return counts


def describe(topo):
    view = build_topology_view(topo)
    kinds = counts_by_kind(view)
    return {
        "switches": len(view["switches"]),
        "hosts": len(view["hosts"]),
        "links": len(view["links"]),
        "host_links": kinds["host"],
        "local_links": kinds["local"],
        "global_links": kinds["global"],
        "aggregate_bw_mbit": len(view["links"]) * REFERENCE_BW,
    }


def main():
    lines = []
    lines.append("Section 5: resource-parity documentation")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("Course spec requirement: DF/DF+ builds should use 'roughly equal "
                 "resources -- matched switch count, aggregated bandwidth, and link count; "
                 "differences must be documented.'")
    lines.append("")
    lines.append("Design decision: DF+ here always uses spines_per_group == leaves_per_group "
                 "(the standard Dragonfly+ full bipartite leaf-spine fabric). We deliberately "
                 "do NOT shrink DF+'s spine count to force switch-count parity with DF -- a "
                 "single spine would funnel all intra-group traffic through one switch, which "
                 "is a congestion confound worse than the resource-count delta documented below.")
    lines.append("")
    lines.append(f"(aggregate bandwidth uses an illustrative reference bw={REFERENCE_BW} Mbit/s "
                 "per link -- every link in this project shares one bw value, so the RATIO "
                 "below is what matters, not the absolute number)")
    lines.append("")

    lines.append(f"{'config':<45s} {'topology':<11s} {'switches':>8s} {'hosts':>6s} "
                 f"{'links':>6s} {'(host/local/global)':>20s} {'agg_bw(Mbit)':>12s}")
    lines.append("-" * 130)

    ratios = []
    for groups, width, hosts, global_links, used_by in CONFIGS:
        df = DragonflyTopo(num_groups=groups, routers_per_group=width, hosts_per_router=hosts,
                           global_links_per_pair=global_links)
        dfp = DragonflyPlusTopo(num_groups=groups, leaves_per_group=width, spines_per_group=width,
                                hosts_per_leaf=hosts, global_links_per_pair=global_links)
        d_df = describe(df)
        d_dfp = describe(dfp)
        config_label = f"groups={groups},width={width},hosts={hosts},gl={global_links}"

        for name, d in (("dragonfly", d_df), ("dragonfly+", d_dfp)):
            breakdown = f"({d['host_links']}/{d['local_links']}/{d['global_links']})"
            lines.append(f"{config_label:<45s} {name:<11s} {d['switches']:>8d} {d['hosts']:>6d} "
                         f"{d['links']:>6d} {breakdown:>20s} {d['aggregate_bw_mbit']:>12d}")

        switch_ratio = d_dfp["switches"] / d_df["switches"]
        link_ratio = d_dfp["links"] / d_df["links"]
        ratios.append((config_label, used_by, switch_ratio, link_ratio))
        lines.append(f"{'':<45s} {'-- used by: ' + used_by:<11s} "
                     f"switch_ratio(DF+/DF)={switch_ratio:.2f}  link_ratio(DF+/DF)={link_ratio:.2f}")
        lines.append("")

    lines.append("=" * 70)
    lines.append("SUMMARY: DF+/DF switch-count and link-count ratios across every config")
    lines.append("=" * 70)
    for config_label, used_by, switch_ratio, link_ratio in ratios:
        lines.append(f"  {config_label:<35s} switches x{switch_ratio:.2f}  links x{link_ratio:.2f}  "
                     f"(used by: {used_by})")
    lines.append("")
    avg_switch_ratio = sum(r[2] for r in ratios) / len(ratios)
    lines.append(f"Average switch-count ratio across all configs: x{avg_switch_ratio:.2f} -- "
                 "this is the resource delta that must be stated alongside every DF-vs-DF+ "
                 "performance number in the final report.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"*** [test5] results written to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
