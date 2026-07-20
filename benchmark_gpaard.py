#!/usr/bin/env python3
"""
benchmark_gpaard.py

Live Mininet experiment comparing DragonflyTopo (canonical Dragonfly) against
DragonflyPlusTopo (leaf-spine Dragonfly+) under real traffic, across a grid
of message sizes and traffic patterns -- including g-PAARD's own 6-step
all-reduce schedule (gpaard.py), a few synthetic patterns from the project's
traffic-load roadmap (uniform / all-to-all / hotspot), and adversarial
worst-case patterns (shift / nearest_neighbor) designed to intentionally
concentrate traffic onto a single global link instead of spreading it evenly.

For each (topology, traffic pattern, message size) combination this builds
the network, installs the existing shortest-path forwarding
(install_gpaard_routing), then replays the pattern's message steps with real
iperf transfers (each step's messages run concurrently; steps run in order,
matching how g-PAARD's schedule is defined), measuring wall-clock completion
time per step. Results are written to a new file (--out) once every
combination has finished, organized one section per traffic pattern (each
with its own results table, Dragonfly-vs-Dragonfly+ speedup, and per-step
timings) so every pattern's story reads standalone.

Requires root (Mininet manipulates network namespaces):

    sudo python3 benchmark_gpaard.py
    sudo python3 benchmark_gpaard.py --sizes 4K,64K,1M,8M --patterns gpaard,uniform,all_to_all,hotspot,shift,nearest_neighbor
    sudo python3 benchmark_gpaard.py --groups 3 --routers 2 --leaves 2 --spines 2 --hosts 2 --out results.txt
    sudo python3 benchmark_gpaard.py --groups 2 --patterns shift --out single_link.txt   # literal single-global-link saturation
"""

from mininet.clean import cleanup
from mininet.log import setLogLevel, info

from argparse import Namespace
import argparse
import os
import random
import subprocess
import sys
import textwrap
import time

from dragonfly_topo import build_network, install_gpaard_routing
from gpaard import build_gpaard_schedule, group_hosts


# ---------------------------------------------------------------------------
# Traffic patterns
# ---------------------------------------------------------------------------
# Every pattern is a function(topo) -> list of steps, where a step is a list
# of (src_host, dst_host) messages that all occur simultaneously. Steps run
# one after another, mirroring g-PAARD's own step semantics.

def pattern_gpaard(topo):
    """g-PAARD's real 6-step all-reduce schedule (reduce-scatter + all-gather)."""
    schedule = build_gpaard_schedule(topo)
    steps = schedule["reduce_scatter"] + schedule["all_gather"]
    return [[(src, dst) for src, dst, _shard in step] for step in steps]


def pattern_uniform(topo, seed=0):
    """Every host sends once to a uniformly random other host, all at once."""
    hosts = sorted(topo.host_map.keys())
    rng = random.Random(seed)
    step = [(h, rng.choice([o for o in hosts if o != h])) for h in hosts]
    return [step]


def pattern_all_to_all(topo):
    """Every host sends to every other host, all at once (heaviest pattern)."""
    hosts = sorted(topo.host_map.keys())
    step = [(src, dst) for src in hosts for dst in hosts if src != dst]
    return [step]


def pattern_hotspot(topo, seed=0):
    """Every host sends to one randomly chosen host at once (worst-case incast)."""
    hosts = sorted(topo.host_map.keys())
    rng = random.Random(seed)
    target = rng.choice(hosts)
    step = [(h, target) for h in hosts if h != target]
    return [step]


# ---------------------------------------------------------------------------
# Adversarial traffic (worst-case fabric stress)
# ---------------------------------------------------------------------------
# Unlike hotspot (which stresses a single *endpoint*'s down-link, regardless
# of which host gets picked), the patterns below are constructed from the
# topology's own group structure so they target a single piece of *fabric*:
# the global link(s) joining one specific pair of groups. Every group in this
# DragonflyTopo/DragonflyPlusTopo is connected to every other group by a
# direct global link (a complete graph over groups), and shortest-path
# routing always takes that one direct hop -- so any traffic pattern that
# sends *all* of one group's egress to the *same* other group forces every
# one of those flows to fight over that single link's bandwidth, with zero
# chance of spreading across alternate paths. That is the textbook
# "adversarial"/"worst-case" traffic used to stress-test Dragonfly fabrics in
# the literature (as opposed to uniform/all-to-all traffic, which spreads
# load evenly across every global link and rarely saturates any single one).

