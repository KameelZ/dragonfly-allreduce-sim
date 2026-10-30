#!/usr/bin/env python3
"""
sweep_common.py

Shared engine for every DF_vs_DF_Plus_testplan.md section (test_0..test_6).
Factored out so each test_N_*.py file only has to define ITS OWN parameter
grid and report layout, not re-derive the ~80 lines of "build the topology,
install routing, pingAll-check, run the pattern --repeat times" boilerplate,
or the statistics (no scipy in this environment -- stdlib-only Student's t).

Everything here is read-only with respect to the topology/routing modules;
it only calls their existing public functions.
"""

import math
import statistics
import sys
import time
from argparse import Namespace

from dragonfly_topo import build_network, install_gpaard_routing
from benchmark_gpaard import PATTERNS, PortAllocator, run_pattern_with_fct, format_size
from visualize_dragonfly_topo import build_topology_view

from benchmark_bandwidth_sweep import summarize_topology_brief, save_topology_plot
from link_utilization import LinkUtilizationSampler, find_global_link_switches, find_local_link_switches

from mininet.clean import cleanup
from mininet.log import info


# ---------------------------------------------------------------------------
# Per-link bandwidth utilization. Tracks the global link between groups 0/1
# (the known shared chokepoint, identical cost on both topologies by
# construction) and a representative intra-group link in group 0 (DF: flat
# mesh; DF+: leaf-spine -- every DF+ local link is leaf-spine since leaves
# never connect to each other directly). Originally only wired into
# test_3_mechanism_isolating.py; factored here so test_1/test_2/test_4 can
# reuse the identical sampler instead of each defining their own copy.
# ---------------------------------------------------------------------------
def make_utilization_sampler(net, view):
    link_specs = {}
    g_link = find_global_link_switches(view, 0, 1)
    if g_link:
        link_specs["global_0_1"] = g_link
    l_link = find_local_link_switches(view, 0)
    if l_link:
        link_specs["local_group0"] = l_link
    return LinkUtilizationSampler(net, link_specs)


def extract_util_series(utils, label, metric):
    return [u[label][metric] for u in utils if u and u.get(label) is not None]


# ---------------------------------------------------------------------------
# Resource safety (see DF_vs_DF_Plus_testplan.md finding #3: a run at
# hosts/group=24 generated ~2,200 concurrent iperf flows and nearly froze
# the host). Every test_N file in this plan should stay <= this.
# ---------------------------------------------------------------------------
HOSTS_PER_GROUP_SAFE_MAX = 10


def check_hosts_per_group_safety(width, hosts):
    hosts_per_group = width * hosts
    if hosts_per_group > HOSTS_PER_GROUP_SAFE_MAX:
        print(f"WARNING: width({width}) * hosts({hosts}) = {hosts_per_group} hosts/group. "
              f"Step 3/4's any-to-any exchange is O(hosts-per-group^2) messages PER GROUP -- "
              f"a previous run at 24 hosts/group generated ~2,200 concurrent iperf flows and "
              f"overwhelmed the VM. {hosts_per_group} hosts/group means "
              f"~{hosts_per_group * (hosts_per_group - 1)} concurrent messages per group per "
              f"step. Proceed only if you know this host can take it.", file=sys.stderr)
    return hosts_per_group


# ---------------------------------------------------------------------------
# g-PAARD step classification (see DF_vs_DF_Plus_testplan.md finding #2).
# pattern_gpaard() returns [step1, step2, step3, step4, step5, step6] at
# indices 0..5. Steps 1/3/4/6 are intra-group (DF: 1 hop: DF+: 2 hops via a
# spine -- the only place the topologies can structurally differ). Steps
# 2/5 are inter-group over the pre-existing global link -- identical cost
# on both topologies by construction. Only meaningful for the "gpaard"
# pattern itself; the adversarial patterns (shift, nearest_neighbor, ...)
# don't have this step structure and should not use this classification.
# ---------------------------------------------------------------------------
INTRA_STEP_IDX = (0, 2, 3, 5)
INTER_STEP_IDX = (1, 4)


def classify_step_times(step_times):
    """Only valid for the 6-step 'gpaard' pattern's step_times list."""
    intra = sum(step_times[i] for i in INTRA_STEP_IDX)
    inter = sum(step_times[i] for i in INTER_STEP_IDX)
    return intra, inter


# ---------------------------------------------------------------------------
# Statistics -- stdlib only (no scipy in this environment).
# ---------------------------------------------------------------------------
_T_CRIT_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def t_critical_95(df):
    if df < 1:
        return float("nan")
    return _T_CRIT_95.get(df, 1.96)


def mean_ci95(samples):
    """(mean, half_width) for a 95% CI. half_width is NaN for n<2; mean is
    also NaN for n==0 (e.g. 'intra'/'inter' are empty for non-gpaard
    patterns, which don't have that 6-step structure)."""
    n = len(samples)
    if n == 0:
        return float("nan"), float("nan")
    mean = sum(samples) / n
    if n < 2:
        return mean, float("nan")
    stdev = statistics.stdev(samples)
    half_width = t_critical_95(n - 1) * stdev / math.sqrt(n)
    return mean, half_width


def ci_overlap(mean_a, hw_a, mean_b, hw_b):
    """True if the two 95% CIs overlap (difference NOT clearly significant
    at this informal level of rigor -- CI-overlap, not a formal hypothesis test)."""
    if math.isnan(hw_a) or math.isnan(hw_b):
        return True  # can't claim significance without a real interval
    lo_a, hi_a = mean_a - hw_a, mean_a + hw_a
    lo_b, hi_b = mean_b - hw_b, mean_b + hw_b
    return lo_a <= hi_b and lo_b <= hi_a


