#!/usr/bin/env python3
"""
verify_gpaard_correctness.py

Standalone correctness-verification environment for g-PAARD (gpaard.py) on
both Dragonfly topologies -- DragonflyTopo (canonical) and DragonflyPlusTopo
(leaf-spine) -- without needing Mininet or root privileges.

Unlike benchmark_gpaard.py (which measures real timing over a live Mininet
network and needs sudo), this only checks CORRECTNESS: does the g-PAARD
6-step schedule actually deliver every host's contribution to every other
host (via gpaard.py's own simulate_allreduce()), and does it respect the
structural guarantees the algorithm depends on -- intra-group steps (1, 3,
4, 6) never leave the group, and the inter-group step (2, 5) always rides a
real, direct global link? Runs both checks across a battery of curated and
randomized topology configurations for both topologies.

Run:
    python3 verify_gpaard_correctness.py

Writes a full pass/fail report to gpaard_correctness_report.txt when done.
"""

import random
import time

from dragonfly_topo import DragonflyTopo
from dragonfly_plus_topo import DragonflyPlusTopo
from gpaard import build_gpaard_schedule, simulate_allreduce, _designated_global_switch


def check_structural(topo):
    """Re-checks the two structural invariants g-PAARD depends on:
    (1) step1/step3 (and their all-gather mirrors, step4/step6) never leave
        a group;
    (2) step2 (and its mirror, step5) always rides a real, direct global
        link -- checked at the level of each group pair's DESIGNATED
        switches (_designated_global_switch), not the sending hosts' own
        switches: on canonical Dragonfly those coincide (the designated
        host sits right on the global-link router), but on Dragonfly+ the
        designated host is a leaf representative one hop away from the
        spine that actually carries the global link, so comparing hosts'
        own switches directly would be the wrong (and stricter-than-real)
        invariant.
    Returns a list of violation strings (empty if everything holds)."""
    violations = []
    schedule = build_gpaard_schedule(topo)
    host_group = {h: g for h, (g, _r, _hid) in topo.host_map.items()}
    real_links = set(topo.links()) | {(b, a) for a, b in topo.links()}

    intra_group_steps = [
        ("reduce_scatter[1]", schedule["reduce_scatter"][0]),
        ("reduce_scatter[3]", schedule["reduce_scatter"][2]),
        ("all_gather[4]", schedule["all_gather"][0]),
        ("all_gather[6]", schedule["all_gather"][2]),
    ]
    for label, step in intra_group_steps:
        for src, dst, _shard in step:
            if host_group[src] != host_group[dst]:
                violations.append(f"{label}: {src}->{dst} crosses groups (should be intra-group)")

    designated_switch = _designated_global_switch(topo)
    inter_group_steps = [
        ("reduce_scatter[2]", schedule["reduce_scatter"][1]),
        ("all_gather[5]", schedule["all_gather"][1]),
    ]
    for label, step in inter_group_steps:
        for src, dst, _shard in step:
            g1, g2 = host_group[src], host_group[dst]
            sw1, sw2 = designated_switch[(g1, g2)], designated_switch[(g2, g1)]
            if (sw1, sw2) not in real_links:
                violations.append(
                    f"{label}: group {g1}->{g2} designated switches {sw1}<->{sw2} are not a direct link")

    return violations


def run_case(name, topo):
    result = {
        "name": name, "topology": type(topo).__name__,
        "num_groups": topo.num_groups, "num_hosts": len(topo.host_map),
    }
    start = time.time()
    try:
        simulate_allreduce(topo)
        result["functional"] = "PASS"
        result["error"] = None
    except (AssertionError, ValueError) as e:
        result["functional"] = "FAIL"
        result["error"] = str(e)

    violations = check_structural(topo)
    result["structural"] = "PASS" if not violations else "FAIL"
    result["violations"] = violations
    result["elapsed_s"] = time.time() - start
    result["overall"] = "PASS" if result["functional"] == "PASS" and result["structural"] == "PASS" else "FAIL"
    return result