def _hosts_by_group(topo):
    """{group_id: [host_names...]} ordered by (router_id, host_id) within
    the group, i.e. each group's hosts in stable physical placement order."""
    by_group = {}
    for host, (group_id, router_id, host_id) in topo.host_map.items():
        by_group.setdefault(group_id, []).append((router_id, host_id, host))
    return {g: [h for _r, _hid, h in sorted(entries)] for g, entries in by_group.items()}


def pattern_shift(topo, shift=1):
    """Adversarial ring-shift traffic: every host in group g sends to the
    position-matched host in group (g + shift) mod num_groups, all at once.

    Because every group pair is joined by exactly `global_links_per_pair`
    direct global links, this dumps 100% of each group's outbound traffic
    onto the single link (or handful of links) to its shift target -- the
    per-link worst case, happening simultaneously on every group-pair edge
    around the ring. Run with `--groups 2` and this becomes a literal
    single-global-link saturation test, since group 0 and group 1 are then
    joined by the network's only global link and *all* traffic crosses it.
    """
    by_group = _hosts_by_group(topo)
    num_groups = topo.num_groups
    step = []
    for g, hosts in by_group.items():
        target_group = (g + shift) % num_groups
        target_hosts = by_group.get(target_group, [])
        if not target_hosts:
            continue
        for i, h in enumerate(hosts):
            dst = target_hosts[i % len(target_hosts)]
            if dst == h:
                if len(target_hosts) == 1:
                    continue  # only possible target is the sender itself
                dst = target_hosts[(i + 1) % len(target_hosts)]
            step.append((h, dst))
    return [step]


def pattern_nearest_neighbor(topo):
    """Adversarial 'nearest neighbor' traffic: hosts are laid out round-robin
    across groups (group 0's 1st host, group 1's 1st host, ..., group 0's
    2nd host, ...) and every host sends to its immediate successor in that
    ordering, all at once.

    This models a common HPC failure mode: a stencil/ring collective whose
    logical neighbors get placed in different groups by the job scheduler.
    What would normally be the cheapest possible communication pattern (only
    ever talking to the node "next door") turns into a pattern where nearly
    every message needs a global hop, and because the round-robin ordering
    only cycles through a handful of group pairs, that global traffic
    concentrates on just those groups' link(s) instead of spreading out.
    """
    by_group = _hosts_by_group(topo)
    num_groups = topo.num_groups
    max_len = max((len(v) for v in by_group.values()), default=0)
    ordering = []
    for pos in range(max_len):
        for g in range(num_groups):
            hosts = by_group.get(g, [])
            if pos < len(hosts):
                ordering.append(hosts[pos])
    n = len(ordering)
    if n < 2:
        return [[]]
    step = [(ordering[i], ordering[(i + 1) % n]) for i in range(n)]
    return [step]


PATTERNS = {
    "gpaard": pattern_gpaard,
    "uniform": pattern_uniform,
    "all_to_all": pattern_all_to_all,
    "hotspot": pattern_hotspot,
    "shift": pattern_shift,
    "nearest_neighbor": pattern_nearest_neighbor,
}

