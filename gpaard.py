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
same intra-group exchange, so the schedule stays the same but role selection
needs to respect that topology: Dragonfly+'s global links attach only to
spine routers, which carry no hosts of their own, so each group-pair's
designated host is instead drawn from a per-group pool of one representative
host per leaf, round-robined across group-pairs. This spreads the global
exchange across the group's leaves instead of funneling every remote group's
traffic through a single host.
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
    direct global link to group g2. chosen[(g1, g2)] and chosen[(g2, g1)]
    always come from the SAME physical link (a real direct connection is
    the whole point -- step 2 rides it), but when global_links_per_pair > 1
    offers several parallel candidate links between the same two groups,
    prefers whichever candidate link has BOTH ends not already designated
    for one of its group's other remote-group pairs, so each group's
    collectors spread across distinct routers/spines instead of piling two
    remote groups onto one host.

    An earlier version of this picked each direction independently
    (g1's and g2's preferred candidate separately), which could pick two
    DIFFERENT physical links for the two directions of the same pair --
    breaking the "step 2 rides a real direct link" invariant (caught by
    verify_gpaard_correctness.py's structural check). Picking one link per
    pair and committing both of its ends together is what keeps that
    invariant while still load-balancing.
    """
    switch_group = _switch_group_map(topo)

    # Collect every candidate link per unordered group pair, each as the
    # (switch_in_lower_group, switch_in_higher_group) tuple -- canonicalized
    # by group id so both ends of one physical link travel together.
    pair_links = {}
    for n1, n2 in topo.links():
        if n1 not in switch_group or n2 not in switch_group:
            continue  # a host link, not a switch-switch link
        g1, g2 = switch_group[n1], switch_group[n2]
        if g1 == g2:
            continue  # a local (intra-group) link, not global
        key, link = (g1, g2), (n1, n2)
        if g1 > g2:
            key, link = (g2, g1), (n2, n1)
        pair_links.setdefault(key, []).append(link)

    missing = [(g1, g2) for g1 in range(topo.num_groups) for g2 in range(g1 + 1, topo.num_groups)
               if (g1, g2) not in pair_links]
    if missing:
        raise ValueError(
            "g-PAARD requires a direct global link between every pair of groups "
            f"(canonical Dragonfly guarantees this); missing pairs: {missing}")

    chosen = {}
    used_by_group = {g: set() for g in range(topo.num_groups)}
    for g_lo in range(topo.num_groups):
        for g_hi in range(g_lo + 1, topo.num_groups):
            links = pair_links[(g_lo, g_hi)]
            # Prefer a link free on BOTH ends; failing that, free on g_lo's
            # end; failing that, just the first candidate (unavoidable once
            # a group has more remote pairs than routers/spines).
            pick = (next((lk for lk in links
                        if lk[0] not in used_by_group[g_lo] and lk[1] not in used_by_group[g_hi]), None)
                   or next((lk for lk in links if lk[0] not in used_by_group[g_lo]), links[0]))
            sw_lo, sw_hi = pick
            chosen[(g_lo, g_hi)] = sw_lo
            chosen[(g_hi, g_lo)] = sw_hi
            used_by_group[g_lo].add(sw_lo)
            used_by_group[g_hi].add(sw_hi)
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
    leaf_reps_by_group = _leaf_representatives_by_group(topo, hosts_by_switch)
    role_host = {}
    next_rep_index = {}
    for (g1, g2), sw in sorted(designated_switch.items()):
        hosts = hosts_by_switch.get(sw, [])
        if hosts:
            role_host[(g1, g2)] = hosts[0]
            continue

        if leaf_reps_by_group is not None:
            # sw is a spine (carries no hosts): spread this group's
            # designated roles round-robin across its leaves' representative
            # hosts instead of piling every remote group onto one host.
            reps = leaf_reps_by_group[g1]
            idx = next_rep_index.get(g1, 0)
            role_host[(g1, g2)] = reps[idx % len(reps)]
            next_rep_index[g1] = idx + 1
            continue

        raise ValueError(
            f"g-PAARD needs at least 1 host on switch {sw} (group {g1}'s designated "
            f"link to group {g2}) to act as its global-node role for that pair")
    return role_host


def _leaf_representatives_by_group(topo, hosts_by_switch):
    """group_id -> sorted list of one representative host per leaf switch in
    that group, for Dragonfly+'s per-group round-robin fallback in
    designate_roles(). Returns None for topologies without a spine/leaf split
    (e.g. canonical DragonflyTopo), where this fallback never applies.
    """
    if not hasattr(topo, "spine_map"):
        return None
    switch_group = _switch_group_map(topo)
    reps_by_group = {}
    for sw, hosts in hosts_by_switch.items():
        if hosts:
            reps_by_group.setdefault(switch_group[sw], []).append(hosts[0])
    for reps in reps_by_group.values():
        reps.sort()
    return reps_by_group


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


def _leaves_by_group(topo):
    """group_id -> list of (switch_id, sorted_host_list), one entry per
    leaf/router in that group, sorted by switch_id. Works identically for
    DragonflyTopo (switch_id = router_id) and DragonflyPlusTopo (switch_id
    = leaf_id) -- both store host_map[host] = (group_id, switch_id, host_id)
    with the same shape, so no topology-specific branching is needed.
    """
    by_group_leaf = {}
    for host_name, (group_id, switch_id, _host_id) in topo.host_map.items():
        by_group_leaf.setdefault(group_id, {}).setdefault(switch_id, []).append(host_name)
    return {
        group_id: [(switch_id, sorted(hosts)) for switch_id, hosts in sorted(by_leaf.items())]
        for group_id, by_leaf in by_group_leaf.items()
    }


def _hierarchical_full_exchange(hosts_by_leaf, shard):
    """Replacement for a flat full any-to-all among one group's hosts for
    `shard`: after running combine, THEN exchange, THEN broadcast (in that
    order -- each depends on the previous one's result, they are NOT one
    simultaneous step), every host ends up holding the union of every other
    host's PRE-CALL knowledge for `shard` -- the same end-state guarantee a
    flat O(hosts^2) any-to-all gives, using far fewer of the messages that
    cross a leaf boundary (DF+'s expensive leaf-spine hop; DF has no
    cheap/expensive distinction between routers, but still sends fewer
    total messages, reducing contention).

    hosts_by_leaf: this group's (switch_id, sorted_host_list) list, from
    _leaves_by_group(topo)[group_id].

    Returns (combine_messages, exchange_messages, broadcast_messages).
    Each leaf's first host (deterministic, since hosts_by_leaf's lists are
    pre-sorted) acts as that leaf's representative for the exchange level.
    """
    combine = []
    representatives = []
    for _switch_id, hosts in hosts_by_leaf:
        representatives.append(hosts[0])
        for src in hosts:
            for dst in hosts:
                if src != dst:
                    combine.append((src, dst, shard))

    exchange = []
    for src in representatives:
        for dst in representatives:
            if src != dst:
                exchange.append((src, dst, shard))

    broadcast = []
    for _switch_id, hosts in hosts_by_leaf:
        rep = hosts[0]
        for other in hosts[1:]:
            broadcast.append((rep, other, shard))

    return combine, exchange, broadcast


def build_gpaard_hierarchical_schedule(topo):
    """Optimization idea #2 from DF_vs_DF_Plus_testplan.md: like
    build_gpaard_schedule, but replaces the flat O(hosts^2) intra-group
    any-to-all in steps 3/4 with a 2-level hierarchy (combine within each
    leaf/router, exchange among one representative per leaf, broadcast
    back) via _hierarchical_full_exchange. Steps 1/2/5/6 are UNCHANGED from
    build_gpaard_schedule -- they're already only O(hosts) (fan-in/out to
    one collector), not worth restructuring.

    Returns the same {"reduce_scatter": [...], "all_gather": [...]} shape,
    just with more, individually cheaper, steps inside it: reduce_scatter
    is [step1, step2, combine, exchange, broadcast] (5 steps, was 3);
    all_gather is [combine, exchange, broadcast, step5, step6] (5 steps,
    was 3) -- the all_gather trio is a FRESH call to the same generator,
    not a literal reversal of reduce_scatter's trio (see note below).

    Why calling the generator again, not reversing step3's hierarchy: a
    flat full any-to-all is its own mirror image under src/dst reversal
    (for every (A,B) it already contains (B,A)), which is why the ORIGINAL
    schedule's step4 = reverse(step3) ends up being the exact same
    multiset of messages -- but my 3-level hierarchy is NOT self-mirroring
    (level 3, representative -> leaf-mates, is a fan-out, not a symmetric
    any-to-all), so "reverse the hierarchy" isn't well-defined the same
    way. Calling the generator again instead is correct regardless: after
    reduce_scatter's hierarchy completes, every host in a group already
    holds the full union for its shard (same guarantee as the original
    flat step3), so all_gather's hierarchy -- processed via
    simulate_allreduce's apply_broadcast (overwrite, not union) -- only
    ever overwrites a host's value with an ALREADY-IDENTICAL value from
    another host, a safe no-op. This mirrors the original design's own
    property (its step3/step4 pair is redundant in the same sense) rather
    than introducing a new one.
    """
    num_groups = topo.num_groups
    groups = group_hosts(topo)
    role_host = designate_roles(topo)
    leaves_by_group = _leaves_by_group(topo)

    # Step 1 (unchanged from build_gpaard_schedule).
    step1 = []
    for g in range(num_groups):
        for j in range(num_groups):
            if j == g:
                continue
            collector = role_host[(g, j)]
            for host in groups[g]:
                if host != collector:
                    step1.append((host, collector, j))

    # Step 2 (unchanged from build_gpaard_schedule).
    step2 = []
    seen_pairs = set()
    for g1 in range(num_groups):
        for g2 in range(num_groups):
            if g1 == g2 or frozenset((g1, g2)) in seen_pairs:
                continue
            seen_pairs.add(frozenset((g1, g2)))
            step2.append((role_host[(g1, g2)], role_host[(g2, g1)], g2))
            step2.append((role_host[(g2, g1)], role_host[(g1, g2)], g1))

    def _hierarchical_trio():
        combine_step, exchange_step, broadcast_step = [], [], []
        for g in range(num_groups):
            c, e, b = _hierarchical_full_exchange(leaves_by_group[g], g)
            combine_step.extend(c)
            exchange_step.extend(e)
            broadcast_step.extend(b)
        return [combine_step, exchange_step, broadcast_step]

    reduce_scatter = [step1, step2] + _hierarchical_trio()
    all_gather = _hierarchical_trio() + [
        [(dst, src, shard) for (src, dst, shard) in step2],
        [(dst, src, shard) for (src, dst, shard) in step1],
    ]

    return {"reduce_scatter": reduce_scatter, "all_gather": all_gather}


def simulate_allreduce(topo, schedule_fn=build_gpaard_schedule):
    """Pure-Python correctness check for a g-PAARD schedule. `schedule_fn`
    defaults to the original build_gpaard_schedule; pass
    build_gpaard_hierarchical_schedule to check the optimization idea #2
    variant instead -- same correctness bar, different message schedule.

    Each host's per-shard data is represented as a frozenset of contributor
    host names (provenance tracking) instead of real numbers -- a message
    "reduces" by unioning the sender's set into the receiver's, and
    "broadcasts" by copying it. After running the full schedule, every host
    must hold, for every shard, the set of ALL hosts in the topology --
    otherwise the all-reduce is wrong (data lost, duplicated, or never
    delivered). Raises AssertionError with details if not.

    Returns the final {host: {shard: frozenset(contributors)}} state.
    """
    schedule = schedule_fn(topo)
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