def curated_cases():
    """Hand-picked configurations covering the structural edge cases g-PAARD
    needs to get right on each topology."""
    cases = [
        ("tiny symmetric", DragonflyTopo(num_groups=2, routers_per_group=2, hosts_per_router=1)),
        ("medium symmetric", DragonflyTopo(num_groups=4, routers_per_group=3, hosts_per_router=2)),
        ("single group (trivial all-to-all)", DragonflyTopo(num_groups=1, routers_per_group=4, hosts_per_router=2)),
        ("asymmetric groups", DragonflyTopo(num_groups=5, routers_per_group=[2, 4, 3, 2, 5], hosts_per_router=[1, 2, 1, 3, 1])),
        ("multiple global links", DragonflyTopo(num_groups=5, routers_per_group=4, hosts_per_router=1, global_links_per_pair=3)),
        ("minimal 1 router/group", DragonflyTopo(num_groups=6, routers_per_group=1, hosts_per_router=2)),
        ("large scale", DragonflyTopo(num_groups=8, routers_per_group=3, hosts_per_router=2)),
    ]
    cases_plus = [
        ("tiny symmetric", DragonflyPlusTopo(num_groups=2, leaves_per_group=2, spines_per_group=2, hosts_per_leaf=1)),
        ("medium symmetric", DragonflyPlusTopo(num_groups=4, leaves_per_group=3, spines_per_group=2, hosts_per_leaf=2)),
        ("single group (trivial all-to-all)", DragonflyPlusTopo(num_groups=1, leaves_per_group=4, spines_per_group=2, hosts_per_leaf=2)),
        ("asymmetric groups", DragonflyPlusTopo(num_groups=5, leaves_per_group=[2, 4, 3, 2, 5], spines_per_group=[2, 3, 2, 4, 2], hosts_per_leaf=[1, 2, 1, 3, 1])),
        ("multiple global links", DragonflyPlusTopo(num_groups=5, leaves_per_group=3, spines_per_group=3, hosts_per_leaf=1, global_links_per_pair=3)),
        ("more remote groups than leaves (role spreading)", DragonflyPlusTopo(num_groups=6, leaves_per_group=2, spines_per_group=2, hosts_per_leaf=1)),
        ("large scale", DragonflyPlusTopo(num_groups=8, leaves_per_group=3, spines_per_group=2, hosts_per_leaf=2)),
    ]
    return [(f"{n} (DF)", t) for n, t in cases] + [(f"{n} (DF+)", t) for n, t in cases_plus]


def small_cases():
    """A handful of deliberately small configurations, kept tiny enough that
    every single g-PAARD message can be printed in full without the report
    becoming unreadable."""
    df = [
        ("tiny DF", DragonflyTopo(num_groups=2, routers_per_group=2, hosts_per_router=1)),
        ("3-group DF", DragonflyTopo(num_groups=3, routers_per_group=2, hosts_per_router=1)),
        ("single-group DF", DragonflyTopo(num_groups=1, routers_per_group=3, hosts_per_router=1)),
    ]
    dfp = [
        ("tiny DF+", DragonflyPlusTopo(num_groups=2, leaves_per_group=2, spines_per_group=2, hosts_per_leaf=1)),
        ("3-group DF+", DragonflyPlusTopo(num_groups=3, leaves_per_group=2, spines_per_group=2, hosts_per_leaf=1)),
        ("single-group DF+", DragonflyPlusTopo(num_groups=1, leaves_per_group=3, spines_per_group=2, hosts_per_leaf=1)),
    ]
    return df + dfp


# (phase, index within that phase's 3 steps, human label). Index 1 is always
# the inter-group step (checked against designated switches); 0 and 2 are
# always intra-group (checked against host group membership).
STEP_INFO = [
    ("reduce_scatter", 0, "Step 1 - reduce-scatter: intra-group collect (each host sends its "
                          "native shard-j fragment to its group's collector for group j)"),
    ("reduce_scatter", 1, "Step 2 - reduce-scatter: inter-group exchange over the real global link"),
    ("reduce_scatter", 2, "Step 3 - reduce-scatter: intra-group any-to-all (finishes reducing "
                          "this group's own shard)"),
    ("all_gather", 0, "Step 4 - all-gather: intra-group any-to-all (mirrors step 3)"),
    ("all_gather", 1, "Step 5 - all-gather: inter-group exchange (mirrors step 2)"),
    ("all_gather", 2, "Step 6 - all-gather: intra-group broadcast (mirrors step 1)"),
]


def dump_full_schedule(name, topo):
    """Every single message in every one of g-PAARD's 6 steps for one small
    topology, each tagged OK/VIOLATION against the same structural
    invariants check_structural() checks, plus the final functional
    (simulate_allreduce) verdict."""
    lines = []
    schedule = build_gpaard_schedule(topo)
    host_group = {h: g for h, (g, _r, _hid) in topo.host_map.items()}
    real_links = set(topo.links()) | {(b, a) for a, b in topo.links()}
    designated_switch = _designated_global_switch(topo)

    lines.append(f"=== {name} ({type(topo).__name__}) ===")
    lines.append(topo.topology_label())
    lines.append(f"hosts ({len(topo.host_map)}): {', '.join(sorted(topo.host_map))}")
    lines.append("")

    total_messages = 0
    for phase, idx, label in STEP_INFO:
        step = schedule[phase][idx]
        is_intra = idx != 1
        lines.append(f"-- {label} --")
        if not step:
            lines.append("  (no messages)")
        for src, dst, shard in step:
            if is_intra:
                ok = host_group[src] == host_group[dst]
                tag = "OK" if ok else "VIOLATION: crosses groups"
            else:
                g1, g2 = host_group[src], host_group[dst]
                sw1, sw2 = designated_switch[(g1, g2)], designated_switch[(g2, g1)]
                ok = (sw1, sw2) in real_links
                tag = "OK" if ok else f"VIOLATION: {sw1}<->{sw2} not directly linked"
            lines.append(f"  {src} -> {dst}   shard={shard}   [{tag}]")
        lines.append(f"  ({len(step)} messages)")
        lines.append("")
        total_messages += len(step)

    try:
        simulate_allreduce(topo)
        lines.append(f"FUNCTIONAL CHECK: PASS -- all {total_messages} messages together correctly "
                     f"deliver every host's contribution to every host, for every shard.")
    except (AssertionError, ValueError) as e:
        lines.append(f"FUNCTIONAL CHECK: FAIL -- {e}")
    lines.append("")
    return lines