# Explanatory text shown at the top of each pattern's section in the report.
# Filled in for every pattern so the report always motivates what it measured;
# hotspot and the adversarial patterns get the most detail since they're the
# ones testing worst-case behavior rather than "typical" traffic.
PATTERN_INFO = {
    "gpaard": (
        "g-PAARD's own 6-step reduce-scatter + all-gather all-reduce schedule. "
        "This is the actual workload the routing in this project was built "
        "for, so it is the baseline every other pattern is judged against: "
        "if Dragonfly+ doesn't win here, the extra spine hardware isn't "
        "paying for itself on the workload that matters."
    ),
    "uniform": (
        "Every host sends once to a uniformly random other host, all at "
        "once. A light, evenly-spread control pattern -- it rarely "
        "concentrates load on any single link or endpoint, so it shows "
        "roughly how the two topologies perform when nothing adversarial "
        "is happening."
    ),
    "all_to_all": (
        "Every host sends to every other host, all at once. The heaviest "
        "'fair' pattern: aggregate load is maximal, but because every host "
        "talks to every other host it is symmetric across the fabric, so "
        "no single link or router is picked out over the others."
    ),
    "hotspot": (
        "Every host sends to one randomly chosen target host, all at once "
        "(a worst-case incast). This is interesting to test here because it "
        "attacks a different weak point than the fabric-side adversarial "
        "patterns below: no matter how many parallel global links or spine "
        "switches sit upstream giving the network path diversity, the last "
        "hop into the target's own router/leaf is single-width, so hotspot "
        "exercises the *endpoint* bottleneck rather than the *fabric* "
        "bottleneck. It models real collectives that do this on purpose "
        "(a reduce-to-one, a parameter-server pull, a straggler retry storm) "
        "and shows whether Dragonfly+'s extra global bandwidth helps at all "
        "once the constraint has moved off the fabric and onto one machine's "
        "NIC."
    ),
    "shift": (
        "Adversarial ring-shift traffic (see 'Adversarial traffic' above): "
        "every host in group g sends to the matching host in the next group "
        "over, so 100% of each group's egress bandwidth is forced onto the "
        "single global link connecting the two groups -- simultaneously, on "
        "every group-pair edge around the ring. Unlike hotspot, the "
        "bottleneck here is squarely on the *fabric*, not any one endpoint: "
        "it directly tests whether Dragonfly+'s extra spine-layer paths and "
        "leaf/spine link fan-out give it real headroom under a link that is "
        "*provably* the only route between two groups, or whether both "
        "topologies choke equally once shortest-path routing has no "
        "alternative to offer."
    ),
    "nearest_neighbor": (
        "Adversarial nearest-neighbor traffic (see 'Adversarial traffic' "
        "above): hosts are ordered round-robin across groups and each sends "
        "to its immediate successor, so an application pattern that *should* "
        "be nearly free (talk only to your neighbor) instead forces almost "
        "every message across a global link. This is worth testing because "
        "it is a realistic failure mode, not a synthetic worst case -- job "
        "placement that ignores the physical topology turns cheap stencil/"
        "ring communication into fabric-bound traffic, and this pattern "
        "measures exactly how expensive that placement mistake is on each "
        "topology."
    ),
}

# Canonical ordering for the report: real workload first, then the
# 'typical'/control patterns, then hotspot (endpoint worst case), then the
# fabric-targeted adversarial patterns (the newest addition).
PATTERN_REPORT_ORDER = [
    "gpaard", "uniform", "all_to_all", "hotspot", "shift", "nearest_neighbor",
]


# ---------------------------------------------------------------------------
# Size parsing
# ---------------------------------------------------------------------------
def parse_size(text):
    """Parse a size string like '64K', '1M', '512' (bytes) into an int byte count."""
    text = text.strip().upper()
    multiplier = 1
    if text.endswith("K"):
        multiplier, text = 1024, text[:-1]
    elif text.endswith("M"):
        multiplier, text = 1024 * 1024, text[:-1]
    elif text.endswith("G"):
        multiplier, text = 1024 * 1024 * 1024, text[:-1]
    return int(float(text) * multiplier)


def parse_int_or_list(text):
    """Parse '3' -> 3, or '2,4,3,2' -> [2, 4, 3, 2] (asymmetric per-group counts,
    matching what DragonflyTopo/DragonflyPlusTopo's routers/leaves/spines/hosts
    args already accept)."""
    text = text.strip()
    if "," in text:
        return [int(x) for x in text.split(",")]
    return int(text)


def format_size(num_bytes):
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):g}M"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:g}K"
    return f"{num_bytes}B"


# ---------------------------------------------------------------------------
# Step execution: real iperf transfers over the live Mininet network
# ---------------------------------------------------------------------------
class PortAllocator:
    """Hands out ever-increasing TCP ports so no port is reused within a
    single network's lifetime (avoids TIME_WAIT bind issues between runs)."""

    def __init__(self, start=6000):
        self._next = start

    def take(self, count):
        ports = list(range(self._next, self._next + count))
        self._next += count
        return ports


