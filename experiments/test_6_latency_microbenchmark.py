#!/usr/bin/env python3
"""
test_6_latency_microbenchmark.py

DF_vs_DF_Plus_testplan.md, Section 6: per-hop latency microbenchmark.

Bypasses iperf/TCP entirely -- directly satisfies the course spec's
"per-packet latency statistics" requirement, and sidesteps the TCP
SYN-retransmit-style noise found in every iperf-based sweep in this project
(DF_vs_DF_Plus_testplan.md finding #5). Ping-based RTT, many samples per
pair, for three host-pair cases:

  (a) same_switch: two hosts on the same router/leaf. 2 links (host-switch-
      host) in EITHER topology -- expect DF ~ DF+ (ratio ~1.0).
  (b) diff_switch_same_group: two hosts on different routers/leaves within
      one group. THE core structural claim of this whole project: DF's path
      is host-switch1-switch2-host (3 links); DF+'s is host-leaf1-spine-
      leaf2-host (4 links, the extra leaf-spine hop). Expected ratio
      DF+/DF = 4/3 ~ 1.333 -- NOT ~2.0 as an earlier version of this file
      claimed (that was counting "hops" loosely rather than actual links on
      the round-trip path; the real first run's data matched 4/3 almost
      exactly: 8.078ms/6.048ms = 1.336).
  (c) cross_group_designated: the actual two hosts g-PAARD's own schedule
      uses to exchange group 0's and group 1's shards directly over the
      pre-existing global link (gpaard.designate_roles). NOT a control, and
      NOT ~1.0 as an earlier version of this file claimed: DF's designated
      host sits directly on the global-link-carrying router (3 links:
      host-switch-[global]-switch-host), but DF+'s designated host is a
      LEAF host -- global links only attach to spines in DF+, never leaves
      -- so it needs a leaf-spine hop on EACH end (5 links: host-leaf-
      spine-[global]-spine-leaf-host). Expected ratio = 5/3 ~ 1.667. This
      was the single most important correction that came out of reviewing
      the first run's results: DF+ pays for the "2-hop" fabric design on
      EVERY inter-group exchange, not just the intra-group ones.

Runs this at multiple scales (groups=4,6,8, matching test_2's scale axis)
since hop counts are structural, not scale-dependent -- the ratios above
should hold at every group count tested. Confirming that is a stronger,
more presentable claim than a single data point.

This should be the lowest-noise, most presentable chart in the whole
report: each ping costs milliseconds (not the seconds an iperf transfer
costs), so 150 samples per pair is cheap and gives a tight CI.

width=4, hosts/router=2 (8 hosts/group, the validated-safe size) throughout.

Requires root (Mininet manipulates network namespaces):

    sudo python3 experiments/test_6_latency_microbenchmark.py
"""

import os
import re
import sys
import time

import matplotlib
matplotlib.use("Agg")  # headless: never try to open a GUI window under sudo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mininet.clean import cleanup
from mininet.log import setLogLevel, info

from dragonfly_topo import build_network, install_gpaard_routing
from gpaard import designate_roles
from visualize_dragonfly_topo import build_topology_view

from benchmark_bandwidth_sweep import summarize_topology_brief, save_topology_plot
from sweep_common import build_topology_args, mean_ci95, format_mean_ci, check_hosts_per_group_safety

WIDTH = 4
HOSTS = 2
GROUPS_VALUES = (4, 6, 8)  # matches test_2's scale axis
BANDWIDTH = 100
DELAY = "1ms"
GLOBAL_LINKS = 1
SETTLE = 1
PING_COUNT = 150
PING_INTERVAL = "0.01"
PLOTS_DIR = "latency_topology_plots"
OUT_PATH = "results_test6_latency.txt"

# (case_name -> expected DF+/DF RTT ratio, from link-counting the round
# trip path; see module docstring for the derivation of each).
EXPECTED_RATIO = {
    "same_switch": 1.0,
    "diff_switch_same_group": 4 / 3,
    "cross_group_designated": 5 / 3,
}