def randomized_cases(num_trials=40, seed=0):
    """Random configurations (group count, per-group asymmetry, global link
    multiplicity) split evenly across both topologies, to catch anything the
    curated cases don't happen to hit."""
    rng = random.Random(seed)
    cases = []
    for i in range(num_trials):
        num_groups = rng.randint(1, 6)
        global_links = rng.randint(1, 3)
        if rng.random() < 0.5:
            topo = DragonflyTopo(
                num_groups=num_groups,
                routers_per_group=[rng.randint(1, 4) for _ in range(num_groups)],
                hosts_per_router=[rng.randint(1, 3) for _ in range(num_groups)],
                global_links_per_pair=global_links)
            cases.append((f"random#{i} (DF)", topo))
        else:
            topo = DragonflyPlusTopo(
                num_groups=num_groups,
                leaves_per_group=[rng.randint(1, 4) for _ in range(num_groups)],
                spines_per_group=[rng.randint(1, 3) for _ in range(num_groups)],
                hosts_per_leaf=[rng.randint(1, 3) for _ in range(num_groups)],
                global_links_per_pair=global_links)
            cases.append((f"random#{i} (DF+)", topo))
    return cases


def build_report(results, curated_count, randomized_count):
    lines = []
    lines.append("g-PAARD correctness verification report")
    lines.append("=" * 82)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"total cases: {len(results)}  (curated: {curated_count}, randomized: {randomized_count})")
    lines.append("checks per case: FUNCTIONAL = simulate_allreduce() (every host ends up with every "
                 "host's contribution); STRUCTURAL = intra-group steps stay in-group, inter-group step "
                 "rides a real direct link")
    lines.append("")

    header = f"{'case':46s} {'topology':17s} {'grp':>4s} {'hosts':>6s} {'func':>6s} {'struct':>7s} {'overall':>8s}"
    lines.append(header)
    lines.append("-" * len(header))
    for r in results:
        lines.append(f"{r['name']:46s} {r['topology']:17s} {r['num_groups']:>4d} {r['num_hosts']:>6d} "
                     f"{r['functional']:>6s} {r['structural']:>7s} {r['overall']:>8s}")

    passed = sum(1 for r in results if r["overall"] == "PASS")
    lines.append("")
    lines.append(f"SUMMARY: {passed}/{len(results)} cases passed")
    if passed == len(results):
        lines.append("g-PAARD is verified CORRECT on both DragonflyTopo and DragonflyPlusTopo "
                     "across every tested configuration.")
    else:
        lines.append("FAILURES DETECTED -- see details below.")

    failures = [r for r in results if r["overall"] == "FAIL"]
    if failures:
        lines.append("")
        lines.append("Failure details")
        lines.append("-" * 82)
        for r in failures:
            lines.append(f"[{r['name']}] ({r['topology']}, groups={r['num_groups']}, hosts={r['num_hosts']})")
            if r["error"]:
                lines.append(f"  functional error: {r['error']}")
            for v in r["violations"]:
                lines.append(f"  structural violation: {v}")
            lines.append("")

    return "\n".join(lines) + "\n"


def main():
    curated = curated_cases()
    randomized = randomized_cases()
    all_cases = curated + randomized
    results = [run_case(name, topo) for name, topo in all_cases]
    report = build_report(results, len(curated), len(randomized))

    report_path = "gpaard_correctness_report.txt"
    with open(report_path, "w") as f:
        f.write(report)

    detail_lines = ["Full step-by-step message detail (small cases)", "=" * 82,
                    "Every message g-PAARD sends in every one of its 6 steps, for a handful of "
                    "deliberately small topologies -- kept small so nothing is elided.", ""]
    for name, topo in small_cases():
        detail_lines.extend(dump_full_schedule(name, topo))
    detail_report = "\n".join(detail_lines) + "\n"

    detail_path = "gpaard_small_case_detail.txt"
    with open(detail_path, "w") as f:
        f.write(detail_report)

    passed = sum(1 for r in results if r["overall"] == "PASS")
    print(f"broad sweep: {passed}/{len(results)} passed -> written to {report_path}")
    print(f"full message detail for {len(small_cases())} small cases -> written to {detail_path}")


if __name__ == "__main__":
    main()