def format_mean_ci(mean, half_width, unit="s"):
    if math.isnan(half_width):
        return f"{mean:.3f}{unit}"
    return f"{mean:.3f}{unit} +/- {half_width:.3f}"


def speedup_and_significance(df_mean, df_hw, dfp_mean, dfp_hw):
    """(speedup, significant) where speedup = avg dragonfly / avg dragonfly+
    (>1 means DF+ faster) and significant = the two 95% CIs do NOT overlap."""
    speedup = df_mean / dfp_mean if dfp_mean > 0 else float("nan")
    significant = not ci_overlap(df_mean, df_hw, dfp_mean, dfp_hw)
    return speedup, significant


def fct_summary(fct_list):
    """(mean, p50, p95, max, n) of a flat list of per-message FCT seconds."""
    n = len(fct_list)
    if n == 0:
        return (float("nan"),) * 4 + (0,)
    ordered = sorted(fct_list)
    mean = sum(ordered) / n

    def pct(p):
        idx = min(n - 1, int(round(p * (n - 1))))
        return ordered[idx]

    return mean, pct(0.50), pct(0.95), ordered[-1], n


# ---------------------------------------------------------------------------
# Topology construction
# ---------------------------------------------------------------------------
def build_topology_args(topology, groups, width, hosts, bw, delay, global_links, spines=None):
    """spines defaults to width (the canonical Dragonfly+ ratio, and DF's
    `routers` doesn't have a separate spine tier anyway) -- pass a different
    value to test an oversubscribed DF+ fabric (spines_per_group > width)."""
    return Namespace(
        topology=topology, groups=groups, routers=width, leaves=width,
        spines=spines if spines is not None else width,
        hosts=hosts, bw=bw, delay=delay, global_links=global_links,
    )


# ---------------------------------------------------------------------------
# The shared sweep engine.
# ---------------------------------------------------------------------------
def run_topology_instance(topology, groups, width, hosts, bw, delay, global_links,
                           patterns, sizes, repeat, settle, timeout,
                           label, plots_dir, lines, timings, config_key,
                           utilization_sampler_factory=None, spines=None):
    """Build one topology, plot + describe it, then for every (pattern, size)
    in the grid run --repeat iterations, recording (total, intra, inter, fct)
    per iteration into timings[(topology, config_key, pattern, size)].

    intra/inter are only meaningful for the 'gpaard' pattern (see
    classify_step_times); for every other pattern they are set to NaN and
    only 'total' and 'fct' should be used.

    `lines` (a list, mutated in place) collects the human-readable topology
    description + plot path for the report; `timings` (a dict, mutated in
    place) collects the raw per-iteration numbers for the caller to
    aggregate and format however that section's report needs.

    `utilization_sampler_factory`, if given, is called once as
    factory(net, view) -> sampler (see link_utilization.LinkUtilizationSampler)
    after routing is installed; the sampler is snapshotted before/after every
    iteration and the resulting per-link utilization dict is appended to
    timings[...]["utilization"] (a list, one entry per iteration). Left as
    None by every test file that doesn't need it (the common case).

    `spines`, if given, overrides spines_per_group away from the default
    (== width) -- e.g. to test an oversubscribed DF+ fabric. Has no effect
    on 'dragonfly' (DragonflyTopo has no spine tier; build_network ignores
    the spines arg for that topology). Left as None by every test file that
    doesn't need it (the common case).
    """
    info(f"\n*** [sweep] building topology={topology} {label}\n")
    cleanup()
    net = build_network(build_topology_args(topology, groups, width, hosts, bw, delay, global_links, spines=spines))
    topo = net.topo

    view = build_topology_view(topo)
    plot_path = save_topology_plot(topo, view, plots_dir, label)
    lines.append("-" * 70)
    lines.append(f"TOPOLOGY UNDER TEST: {topology}   [{label}]")
    lines.append("-" * 70)
    lines.append(summarize_topology_brief(topo, view))
    lines.append(f"[full topology plot saved to {plot_path}]")
    lines.append("")

    net.start()
    try:
        net.staticArp()
        install_gpaard_routing(net)
        time.sleep(settle)
        loss = net.pingAll()
        if loss != 0.0:
            raise RuntimeError(
                f"{topology}/{label}: pingAll reported {loss:.1f}% packet loss -- "
                "routing is broken, aborting before running iperf")

        sampler = utilization_sampler_factory(net, view) if utilization_sampler_factory else None

        for pattern_name in patterns:
            steps = PATTERNS[pattern_name](topo)
            is_gpaard = (pattern_name == "gpaard")
            port_allocator = PortAllocator()
            for size_bytes in sizes:
                totals, intras, inters, fcts, utils = [], [], [], [], []
                for i in range(repeat):
                    before = sampler.snapshot() if sampler else None
                    t_wall_start = time.time()
                    total_time, step_times, per_step_fct = run_pattern_with_fct(
                        net, steps, size_bytes, port_allocator, timeout)
                    totals.append(total_time)
                    if is_gpaard:
                        intra, inter = classify_step_times(step_times)
                        intras.append(intra)
                        inters.append(inter)
                    for step_records in per_step_fct:
                        fcts.extend(duration for (_s, _d, duration) in step_records)
                    if sampler:
                        wall_duration = time.time() - t_wall_start
                        utils.append(sampler.utilization_since(before, wall_duration, bw))
                    info(f"*** [sweep] {label:28s} {topology:11s} pattern={pattern_name:16s} "
                         f"size={format_size(size_bytes):>6s} iter={i + 1}/{repeat} "
                         f"-> total={total_time:.3f}s\n")
                timings[(topology, config_key, pattern_name, size_bytes)] = {
                    "total": totals, "intra": intras, "inter": inters, "fct": fcts,
                    "utilization": utils if sampler else None,
                }
    finally:
        net.stop()