def run_step(net, step, size_bytes, ports, timeout):
    """Run one step's messages concurrently over real iperf, return elapsed seconds.

    Each message gets its own port (even repeated destinations, e.g. g-PAARD's
    fan-in collector steps) so concurrent flows to the same host don't collide.
    """
    if not step:
        return 0.0

    servers = []
    start = time.time()
    try:
        for (_src, dst), port in zip(step, ports):
            dst_node = net.get(dst)
            servers.append(dst_node.popen(["iperf", "-s", "-p", str(port)]))
        time.sleep(0.15)  # let servers bind before clients connect

        clients = []
        for (src, dst), port in zip(step, ports):
            src_node = net.get(src)
            dst_ip = net.get(dst).IP()
            clients.append(src_node.popen(
                ["iperf", "-c", dst_ip, "-p", str(port), "-n", str(size_bytes)]))

        for client in clients:
            try:
                client.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                client.kill()
                client.communicate()
        return time.time() - start
    finally:
        for server in servers:
            server.terminate()
        for server in servers:
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def run_pattern(net, steps, size_bytes, port_allocator, timeout):
    """Run every step of a pattern in order, return (total_time, per_step_times)."""
    step_times = []
    for step in steps:
        ports = port_allocator.take(len(step))
        step_times.append(run_step(net, step, size_bytes, ports, timeout))
    return sum(step_times), step_times


# ---------------------------------------------------------------------------
# Benchmark driver
# ---------------------------------------------------------------------------
def build_topology_args(topology, groups, routers, leaves, spines, hosts, bw, delay, global_links):
    return Namespace(
        topology=topology, groups=groups, routers=routers, leaves=leaves,
        spines=spines, hosts=hosts, bw=bw, delay=delay, global_links=global_links,
    )


def run_benchmark_for_topology(topology, args, results):
    info(f"\n*** [benchmark] building topology={topology}\n")
    cleanup()
    net_args = build_topology_args(
        topology, args.groups, args.routers, args.leaves, args.spines,
        args.hosts, args.bw, args.delay, args.global_links)
    net = build_network(net_args)
    topo = net.topo

    net.start()
    try:
        net.staticArp()
        install_gpaard_routing(net)
        info(f"*** [benchmark] settling {args.settle}s before sanity pingAll\n")
        time.sleep(args.settle)
        loss = net.pingAll()
        if loss != 0.0:
            raise RuntimeError(
                f"{topology}: pingAll reported {loss:.1f}% packet loss -- "
                "routing is broken, aborting before running iperf")

        num_hosts = len(topo.host_map)
        num_groups = topo.num_groups
        topo_label = topo.topology_label()
        port_allocator = PortAllocator()

        for pattern_name in args.patterns:
            steps = PATTERNS[pattern_name](topo)
            num_messages = sum(len(step) for step in steps)
            for size_bytes in args.sizes:
                run_times = []
                run_step_times = None
                for _rep in range(args.repeat):
                    total_time, step_times = run_pattern(
                        net, steps, size_bytes, port_allocator, args.timeout)
                    run_times.append(total_time)
                    run_step_times = step_times
                mean_time = sum(run_times) / len(run_times)
                total_bytes = num_messages * size_bytes
                throughput_mbps = (total_bytes * 8 / mean_time / 1e6) if mean_time > 0 else 0.0
                info(f"*** [benchmark] {topology:11s} {pattern_name:10s} "
                     f"size={format_size(size_bytes):>6s} -> {mean_time:.3f}s "
                     f"({num_messages} msgs, {throughput_mbps:.2f} Mbit/s aggregate)\n")
                results.append({
                    "topology": topology,
                    "topology_label": topo_label,
                    "num_hosts": num_hosts,
                    "num_groups": num_groups,
                    "pattern": pattern_name,
                    "num_steps": len(steps),
                    "num_messages": num_messages,
                    "message_size_bytes": size_bytes,
                    "repeat": args.repeat,
                    "run_times_s": run_times,
                    "mean_time_s": mean_time,
                    "step_times_s": run_step_times,
                    "aggregate_throughput_mbps": throughput_mbps,
                })
    finally:
        net.stop()


