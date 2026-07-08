#!/usr/bin/env python3
"""
test_gpaard.py

Pytest suite for gpaard.py -- verifies the g-PAARD all-reduce schedule is
structurally correct (intra-group steps stay intra-group, inter-group steps
ride a real global link) and functionally correct (every host ends up with
every other host's contribution after the full 6-step schedule), across
several DragonflyTopo configurations. Also checks the Dragonfly+ adaptation
and a couple of guard rails (topologies g-PAARD can't run on at all).

    pytest test_gpaard.py
"""

import pytest

from dragonfly_topo import DragonflyTopo
from dragonfly_plus_topo import DragonflyPlusTopo
from gpaard import build_gpaard_schedule, simulate_allreduce, group_hosts, _designated_global_switch


def test_schedule_message_counts_match_paper_example():
    # Paper's worked example: 32 nodes in 4 groups of 8 (DF(2,4,2)) -- "6
    # global links... 6 pairs of nodes can communicate" in step 2, since
    # C(4,2) = 6 group pairs, one link used in each direction.
    topo = DragonflyTopo(num_groups=4, routers_per_group=4, hosts_per_router=2, global_links_per_pair=1)
    schedule = build_gpaard_schedule(topo)
    step1, step2, step3 = schedule["reduce_scatter"]

    assert len(step2) == 2 * 6  # 6 group pairs, 2 directions each

    # Step 1: every host except the collector sends once per (group, other
    # group) -- (hosts_per_group - 1) senders x (num_groups - 1) destinations,
    # for each of the 4 groups.
    hosts_per_group = 4 * 2
    assert len(step1) == 4 * (hosts_per_group - 1) * (4 - 1)

    # Step 3: a full any-to-any exchange within each of the 4 groups.
    assert len(step3) == 4 * hosts_per_group * (hosts_per_group - 1)


def test_full_allreduce_correctness_symmetric():
    topo = DragonflyTopo(num_groups=4, routers_per_group=3, hosts_per_router=2, global_links_per_pair=1)
    simulate_allreduce(topo)  # raises AssertionError on any incorrect/incomplete result


def test_full_allreduce_correctness_with_multiple_global_links():
    topo = DragonflyTopo(num_groups=5, routers_per_group=4, hosts_per_router=1, global_links_per_pair=3)
    simulate_allreduce(topo)


def test_full_allreduce_correctness_asymmetric_groups():
    topo = DragonflyTopo(num_groups=5, routers_per_group=[2, 4, 3, 2, 5], hosts_per_router=[1, 2, 1, 3, 1])
    simulate_allreduce(topo)


def test_single_group_is_trivial_all_to_all():
    topo = DragonflyTopo(num_groups=1, routers_per_group=4, hosts_per_router=2)
    schedule = build_gpaard_schedule(topo)
    step1, step2, step3 = schedule["reduce_scatter"]

    assert step1 == []  # no other groups to collect for
    assert step2 == []  # no group pairs to exchange across
    hosts_per_group = 4 * 2
    assert len(step3) == hosts_per_group * (hosts_per_group - 1)

    simulate_allreduce(topo)


def test_step1_and_step3_messages_stay_within_one_group():
    topo = DragonflyTopo(num_groups=4, routers_per_group=3, hosts_per_router=2)
    host_group = {h: g for g, hosts in group_hosts(topo).items() for h in hosts}
    schedule = build_gpaard_schedule(topo)

    for step in (schedule["reduce_scatter"][0], schedule["reduce_scatter"][2],
                 schedule["all_gather"][0], schedule["all_gather"][2]):
        for src, dst, _shard in step:
            assert host_group[src] == host_group[dst]


def test_step2_messages_cross_a_real_global_link():
    # Each step-2 message's src/dst hosts must sit on switches that are
    # DIRECTLY linked in the real topology -- the paper's "only one hop"
    # claim for the inter-group step, checked at the switch/router level
    # (hops are counted between routers, not literally host-to-host).
    topo = DragonflyTopo(num_groups=4, routers_per_group=3, hosts_per_router=2)
    switch_of_group_router = {(g, r): sw for (g, r), sw in topo.router_map.items()}
    switch_of_host = {
        host: switch_of_group_router[(g, r)]
        for host, (g, r, _h) in topo.host_map.items()
    }
    real_links = set(topo.links()) | {(b, a) for a, b in topo.links()}

    schedule = build_gpaard_schedule(topo)
    for step in (schedule["reduce_scatter"][1], schedule["all_gather"][1]):
        for src, dst, _shard in step:
            sw1, sw2 = switch_of_host[src], switch_of_host[dst]
            assert (sw1, sw2) in real_links, f"{sw1}<->{sw2} is not a direct link"


def test_dragonfly_plus_allreduce_correctness():
    topo = DragonflyPlusTopo(num_groups=2, leaves_per_group=2, spines_per_group=2, hosts_per_leaf=1)
    schedule = build_gpaard_schedule(topo)
    step1, step2, step3 = schedule["reduce_scatter"]

    assert len(step2) == 2  # one group pair, two directions
    assert len(step1) == 2  # one sender per group, one remote destination
    assert len(step3) == 4  # one local exchange in each direction per group

    simulate_allreduce(topo)


def test_dragonfly_plus_step2_messages_use_global_spines():
    topo = DragonflyPlusTopo(num_groups=3, leaves_per_group=2, spines_per_group=2, hosts_per_leaf=1)
    designated_switch = _designated_global_switch(topo)
    real_links = set(topo.links()) | {(b, a) for a, b in topo.links()}

    # Every pair of groups should map to directly connected spine routers.
    for g1 in range(topo.num_groups):
        for g2 in range(g1 + 1, topo.num_groups):
            sw1 = designated_switch[(g1, g2)]
            sw2 = designated_switch[(g2, g1)]
            assert (sw1, sw2) in real_links, f"{sw1}<->{sw2} is not a direct global link"


def test_raises_when_a_group_has_no_hosts():
    topo = DragonflyTopo(num_groups=3, routers_per_group=2, hosts_per_router=[1, 0, 1])
    with pytest.raises(ValueError, match="no hosts|at least 1 host"):
        build_gpaard_schedule(topo)
