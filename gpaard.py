#!/usr/bin/env python3
"""
gpaard.py

g-PAARD (generalized Proximity-Aware All-Reduce for Dragonfly), from
Ma et al., "Evaluation of Topology-Aware All-Reduce Algorithm for Dragonfly
Networks" (NPC 2021 / LNCS 13152).

g-PAARD is an ALL-REDUCE COMMUNICATION PATTERN, not a routing algorithm: it
decides which hosts send what to which other hosts, and in what order, to
complete an all-reduce in 6 steps (a 3-step reduce-scatter mirrored by a
3-step all-gather) by exploiting Dragonfly's group structure. It runs on top
of whatever unicast routing is already installed (dragonfly_topo.py's
install_gpaard_routing() shortest-path flows) -- it does not change routing
at all, which is why it lives in its own module.

Algorithm, for a canonical Dragonfly with g groups where every group has a
direct global link to every other group (true for DragonflyTopo regardless
of its parameters -- see dragonfly_topo.py's _add_global_links):

Each host's data is conceptually split into g shards, one per group; shard j
is destined to end up fully reduced and evenly held across group j's hosts.

Reduce-scatter:
  Step 1 (intra-group, 1 hop): for every group g and every OTHER group j,
    every host in g sends its native shard-j fragment to g's one designated
    "global node" for reaching j (the host on the switch carrying g's
    global link to j). That host ends up holding group g's local sum of
    shard j.
  Step 2 (inter-group, 1 hop over the pre-existing global link): for every
    group pair {g1, g2}, their mutually-designated global nodes exchange
    local sums directly and simultaneously (g1's node sends its local sum
    of shard g2, and vice versa).
  Step 3 (intra-group): group g now finishes shard g -- the (num_groups-1)
    partial sums it just received (one per remote group, each at a
    different designated host) plus its own native shard-g fragments
    (spread across every host in g) are combined via a full any-to-any
    exchange, so every host in g ends up holding an even slice of the fully
    reduced shard g.

All-gather mirrors this in reverse (broadcast instead of reduce):
  Step 4 = reverse of step 3 (every host in g gathers all of shard g).
  Step 5 = reverse of step 2 (designated nodes exchange the now-complete
    shards across the same global links).
  Step 6 = reverse of step 1 (designated nodes broadcast the shard they
    just received to the rest of their group).

After step 6 every host holds every shard: the complete all-reduced result.

Scope: this module assumes canonical Dragonfly's all-to-all intra-group
mesh (steps 1/3 need only 1 hop between any two hosts' routers in a group).
Dragonfly+'s leaf-spine fabric needs 2 hops (leaf -> spine -> leaf) for the
same intra-group exchange, so the schedule stays the same but the role
selection and cost model need to respect that topology. This module now
supports both Dragonfly and Dragonfly+ by selecting a host representative per
group in the Dragonfly+ case and preserving the same 6-step pattern.
"""


def _switch_group_map(topo):
    """switch_name -> group_id, derived from topo.router_map."""
    return {sw_name: group_id for (group_id, _router_id), sw_name in topo.router_map.items()}


def _hosts_by_switch(topo):
    """switch_name -> sorted list of host names attached to it."""
    switch_of = {(group_id, router_id): sw_name for (group_id, router_id), sw_name in topo.router_map.items()}
    by_switch = {sw_name: [] for sw_name in topo.router_map.values()}
    for host_name, (group_id, router_id, _host_id) in topo.host_map.items():
        by_switch[switch_of[(group_id, router_id)]].append(host_name)
    for hosts in by_switch.values():
        hosts.sort()
    return by_switch


def group_hosts(topo):
    """group_id -> sorted list of host names in that group."""
    groups = {g: [] for g in range(topo.num_groups)}
    for host_name, (group_id, _router_id, _host_id) in topo.host_map.items():
        groups[group_id].append(host_name)
    for hosts in groups.values():
        hosts.sort()
    return groups


def _designated_global_switch(topo):
    """(g1, g2) -> the switch in group g1 carrying g-PAARD's designated
    direct global link to group g2. Picks one deterministically (the first
    found in sorted link order) when global_links_per_pair > 1 offers
    several parallel links between the same two groups -- g-PAARD uses
    exactly one direct hop per group pair, not all of them.
    """
    switch_group = _switch_group_map(topo)
    chosen = {}
    for n1, n2 in sorted(topo.links()):
        if n1 not in switch_group or n2 not in switch_group:
            continue  # a host link, not a switch-switch link
        g1, g2 = switch_group[n1], switch_group[n2]
        if g1 == g2:
            continue  # a local (intra-group) link, not global
        chosen.setdefault((g1, g2), n1)
        chosen.setdefault((g2, g1), n2)

    missing = [(g1, g2) for g1 in range(topo.num_groups) for g2 in range(topo.num_groups)
               if g1 != g2 and (g1, g2) not in chosen]
    if missing:
        raise ValueError(
            "g-PAARD requires a direct global link between every pair of groups "
            f"(canonical Dragonfly guarantees this); missing pairs: {missing}")
    return chosen