def _wrap(text, width=78, indent=""):
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _write_pattern_section(lines, pattern_name, pattern_results):
    """Emit one self-contained section for a single traffic pattern: its
    'why test this' blurb, its results table, its Dragonfly-vs-Dragonfly+
    speedup, and its per-step timings -- everything needed to read this
    pattern's story without cross-referencing any other section."""
    title = f"PATTERN: {pattern_name}"
    lines.append("=" * 70)
    lines.append(title)
    lines.append("=" * 70)
    blurb = PATTERN_INFO.get(pattern_name)
    if blurb:
        lines.append(_wrap(blurb))
        lines.append("")

    header = f"{'topology':11s} {'msg size':>9s} {'steps':>6s} {'msgs':>6s} {'time(s)':>10s} {'Mbit/s':>10s}"
    lines.append(header)
    lines.append("-" * len(header))
    for r in pattern_results:
        lines.append(
            f"{r['topology']:11s} {format_size(r['message_size_bytes']):>9s} "
            f"{r['num_steps']:>6d} {r['num_messages']:>6d} {r['mean_time_s']:>10.3f} "
            f"{r['aggregate_throughput_mbps']:>10.2f}")

    lines.append("")
    lines.append("Dragonfly vs Dragonfly+ speedup (dragonfly time / dragonfly+ time, >1 means Dragonfly+ faster)")
    lines.append("-" * 70)
    by_key = {(r["topology"], r["message_size_bytes"]): r["mean_time_s"] for r in pattern_results}
    seen_sizes = []
    for r in pattern_results:
        if r["message_size_bytes"] not in seen_sizes:
            seen_sizes.append(r["message_size_bytes"])
    any_speedup = False
    for size_bytes in seen_sizes:
        t_df = by_key.get(("dragonfly", size_bytes))
        t_dfp = by_key.get(("dragonfly+", size_bytes))
        if t_df is not None and t_dfp is not None and t_dfp > 0:
            any_speedup = True
            lines.append(f"{format_size(size_bytes):>9s}  speedup={t_df / t_dfp:.3f}  "
                         f"(dragonfly={t_df:.3f}s, dragonfly+={t_dfp:.3f}s)")
    if not any_speedup:
        lines.append("(need both dragonfly and dragonfly+ results at a common size to compute speedup)")

    lines.append("")
    lines.append("Per-step timings")
    lines.append("-" * 70)
    for r in pattern_results:
        step_str = ", ".join(f"{t:.3f}" for t in r["step_times_s"])
        lines.append(f"{r['topology']:11s} {format_size(r['message_size_bytes']):>9s}: [{step_str}]")
    lines.append("")


IDEAS_FOR_FURTHER_TESTING = """\
Ideas for further adversarial testing (not yet implemented)

- Single transit-group congestion: shift/nearest_neighbor above saturate a
  single global *link*, but DragonflyTopo/DragonflyPlusTopo currently wire
  every group directly to every other group (a complete graph over groups),
  so shortest-path routing never needs a two-hop, transit-through-a-third-
  -group path -- there is nothing to force onto a "transit group" yet. A
  true transit-group adversarial test needs a sparser inter-group wiring
  (e.g. only neighboring groups in a ring/mesh get direct global links, as
  in large-scale Dragonfly deployments where a full inter-group mesh stops
  being economical), plus routing that can actually take a 2-hop path. Once
  that topology variant exists, the matching pattern is easy: pick two
  groups with no direct link and send all their mutual traffic through it,
  hammering whichever group sits in the middle.
- Link/switch failure combined with adversarial traffic: drop one global
  link out of a group pair that already has global_links_per_pair > 1 mid-run
  and re-measure shift/hotspot, to see how routing (and Dragonfly+'s extra
  spine paths specifically) degrades under combined congestion + failure
  rather than either alone.
- Adaptive vs oblivious routing comparison: run the same adversarial
  patterns once with the current fixed shortest-path routing and once with
  a Valiant/UGAL-style load-balanced alternative, to quantify how much of
  the adversarial penalty is inherent to the topology vs an artifact of
  always taking the same static path.
- Asymmetric Dragonfly+ spine stress: target the shift pattern specifically
  at the spine layer (rather than group-to-group) to see whether an
  imbalanced leaf/spine fan-out (--leaves != --spines) creates its own
  single-switch hotspot independent of the global-link one.
"""


