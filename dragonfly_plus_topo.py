#!/usr/bin/env python3
"""
dragonfly_plus_topo.py

The Dragonfly+ topology for the dragonfly-allreduce-sim project: each group
is a 2-tier leaf-spine (folded-Clos) fabric, instead of canonical Dragonfly's
all-to-all router mesh (DragonflyTopo, in dragonfly_topo.py).

Routing, CLI, and the Mininet network factory are shared, topology-agnostic
infrastructure that lives in dragonfly_topo.py -- it imports DragonflyPlusTopo
from this file to wire it into the same --topology dragonfly+ CLI path.
"""

from mininet.topo import Topo
from mininet.log import info


def _per_group(value, num_groups, name):
    """Normalize a `leaves_per_group`/`spines_per_group`/`hosts_per_leaf`-style
    argument into a per-group list of length `num_groups`. A single int is
    broadcast to every group (the common, symmetric case); a sequence lets
    each group have its own count (asymmetric groups)."""
    if isinstance(value, int):
        return [value] * num_groups
    values = list(value)
    if len(values) != num_groups:
        raise ValueError(
            f"{name} must be an int or a sequence of length num_groups "
            f"({num_groups}), got length {len(values)}")
    return values


class DragonflyPlusTopo(Topo):
    """Dragonfly+ topology.

    Canonical Dragonfly wires every router within a group to every other
    router (all-to-all), so each router's local-link count grows with group
    size — that eats into the fixed port budget a router also needs for
    global links and host ports, capping how large a group can get.

    Dragonfly+ replaces the all-to-all mesh with a 2-tier leaf-spine
    (folded-Clos) fabric per group:
        - `leaves_per_group` leaf routers, each hosting `hosts_per_leaf` end
          hosts, wired to every spine router in the same group.
        - `spines_per_group` spine routers, which carry NO hosts and are the
          only routers that carry inter-group (global) links.

    A leaf's local-link count is now fixed at `spines_per_group`, independent
    of how many other leaves share the group — the tradeoff is one extra
    intra-group hop (leaf -> spine -> leaf) versus canonical Dragonfly's
    single hop between any two routers in a group.
    """

    def __init__(self, num_groups=2, leaves_per_group=2, spines_per_group=2,
                 hosts_per_leaf=1, link_bw=10, link_delay="1ms",
                 global_links_per_pair=1, **kwargs):
        self.num_groups = num_groups
        self.leaves_per_group = _per_group(leaves_per_group, num_groups, "leaves_per_group")
        self.spines_per_group = _per_group(spines_per_group, num_groups, "spines_per_group")
        self.hosts_per_leaf = _per_group(hosts_per_leaf, num_groups, "hosts_per_leaf")
        if any(s < 1 for s in self.spines_per_group):
            raise ValueError("every group needs at least 1 spine router")
        if any(l < 1 for l in self.leaves_per_group):
            raise ValueError("every group needs at least 1 leaf router")
        self.link_bw = link_bw
        self.link_delay = link_delay
        self.global_links_per_pair = global_links_per_pair

        # router_map covers BOTH leaf and spine switches, keyed by
        # (group_id, router_id) with leaves numbered first and spines after
        # (router_id = leaf_id, or leaves_per_group[g] + spine_id) — this
        # keeps it compatible with anything that only needs "which group is
        # this switch in" (e.g. visualize_dragonfly_topo.py's classify_link),
        # without needing to know about the leaf/spine distinction at all.
        self.router_map = {}   # (group_id, router_id) -> switch name (leaf or spine)
        self.leaf_map = {}     # (group_id, leaf_id) -> switch name
        self.spine_map = {}    # (group_id, spine_id) -> switch name
        self.host_map = {}     # host name -> (group_id, leaf_id, host_id)

        super(DragonflyPlusTopo, self).__init__(**kwargs)

    def build(self, *args, **kwargs):
        """Construct the Dragonfly+ graph: leaves+hosts, spines, leaf-spine
        fabric per group, then inter-group links between spines."""
        info("*** Building Dragonfly+ topology "
             f"(groups={self.num_groups}, leaves/group={self.leaves_per_group}, "
             f"spines/group={self.spines_per_group}, hosts/leaf={self.hosts_per_leaf})\n")

        # 1. Create leaf routers, their attached hosts, and spine routers.
        for g in range(self.num_groups):
            for l in range(self.leaves_per_group[g]):
                sw_name = self._add_leaf(g, l)
                for h in range(self.hosts_per_leaf[g]):
                    self._add_host(g, l, h, sw_name)
            for s in range(self.spines_per_group[g]):
                self._add_spine(g, s)

        # 2. Intra-group (local) links: full leaf-spine bipartite fabric.
        self._add_leaf_spine_links()

        # 3. Inter-group (global) links: connect groups' spines together.
        self._add_global_links()

    # --- Construction helpers ------------------------------------------------
    def _add_leaf(self, group_id, leaf_id):
        """Add a single leaf router (switch) and register it."""
        sw_name = f"l{group_id}_{leaf_id}"
        self.addSwitch(sw_name)
        self.leaf_map[(group_id, leaf_id)] = sw_name
        self.router_map[(group_id, leaf_id)] = sw_name
        return sw_name

    def _add_spine(self, group_id, spine_id):
        """Add a single spine router (switch) and register it."""
        sw_name = f"sp{group_id}_{spine_id}"
        self.addSwitch(sw_name)
        router_id = self.leaves_per_group[group_id] + spine_id
        self.spine_map[(group_id, spine_id)] = sw_name
        self.router_map[(group_id, router_id)] = sw_name
        return sw_name

    def _add_host(self, group_id, leaf_id, host_id, sw_name):
        """Add a single host and attach it to its leaf router."""
        host_name = f"h{group_id}_{leaf_id}_{host_id}"
        self.addHost(host_name)
        self.host_map[host_name] = (group_id, leaf_id, host_id)
        self.addLink(host_name, sw_name, bw=self.link_bw, delay=self.link_delay)
        return host_name

    def _add_leaf_spine_links(self):
        """Wire every leaf to every spine within its group (a full bipartite,
        non-blocking Clos fabric — the core Dragonfly+ difference from
        Dragonfly's all-to-all router mesh)."""
        for g in range(self.num_groups):
            for l in range(self.leaves_per_group[g]):
                leaf_name = self.leaf_map[(g, l)]
                for s in range(self.spines_per_group[g]):
                    spine_name = self.spine_map[(g, s)]
                    self.addLink(leaf_name, spine_name,
                                 bw=self.link_bw, delay=self.link_delay)

    def _add_global_links(self):
        """Wire groups together with inter-group (global) links attached only
        to spine routers (leaves never carry global links in Dragonfly+).

        Mirrors DragonflyTopo._add_global_links: endpoints are chosen
        round-robin across each group's spines so global-port usage is spread
        evenly, and `global_links_per_pair` controls how many parallel
        global links connect each group pair.
        """
        next_spine = {g: 0 for g in range(self.num_groups)}

        for g1 in range(self.num_groups):
            for g2 in range(g1 + 1, self.num_groups):
                for _ in range(self.global_links_per_pair):
                    s1 = next_spine[g1] % self.spines_per_group[g1]
                    s2 = next_spine[g2] % self.spines_per_group[g2]
                    next_spine[g1] += 1
                    next_spine[g2] += 1

                    sw1 = self.spine_map[(g1, s1)]
                    sw2 = self.spine_map[(g2, s2)]
                    self.addLink(sw1, sw2,
                                 bw=self.link_bw, delay=self.link_delay)

    def topology_label(self):
        """Short human-readable description of this topology's parameters,
        for use in plot titles / logs."""
        return (f"Dragonfly+ -- groups={self.num_groups}, leaves/group={self.leaves_per_group}, "
                f"spines/group={self.spines_per_group}, hosts/leaf={self.hosts_per_leaf}, "
                f"global={self.global_links_per_pair}")