def designate_roles(topo):
    """(g1, g2) -> the host in group g1 designated to talk to group g2: it
    collects g1's local sum of shard g2 in step 1, exchanges it directly
    with group g2's mirror host over their shared global link in step 2,
    and receives group g2's local sum of shard g1 in return.
    """
    for group_id, hosts in group_hosts(topo).items():
        if not hosts:
            raise ValueError(f"g-PAARD needs at least 1 host in every group; group {group_id} has none")

    designated_switch = _designated_global_switch(topo)
    hosts_by_switch = _hosts_by_switch(topo)
    hosts_by_group = group_hosts(topo)
    role_host = {}
    for (g1, g2), sw in designated_switch.items():
        hosts = hosts_by_switch.get(sw, [])
        if hosts:
            role_host[(g1, g2)] = hosts[0]
            continue

        if hasattr(topo, "spine_map"):
            role_host[(g1, g2)] = hosts_by_group[g1][0]
            continue

        raise ValueError(
            f"g-PAARD needs at least 1 host on switch {sw} (group {g1}'s designated "
            f"link to group {g2}) to act as its global-node role for that pair")
    return role_host


def build_gpaard_schedule(topo):
    """Compute the 6-step g-PAARD message schedule for `topo`.

    Returns {"reduce_scatter": [step1, step2, step3], "all_gather": [step4, step5, step6]},
    where each step is a list of (src_host, dst_host, shard) messages that
    all occur simultaneously within that step (shard is a group id 0..num_groups-1).
    """
    num_groups = topo.num_groups
    groups = group_hosts(topo)
    role_host = designate_roles(topo)

    # Step 1: every host in group g sends its native shard-j fragment to
    # g's designated collector for reaching group j (for every other j).
    step1 = []
    for g in range(num_groups):
        for j in range(num_groups):
            if j == g:
                continue
            collector = role_host[(g, j)]
            for host in groups[g]:
                if host != collector:
                    step1.append((host, collector, j))

    # Step 2: each group pair's mutually-designated hosts exchange local
    # sums directly over their shared global link, once per direction.
    step2 = []
    seen_pairs = set()
    for g1 in range(num_groups):
        for g2 in range(num_groups):
            if g1 == g2 or frozenset((g1, g2)) in seen_pairs:
                continue
            seen_pairs.add(frozenset((g1, g2)))
            step2.append((role_host[(g1, g2)], role_host[(g2, g1)], g2))
            step2.append((role_host[(g2, g1)], role_host[(g1, g2)], g1))

    # Step 3: within each group, a full any-to-any exchange finishes
    # reducing that group's own shard across all of its hosts.
    step3 = []
    for g in range(num_groups):
        hosts = groups[g]
        for src in hosts:
            for dst in hosts:
                if src != dst:
                    step3.append((src, dst, g))

    reduce_scatter = [step1, step2, step3]
    # All-gather mirrors reduce-scatter exactly in reverse: same pairs and
    # shard labels, opposite direction and opposite step order (step 4
    # mirrors step 3, step 5 mirrors step 2, step 6 mirrors step 1).
    all_gather = [
        [(dst, src, shard) for (src, dst, shard) in step3],
        [(dst, src, shard) for (src, dst, shard) in step2],
        [(dst, src, shard) for (src, dst, shard) in step1],
    ]

    return {"reduce_scatter": reduce_scatter, "all_gather": all_gather}


def simulate_allreduce(topo):
    """Pure-Python correctness check for the g-PAARD schedule.

    Each host's per-shard data is represented as a frozenset of contributor
    host names (provenance tracking) instead of real numbers -- a message
    "reduces" by unioning the sender's set into the receiver's, and
    "broadcasts" by copying it. After running the full schedule, every host
    must hold, for every shard, the set of ALL hosts in the topology --
    otherwise the all-reduce is wrong (data lost, duplicated, or never
    delivered). Raises AssertionError with details if not.

    Returns the final {host: {shard: frozenset(contributors)}} state.
    """
    schedule = build_gpaard_schedule(topo)
    groups = group_hosts(topo)
    num_groups = topo.num_groups
    all_hosts = [h for hosts in groups.values() for h in hosts]

    # Every host starts as the sole contributor to each of its own g shards.
    state = {h: {j: frozenset({h}) for j in range(num_groups)} for h in all_hosts}

    def apply_reduce(step):
        incoming = {}
        for src, dst, shard in step:
            incoming.setdefault((dst, shard), set()).add(src)
        updates = {}
        for (dst, shard), srcs in incoming.items():
            merged = state[dst][shard]
            for src in srcs:
                merged = merged | state[src][shard]
            updates[(dst, shard)] = merged
        for (dst, shard), merged in updates.items():
            state[dst][shard] = merged

    def apply_broadcast(step):
        updates = {}
        for src, dst, shard in step:
            updates[(dst, shard)] = state[src][shard]
        for (dst, shard), value in updates.items():
            state[dst][shard] = value

    for step in schedule["reduce_scatter"]:
        apply_reduce(step)
    for step in schedule["all_gather"]:
        apply_broadcast(step)

    expected = frozenset(all_hosts)
    for host in all_hosts:
        for shard in range(num_groups):
            if state[host][shard] != expected:
                missing = expected - state[host][shard]
                extra = state[host][shard] - expected
                raise AssertionError(
                    f"host {host} shard {shard} incomplete after all-reduce: "
                    f"missing={sorted(missing)} extra={sorted(extra)}")

    return state