def write_results(results, out_path, args):
    lines = []
    lines.append("g-PAARD Dragonfly vs Dragonfly+ benchmark results")
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"topology params: groups={args.groups} routers={args.routers} "
                 f"leaves={args.leaves} spines={args.spines} hosts={args.hosts} "
                 f"bw={args.bw}Mbit delay={args.delay} global_links={args.global_links}")
    lines.append(f"patterns: {', '.join(args.patterns)}")
    lines.append(f"message sizes: {', '.join(format_size(s) for s in args.sizes)}")
    lines.append(f"repeat: {args.repeat}")
    lines.append("")

    by_pattern = {}
    for r in results:
        by_pattern.setdefault(r["pattern"], []).append(r)

    ordered_patterns = [p for p in PATTERN_REPORT_ORDER if p in by_pattern]
    ordered_patterns += [p for p in by_pattern if p not in ordered_patterns]

    for pattern_name in ordered_patterns:
        _write_pattern_section(lines, pattern_name, by_pattern[pattern_name])

    if {"shift", "nearest_neighbor"} & set(by_pattern):
        lines.append("=" * 70)
        lines.append(IDEAS_FOR_FURTHER_TESTING)

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    info(f"\n*** [benchmark] results written to {out_path}\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Benchmark g-PAARD and synthetic traffic patterns on Dragonfly vs Dragonfly+ in Mininet.")
    parser.add_argument("--topologies", type=str, default="dragonfly,dragonfly+",
                        help="comma-separated topologies to test (default: dragonfly,dragonfly+)")
    parser.add_argument("--patterns", type=str,
                        default="gpaard,uniform,all_to_all,hotspot,shift,nearest_neighbor",
                        help="comma-separated traffic patterns: "
                             f"{', '.join(sorted(PATTERNS))} (default: all of them; "
                             "shift/nearest_neighbor are adversarial patterns that "
                             "concentrate traffic onto a single global link)")
    parser.add_argument("--sizes", type=str, default="4K,64K,1M,8M",
                        help="comma-separated message sizes, e.g. '4K,64K,1M,8M' (default: 4K,64K,1M,8M)")
    parser.add_argument("-g", "--groups", type=int, default=3, help="number of groups (default: 3)")
    parser.add_argument("-r", "--routers", type=str, default="2",
                        help="routers per group, dragonfly only: an int, or a comma-separated "
                             "per-group list e.g. '2,4,3,2' for asymmetric groups (default: 2)")
    parser.add_argument("--leaves", type=str, default="2",
                        help="leaves per group, dragonfly+ only: int or comma-separated per-group list (default: 2)")
    parser.add_argument("--spines", type=str, default="2",
                        help="spines per group, dragonfly+ only: int or comma-separated per-group list (default: 2)")
    parser.add_argument("-H", "--hosts", type=str, default="1",
                        help="hosts per router/leaf: int or comma-separated per-group list (default: 1)")
    parser.add_argument("--bw", type=float, default=10, help="link bandwidth in Mbit/s (default: 10)")
    parser.add_argument("--delay", type=str, default="1ms", help="per-link delay (default: 1ms)")
    parser.add_argument("--global-links", type=int, default=1, help="global links per group pair (default: 1)")
    parser.add_argument("--settle", type=int, default=1, help="seconds to wait after installing flows (default: 1)")
    parser.add_argument("--repeat", type=int, default=1, help="repetitions per (pattern, size) to average (default: 1)")
    parser.add_argument("--timeout", type=int, default=60, help="per-message iperf timeout in seconds (default: 60)")
    parser.add_argument("--out", type=str, default="benchmark_results.txt", help="results file to write (default: benchmark_results.txt)")
    args = parser.parse_args(argv)

    args.topologies = [t.strip() for t in args.topologies.split(",") if t.strip()]
    args.patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]
    for p in args.patterns:
        if p not in PATTERNS:
            parser.error(f"unknown pattern '{p}', choose from: {', '.join(sorted(PATTERNS))}")
    args.sizes = [parse_size(s) for s in args.sizes.split(",") if s.strip()]
    args.routers = parse_int_or_list(args.routers)
    args.leaves = parse_int_or_list(args.leaves)
    args.spines = parse_int_or_list(args.spines)
    args.hosts = parse_int_or_list(args.hosts)
    return args


def main():
    args = parse_args()  # handles --help before the root check below

    if os.geteuid() != 0:
        print("benchmark_gpaard.py must run as root (Mininet needs to manipulate "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    setLogLevel("info")

    results = []
    try:
        for topology in args.topologies:
            run_benchmark_for_topology(topology, args, results)
    finally:
        if results:
            write_results(results, args.out, args)
        else:
            info("*** [benchmark] no results collected, nothing written\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
