#!/usr/bin/env python3
"""
test_3_mechanism_isolating.py

DF_vs_DF_Plus_testplan.md, Section 3: mechanism-isolating sweep.

Targets the specific corner the project's own earlier analysis flagged as
where a DF-vs-DF+ topology effect, if any, should be most visible: low
bandwidth + large messages (small messages/high bandwidth mostly measure
TCP/iperf setup overhead, not the network). Also re-checks the
"global_links_per_pair is inert for g-PAARD" claim (single designated link,
no ECMP in the baseline routing) at proper statistical rigor -- low-repeat
data in results_bw_sweep_four_groups_2link.txt (n=5) hinted, unconfirmed, at
a secondary effect worth checking rather than taking on faith.

Topology: groups=4, width=4, hosts/router=2 (8 hosts/group, the validated-
safe size used throughout this plan).

Adds instrumentation item #3 from the test plan: per-link bandwidth
utilization, sampled via link_utilization.py, on (a) the global link
between groups 0 and 1 -- the known shared chokepoint, identical cost on
both topologies by construction -- and (b) a representative intra-group
link in group 0 -- DF's flat router mesh vs DF+'s leaf-spine fabric, which
has no DF equivalent. This answers a different question than "who finished
first": does DF+'s extra fabric spread load across more links even when
total completion time doesn't move?

Alternates which topology is measured first across the bandwidth axis (as
in the project's own earlier benchmark_df_vs_dfplus.py) so any monotonic
system-level drift over the run affects both arms roughly equally.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_3_mechanism_isolating.py
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
BANDWIDTHS = [10, 50]          # Mbit/s -- the low end, per the project's own prior analysis
SIZES = [1048576, 4194304]     # 1M, 4M -- large enough to be bandwidth-bound, not overhead-bound
GLOBAL_LINKS_VALUES = [1, 2]   # re-checking the "inert" claim
REPEAT = 30
DELAY = "1ms"
SETTLE = 1
TIMEOUT = 60
PLOTS_DIR = "mechanism_topology_plots"
OUT_PATH = "results_test3_mechanism.txt"


def main():
    if os.geteuid() != 0:
        print("test_3_mechanism_isolating.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 3: mechanism-isolating sweep (low bandwidth / large message corner, "
                 "+ per-link utilization)")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups={GROUPS} width={WIDTH} hosts/router={HOSTS} ({WIDTH * HOSTS} hosts/group) "
                 f"delay={DELAY}")
    lines.append(f"bandwidths (Mbit/s): {', '.join('%g' % b for b in BANDWIDTHS)}")
    lines.append(f"message sizes: {', '.join(format_size(s) for s in SIZES)}")
    lines.append(f"global_links_per_pair: {GLOBAL_LINKS_VALUES}")
    lines.append(f"pattern: gpaard (only)  repeat: {REPEAT}")
    lines.append("")
    lines.append(PATTERN_INFO["gpaard"])
    lines.append("")
    lines.append("Utilization tracks the global link (groups 0<->1, identical cost on both "
                 "topologies by construction -- a control) and a representative intra-group "
                 "link in group 0 (DF: flat router mesh; DF+: leaf-spine, no DF equivalent).")
    lines.append("")

    timings = {}
    for global_links in GLOBAL_LINKS_VALUES:
        for bw_idx, bw in enumerate(BANDWIDTHS):
            order = ("dragonfly", "dragonfly+") if bw_idx % 2 == 0 else ("dragonfly+", "dragonfly")
            for topology in order:
                label = f"gl{global_links}_bw{bw:g}_{topology.replace('+', 'plus')}"
                run_topology_instance(
                    topology, groups=GROUPS, width=WIDTH, hosts=HOSTS, bw=bw,
                    delay=DELAY, global_links=global_links, patterns=["gpaard"],
                    sizes=SIZES, repeat=REPEAT, settle=SETTLE, timeout=TIMEOUT,
                    label=label, plots_dir=PLOTS_DIR, lines=lines, timings=timings,
                    config_key=("gl", global_links, "bw", bw),
                    utilization_sampler_factory=make_utilization_sampler,
                )

    lines.append("=" * 70)
    lines.append(f"RESULTS: mean [95% CI], n={REPEAT}")
    lines.append("=" * 70)

    summary_rows = []
    for global_links in GLOBAL_LINKS_VALUES:
        for bw in BANDWIDTHS:
            lines.append("-" * 70)
            lines.append(f"global_links_per_pair={global_links}  bandwidth={bw:g} Mbit/s")
            lines.append("-" * 70)
            for size_bytes in SIZES:
                lines.append(f"  size={format_size(size_bytes)}")
                stats = {}
                for topology in ("dragonfly", "dragonfly+"):
                    data = timings[(topology, ("gl", global_links, "bw", bw), "gpaard", size_bytes)]
                    t_mean, t_hw = mean_ci95(data["total"])
                    i_mean, i_hw = mean_ci95(data["intra"])
                    stats[topology] = {"total": (t_mean, t_hw), "intra": (i_mean, i_hw)}
                    lines.append(f"    {topology:11s} total={format_mean_ci(t_mean, t_hw):>22s}  "
                                 f"intra={format_mean_ci(i_mean, i_hw):>22s}")
                    fct_mean, fct_p50, fct_p95, fct_max, fct_n = fct_summary(data["fct"])
                    lines.append(f"    {'':11s} FCT: mean={fct_mean:.4f}s p50={fct_p50:.4f}s "
                                 f"p95={fct_p95:.4f}s max={fct_max:.4f}s (n={fct_n} messages)")

                    utils = data["utilization"] or []
                    for link_label in ("global_0_1", "local_group0"):
                        series = extract_util_series(utils, link_label, "achieved_mbit")
                        if series:
                            u_mean, u_hw = mean_ci95(series)
                            lines.append(f"    {'':11s} {link_label:13s} achieved="
                                         f"{format_mean_ci(u_mean, u_hw, unit='Mbit/s')}")

                tot_speedup, tot_sig = speedup_and_significance(
                    *stats["dragonfly"]["total"], *stats["dragonfly+"]["total"])
                intra_speedup, intra_sig = speedup_and_significance(
                    *stats["dragonfly"]["intra"], *stats["dragonfly+"]["intra"])
                lines.append(f"    speedup total: {tot_speedup:.3f}  significant={tot_sig}   "
                             f"speedup intra-only: {intra_speedup:.3f}  significant={intra_sig}")
                summary_rows.append((global_links, bw, size_bytes, tot_speedup, tot_sig))
                lines.append("")

    lines.append("=" * 70)
    lines.append("SUMMARY (speedup = avg dragonfly / avg dragonfly+, >1 = DF+ faster)")
    lines.append("=" * 70)
    lines.append(f"  {'gl':>4s} {'bw':>8s} {'size':>6s} {'speedup':>10s} {'sig':>5s}")
    for global_links, bw, size_bytes, tot_sp, tot_sig in summary_rows:
        lines.append(f"  {global_links:>4d} {bw:>8g} {format_size(size_bytes):>6s} "
                     f"{tot_sp:>10.3f} {str(tot_sig):>5s}")
    lines.append("")
    lines.append("If global_links=1 and global_links=2 rows look statistically "
                 "indistinguishable (overlapping CIs / similar speedups), that confirms the "
                 "'extra global links are inert for g-PAARD' claim; a consistent difference "
                 "between them would contradict it and is worth a closer look.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test3] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