PING_SUMMARY_RE = re.compile(
    r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")
PING_LOSS_RE = re.compile(r"([\d.]+)% packet loss")


def parse_ping_summary(output):
    m = PING_SUMMARY_RE.search(output)
    if not m:
        return None
    return tuple(float(x) for x in m.groups())  # (min_ms, avg_ms, max_ms, mdev_ms)


def parse_ping_loss(output):
    m = PING_LOSS_RE.search(output)
    return float(m.group(1)) if m else float("nan")


def find_same_switch_pair(topo):
    """Two hosts sharing the same router/leaf (0 inter-switch hops)."""
    by_switch = {}
    for host_name, (g, r, _h) in topo.host_map.items():
        by_switch.setdefault((g, r), []).append(host_name)
    for hosts in by_switch.values():
        if len(hosts) >= 2:
            return sorted(hosts)[0], sorted(hosts)[1]
    return None


def find_diff_switch_same_group_pair(topo):
    """Two hosts on different routers/leaves within the same group."""
    by_group_switch = {}
    for host_name, (g, r, _h) in topo.host_map.items():
        by_group_switch.setdefault(g, {}).setdefault(r, []).append(host_name)
    for group_id, by_router in by_group_switch.items():
        routers = sorted(by_router.keys())
        if len(routers) >= 2:
            return by_router[routers[0]][0], by_router[routers[1]][0]
    return None


def find_cross_group_designated_pair(topo):
    """The two hosts g-PAARD's own schedule uses to exchange group 0's and
    group 1's shards directly over the pre-existing global link."""
    if topo.num_groups < 2:
        return None
    role_host = designate_roles(topo)
    h1 = role_host.get((0, 1))
    h2 = role_host.get((1, 0))
    if h1 is None or h2 is None:
        return None
    return h1, h2


PAIR_FINDERS = {
    "same_switch": find_same_switch_pair,
    "diff_switch_same_group": find_diff_switch_same_group_pair,
    "cross_group_designated": find_cross_group_designated_pair,
}


def run_ping_microbenchmark(topology, groups, lines, results):
    label = f"latency_groups{groups}_{topology.replace('+', 'plus')}"
    info(f"\n*** [test6] building topology={topology} groups={groups}\n")
    cleanup()
    net = build_network(build_topology_args(
        topology, groups, WIDTH, HOSTS, BANDWIDTH, DELAY, GLOBAL_LINKS))
    topo = net.topo

    view = build_topology_view(topo)
    plot_path = save_topology_plot(topo, view, PLOTS_DIR, label)
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
        time.sleep(SETTLE)
        loss = net.pingAll()
        if loss != 0.0:
            raise RuntimeError(
                f"{topology}/groups={groups}: pingAll reported {loss:.1f}% packet loss -- "
                "routing is broken, aborting before running ping microbenchmark")

        for case_name, finder in PAIR_FINDERS.items():
            pair = finder(topo)
            if pair is None:
                info(f"*** [test6] {topology} groups={groups}: no valid pair for "
                     f"case={case_name}, skipping\n")
                results[(topology, groups, case_name)] = None
                continue
            src, dst = pair
            dst_ip = net.get(dst).IP()
            info(f"*** [test6] {topology} groups={groups} case={case_name}: "
                 f"{src} -> {dst} ({dst_ip}), {PING_COUNT} pings\n")
            output = net.get(src).cmd(
                f"ping -c {PING_COUNT} -i {PING_INTERVAL} {dst_ip}")
            summary = parse_ping_summary(output)
            pkt_loss = parse_ping_loss(output)
            results[(topology, groups, case_name)] = {
                "pair": (src, dst), "summary": summary, "loss_pct": pkt_loss,
            }
    finally:
        net.stop()


def main():
    if os.geteuid() != 0:
        print("test_6_latency_microbenchmark.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    check_hosts_per_group_safety(WIDTH, HOSTS)
    setLogLevel("info")

    lines = []
    lines.append("Section 6: per-hop latency microbenchmark (ping-based RTT, no iperf/TCP)")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"groups swept: {list(GROUPS_VALUES)}  width={WIDTH} hosts/router={HOSTS} "
                 f"({WIDTH * HOSTS} hosts/group) bw={BANDWIDTH}Mbit delay={DELAY} "
                 f"global_links={GLOBAL_LINKS}")
    lines.append(f"ping count per pair: {PING_COUNT}  interval: {PING_INTERVAL}s")
    lines.append("")
    lines.append("(a) same_switch: 2 links (host-switch-host) either topology -- expect ratio ~1.0.")
    lines.append("(b) diff_switch_same_group: THE core structural claim -- DF is 3 links "
                 "(host-switch1-switch2-host), DF+ is 4 (host-leaf1-spine-leaf2-host) -- "
                 "expect ratio = 4/3 ~ 1.333.")
    lines.append("(c) cross_group_designated: g-PAARD's actual step-2 host pair. NOT a "
                 "control -- DF+'s designated host is a leaf, and global links only attach "
                 "to spines, so it pays a leaf-spine hop on EACH end: DF is 3 links "
                 "(host-switch-[global]-switch-host), DF+ is 5 (host-leaf-spine-[global]-"
                 "spine-leaf-host) -- expect ratio = 5/3 ~ 1.667.")
    lines.append("Hop counts are structural, not scale-dependent -- these ratios should hold "
                 "at every group count swept below.")
    lines.append("")

    results = {}
    for groups in GROUPS_VALUES:
        for topology in ("dragonfly", "dragonfly+"):
            run_ping_microbenchmark(topology, groups, lines, results)

    lines.append("=" * 70)
    lines.append("RESULTS: ping RTT min/avg/max/mdev (ms), from the ping utility's own summary")
    lines.append("=" * 70)

    summary_rows = []  # (groups, case_name, ratio) for the final table
    for groups in GROUPS_VALUES:
        lines.append("-" * 70)
        lines.append(f"groups={groups}")
        lines.append("-" * 70)
        for case_name in PAIR_FINDERS:
            lines.append(f"  CASE: {case_name}  (expected ratio ~{EXPECTED_RATIO[case_name]:.3f})")
            avgs = {}
            for topology in ("dragonfly", "dragonfly+"):
                r = results.get((topology, groups, case_name))
                if r is None or r["summary"] is None:
                    lines.append(f"    {topology:11s} <no data -- pair not found or ping failed>")
                    continue
                min_ms, avg_ms, max_ms, mdev_ms = r["summary"]
                avgs[topology] = avg_ms
                src, dst = r["pair"]
                lines.append(f"    {topology:11s} {src}->{dst}  "
                             f"min={min_ms:.3f}ms avg={avg_ms:.3f}ms max={max_ms:.3f}ms "
                             f"mdev={mdev_ms:.3f}ms  loss={r['loss_pct']:.1f}%  (n={PING_COUNT})")
            if "dragonfly" in avgs and "dragonfly+" in avgs and avgs["dragonfly"] > 0:
                ratio = avgs["dragonfly+"] / avgs["dragonfly"]
                lines.append(f"    DF+/DF avg RTT ratio: {ratio:.3f}  "
                             f"(expected ~{EXPECTED_RATIO[case_name]:.3f})")
                summary_rows.append((groups, case_name, ratio))
            lines.append("")

    lines.append("=" * 70)
    lines.append("SUMMARY: measured vs expected DF+/DF ratio, across every group count swept")
    lines.append("=" * 70)
    lines.append(f"  {'groups':>8s} {'case':>24s} {'measured':>10s} {'expected':>10s}")
    for groups, case_name, ratio in summary_rows:
        lines.append(f"  {groups:>8d} {case_name:>24s} {ratio:>10.3f} "
                     f"{EXPECTED_RATIO[case_name]:>10.3f}")
    lines.append("")
    lines.append("If measured stays close to expected across every groups row for a case, "
                 "that confirms the ratio is a structural (hop-count) property, not an "
                 "artifact of one particular scale.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [test6] results written to {OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
