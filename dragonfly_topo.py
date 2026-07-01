#!/usr/bin/env python3
"""
dragonfly_topo.py

Foundational boilerplate for simulating and evaluating Dragonfly / Dragonfly+
network topologies under dynamic traffic loads in Mininet.

Project: dragonfly-allreduce-sim (Technion course 236340)
    Performance analysis and optimization of Dragonfly network topologies and
    routing algorithms under dynamic traffic loads using Mininet.

Project goal (CDR scope):
    - Build a custom Dragonfly topology in Mininet.
    - Implement and analyze the g-PAARD routing algorithm.
    - Drive the network with dynamic/synthetic traffic loads and collect metrics.

This file holds the canonical DragonflyTopo plus all shared, topology-agnostic
infrastructure: g-PAARD routing, the traffic-load stub, the Mininet network
factory, and the CLI. The Dragonfly+ topology (DragonflyPlusTopo) lives in
dragonfly_plus_topo.py and is imported here so build_network() can select
between the two via --topology.

The g-PAARD routing logic and traffic generators are stubbed out and marked
with explicit injection points so they can be implemented incrementally.
"""

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.clean import cleanup

from collections import deque
from functools import partial
import argparse
import sys
import time

from dragonfly_plus_topo import DragonflyPlusTopo


# ---------------------------------------------------------------------------
# Topology definition
# ---------------------------------------------------------------------------
def _per_group(value, num_groups, name):
    """Normalize a `routers_per_group`/`hosts_per_router`-style argument into
    a per-group list of length `num_groups`. A single int is broadcast to
    every group (the common, symmetric case); a sequence lets each group
    have its own count (asymmetric groups)."""
    if isinstance(value, int):
        return [value] * num_groups
    values = list(value)
    if len(values) != num_groups:
        raise ValueError(
            f"{name} must be an int or a sequence of length num_groups "
            f"({num_groups}), got length {len(values)}")
    return values


class DragonflyTopo(Topo):
    """Custom Dragonfly topology.

    A Dragonfly network is organized into `num_groups` groups. Each group
    contains `routers_per_group` routers connected by intra-group (local) links.
    Groups are connected to one another by inter-group (global) links. Each
    router hosts `hosts_per_router` end hosts.

    `routers_per_group` and `hosts_per_router` each accept either a single int
    (broadcast to every group, the canonical symmetric Dragonfly) or a
    sequence of length `num_groups` giving each group its own router/host
    count (asymmetric groups).

    The current implementation wires up a minimal, fully-modular skeleton so the
    network boots cleanly. Exact Dragonfly wiring (all-to-all intra-group and the
    global link assignment) is built in the helper methods below and can be
    refined as the design matures.
    """

    def __init__(self, num_groups=2, routers_per_group=2, hosts_per_router=1,
                 link_bw=10, link_delay="1ms", global_links_per_pair=1, **kwargs):
        # Store design parameters before Topo.__init__ triggers build().
        self.num_groups = num_groups
        self.routers_per_group = _per_group(routers_per_group, num_groups, "routers_per_group")
        self.hosts_per_router = _per_group(hosts_per_router, num_groups, "hosts_per_router")
        if any(r < 1 for r in self.routers_per_group):
            raise ValueError("every group needs at least 1 router")
        self.link_bw = link_bw
        self.link_delay = link_delay
        self.global_links_per_pair = global_links_per_pair

        # Bookkeeping for switches/hosts so routing logic can reason about them.
        # NOTE: these are deliberately NOT named `hosts`/`switches`, which would
        # shadow Topo.hosts()/Topo.switches() and break Mininet's build step.
        self.router_map = {}   # (group_id, router_id) -> switch name
        self.host_map = {}     # host name -> (group_id, router_id, host_id)

        super(DragonflyTopo, self).__init__(**kwargs)

    def build(self, *args, **kwargs):
        """Construct the Dragonfly graph: routers, hosts, local + global links."""
        info("*** Building Dragonfly topology "
             f"(groups={self.num_groups}, routers/group={self.routers_per_group}, "
             f"hosts/router={self.hosts_per_router})\n")

        # 1. Create routers (switches) and their attached hosts.
        for g in range(self.num_groups):
            for r in range(self.routers_per_group[g]):
                sw_name = self._add_router(g, r)
                for h in range(self.hosts_per_router[g]):
                    self._add_host(g, r, h, sw_name)

        # 2. Intra-group (local) links: all-to-all within each group.
        self._add_local_links()

        # 3. Inter-group (global) links: connect groups together.
        self._add_global_links()

    # --- Construction helpers ------------------------------------------------
    def _add_router(self, group_id, router_id):
        """Add a single router (switch) and register it."""
        sw_name = f"s{group_id}_{router_id}"
        self.addSwitch(sw_name)
        self.router_map[(group_id, router_id)] = sw_name
        return sw_name

    def _add_host(self, group_id, router_id, host_id, sw_name):
        """Add a single host and attach it to its router."""
        host_name = f"h{group_id}_{router_id}_{host_id}"
        self.addHost(host_name)
        self.host_map[host_name] = (group_id, router_id, host_id)
        self.addLink(host_name, sw_name, bw=self.link_bw, delay=self.link_delay)
        return host_name

    def _add_local_links(self):
        """Wire all routers within a group to each other (all-to-all)."""
        for g in range(self.num_groups):
            routers = [self.router_map[(g, r)] for r in range(self.routers_per_group[g])]
            for i in range(len(routers)):
                for j in range(i + 1, len(routers)):
                    self.addLink(routers[i], routers[j],
                                 bw=self.link_bw, delay=self.link_delay)

    def _add_global_links(self):
        """Wire groups together with inter-group (global) links.

        Canonical Dragonfly connects every pair of groups with (at least) one
        global link. Rather than hanging all global links off router 0, the
        endpoint routers are chosen round-robin within each group so the global
        links are spread evenly across the routers of a group. This balances
        global-port usage and gives the routing algorithm genuine path
        diversity to exploit.

        `global_links_per_pair` controls how many parallel global links connect
        each group pair (1 = sparse/canonical, higher = richer global bandwidth).
        """
        # Per-group cursor for round-robin router selection.
        next_router = {g: 0 for g in range(self.num_groups)}

        for g1 in range(self.num_groups):
            for g2 in range(g1 + 1, self.num_groups):
                for _ in range(self.global_links_per_pair):
                    r1 = next_router[g1] % self.routers_per_group[g1]
                    r2 = next_router[g2] % self.routers_per_group[g2]
                    next_router[g1] += 1
                    next_router[g2] += 1

                    sw1 = self.router_map[(g1, r1)]
                    sw2 = self.router_map[(g2, r2)]
                    self.addLink(sw1, sw2,
                                 bw=self.link_bw, delay=self.link_delay)

    def topology_label(self):
        """Short human-readable description of this topology's parameters,
        for use in plot titles / logs."""
        return (f"Dragonfly -- groups={self.num_groups}, routers/group={self.routers_per_group}, "
                f"hosts/router={self.hosts_per_router}, global={self.global_links_per_pair}")


# ---------------------------------------------------------------------------
# g-PAARD routing — INJECTION POINT
# ---------------------------------------------------------------------------
def _build_switch_graph(net):
    """Derive the router graph and host attachments from the running network.

    Returns:
        adj:          {switch_name: {neighbor_switch_name: local_Intf}}
        host_attach:  {host_name: (switch_name, switch_side_Intf)}

    The Intf objects are read straight from Mininet's live link list, so the
    OpenFlow port numbers derived from them (via ``switch.ports[intf]``) match
    the actual datapath — no guessing about link-creation order.
    """
    switch_names = {s.name for s in net.switches}
    adj = {s.name: {} for s in net.switches}
    host_attach = {}

    for link in net.links:
        intf_a, intf_b = link.intf1, link.intf2
        node_a, node_b = intf_a.node, intf_b.node
        a_is_sw = node_a.name in switch_names
        b_is_sw = node_b.name in switch_names

        if a_is_sw and b_is_sw:
            adj[node_a.name][node_b.name] = intf_a
            adj[node_b.name][node_a.name] = intf_b
        elif a_is_sw and not b_is_sw:
            host_attach[node_b.name] = (node_a.name, intf_a)
        elif b_is_sw and not a_is_sw:
            host_attach[node_a.name] = (node_b.name, intf_b)

    return adj, host_attach


def _shortest_path_next_hops(adj, dst_switch):
    """BFS from ``dst_switch`` over the router graph.

    Returns ``next_hop[switch] = neighbor`` giving, for every switch, the
    neighbour to forward to in order to make progress toward ``dst_switch``
    along a shortest path. Neighbours are visited in sorted order so the result
    is deterministic (a fixed shortest-path tree per destination).

    Per-destination shortest-path forwarding is inherently loop-free, which is
    why this replaces STP entirely: there is no spanning tree and no flooding,
    so the network uses the real Dragonfly link diversity instead of collapsing
    onto one tree.
    """
    next_hop = {}
    visited = {dst_switch}
    queue = deque([dst_switch])
    while queue:
        current = queue.popleft()
        for neighbor in sorted(adj[current]):
            if neighbor not in visited:
                visited.add(neighbor)
                next_hop[neighbor] = current
                queue.append(neighbor)
    return next_hop


def select_path_next_hop(switch_name, dst_switch, adj, shortest_tree):
    """Choose the next-hop neighbour for traffic at ``switch_name``.

    >>> ADAPTIVE ROUTING INJECTION POINT <<<
    This is the single decision function the routing layer consults for every
    (current switch, destination) pair. Right now it returns the deterministic
    *minimal* (shortest-path) next hop -- the "MIN routing" baseline the
    g-PAARD paper evaluates against (see gpaard.py).

    NOTE: g-PAARD itself is NOT implemented here. g-PAARD (gpaard.py) is an
    all-reduce communication *pattern* -- it decides which hosts exchange
    data and in what order -- and runs unchanged on top of whatever unicast
    routing this function produces. This injection point is for swapping in
    an *adaptive/load-aware routing* policy instead (e.g. UGAL: weigh the
    minimal next hop against non-minimal, via-an-intermediate-group hops
    using live link-load). The surrounding install_gpaard_routing() machinery
    (flow installation, port lookup) does not need to change -- only this
    policy does.
    """
    return shortest_tree.get(switch_name)


def install_gpaard_routing(net):
    """Install destination-based forwarding flows on every switch.

    Despite the name (kept for continuity with earlier milestones), this
    installs plain unicast routing -- it's the underlying transport g-PAARD's
    all-reduce message schedule (gpaard.py) runs on top of, not g-PAARD
    itself.

    Strategy (controller-less, deterministic):
        - Hosts use static ARP (set in main), so no broadcast/flooding occurs.
        - For each destination host we compute a shortest-path tree over the
          router graph and, at every switch, install one flow matching the
          host's destination MAC and outputting toward the chosen next hop.
        - The next hop is chosen by select_path_next_hop(), the adaptive-
          routing policy hook, so swapping in a load-aware algorithm later
          touches one place.

    Switches run in OVS 'secure' fail-mode (default for OVSSwitch) with no
    controller, so only these explicitly installed flows forward traffic.
    """
    info("*** [routing] computing and installing forwarding flows\n")
    adj, host_attach = _build_switch_graph(net)

    # Start from a clean slate so re-installs are idempotent.
    for switch in net.switches:
        switch.cmd("ovs-vsctl set-fail-mode", switch.name, "secure")
        switch.dpctl("del-flows")

    flow_count = 0
    for host in net.hosts:
        dst_switch_name, dst_side_intf = host_attach[host.name]
        dst_mac = host.MAC()
        shortest_tree = _shortest_path_next_hops(adj, dst_switch_name)

        for switch in net.switches:
            if switch.name == dst_switch_name:
                # Destination is local: hand the frame straight to the host.
                out_port = switch.ports[dst_side_intf]
            else:
                next_hop = select_path_next_hop(
                    switch.name, dst_switch_name, adj, shortest_tree)
                if next_hop is None:
                    continue  # no path (should not happen in a connected DF)
                out_port = switch.ports[adj[switch.name][next_hop]]

            switch.dpctl("add-flow", f"dl_dst={dst_mac},actions=output:{out_port}")
            flow_count += 1

    info(f"*** [routing] installed {flow_count} flows across "
         f"{len(net.switches)} switches\n")
    return


# ---------------------------------------------------------------------------
# Dynamic traffic load generation — INJECTION POINT
# ---------------------------------------------------------------------------
def start_traffic_load(net):
    """Start dynamic/synthetic traffic loads across the network.

    TODO (CDR deliverable): Implement traffic generators here.
        - Spin up iperf/iperf3 (or custom) flows between host pairs.
        - Model dynamic load patterns (uniform, adversarial, hotspot, etc.).
        - Collect throughput/latency metrics for analysis.

    For now this is a no-op placeholder.
    """
    info("*** [traffic] load generators not yet implemented (placeholder)\n")
    # >>> traffic load generators will be injected here <<<
    return


# ---------------------------------------------------------------------------
# Network factory
# ---------------------------------------------------------------------------
def build_network(args):
    """Instantiate the Mininet network with the chosen Dragonfly topology."""
    if args.topology == "dragonfly+":
        topo = DragonflyPlusTopo(
            num_groups=args.groups,
            leaves_per_group=args.leaves,
            spines_per_group=args.spines,
            hosts_per_leaf=args.hosts,
            link_bw=args.bw,
            link_delay=args.delay,
            global_links_per_pair=args.global_links,
        )
    else:
        topo = DragonflyTopo(
            num_groups=args.groups,
            routers_per_group=args.routers,
            hosts_per_router=args.hosts,
            link_bw=args.bw,
            link_delay=args.delay,
            global_links_per_pair=args.global_links,
        )

    # OVSSwitch defaults to OVS 'secure' fail-mode, so with no controller each
    # switch drops all traffic until we install explicit flows in
    # install_gpaard_routing(). This gives us full, deterministic control of
    # forwarding (no STP, no flooding) while still requiring no controller
    # binary. Swap in a RemoteController here if/when an SDN controller is used.
    net = Mininet(
        topo=topo,
        switch=OVSSwitch,
        link=TCLink,
        controller=None,  # controller-less: forwarding driven by installed flows
        autoSetMacs=True,
    )
    return net


def parse_args(argv=None):
    """Parse command-line configuration for the Dragonfly simulation."""
    parser = argparse.ArgumentParser(
        description="Dragonfly / Dragonfly+ topology simulation in Mininet.")
    parser.add_argument("-t", "--topology", choices=["dragonfly", "dragonfly+"], default="dragonfly",
                        help="topology variant: canonical Dragonfly (all-to-all "
                             "routers per group) or Dragonfly+ (leaf-spine fabric "
                             "per group) (default: dragonfly)")
    parser.add_argument("-g", "--groups", type=int, default=2,
                        help="number of Dragonfly groups (default: 2)")
    parser.add_argument("-r", "--routers", type=int, default=2,
                        help="routers per group, dragonfly only (default: 2)")
    parser.add_argument("--leaves", type=int, default=2,
                        help="leaf routers per group, dragonfly+ only (default: 2)")
    parser.add_argument("--spines", type=int, default=2,
                        help="spine routers per group, dragonfly+ only (default: 2)")
    parser.add_argument("-H", "--hosts", type=int, default=1,
                        help="hosts per router (dragonfly) / per leaf (dragonfly+) (default: 1)")
    parser.add_argument("--bw", type=float, default=10,
                        help="link bandwidth in Mbit/s (default: 10)")
    parser.add_argument("--delay", type=str, default="1ms",
                        help="per-link delay, e.g. '1ms' (default: 1ms)")
    parser.add_argument("--global-links", type=int, default=1,
                        help="global links per group pair (default: 1)")
    parser.add_argument("--test", action="store_true",
                        help="run a non-interactive pingall and exit with a "
                             "pass/fail status instead of opening the CLI")
    parser.add_argument("--settle", type=int, default=1,
                        help="seconds to wait after installing flows before "
                             "the --test pingall (default: 1)")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    setLogLevel("info")
    args = parse_args()

    # Clear any leftover Mininet state (dangling interfaces, stale OVS bridges,
    # orphaned controllers) from a previous run that may not have shut down
    # cleanly. This prevents "RTNETLINK answers: File exists" errors on startup.
    info("*** Cleaning up any stale Mininet state\n")
    cleanup()

    net = build_network(args)
    exit_code = 0

    try:
        net.start()

        # Populate static ARP entries on every host so no host ever needs to
        # broadcast an ARP request. Combined with secure-mode switches, this
        # means the ONLY traffic in the network is unicast we explicitly route
        # — no flooding, so the Dragonfly loops are harmless without STP.
        info("*** Installing static ARP entries\n")
        net.staticArp()

        # Install the (currently shortest-path) forwarding flows. This is the
        # g-PAARD injection point.
        install_gpaard_routing(net)

        # Traffic generators (currently a no-op placeholder).
        start_traffic_load(net)

        if args.test:
            # Non-interactive verification: let flows settle, then run an
            # all-pairs ping. Exit non-zero on any packet loss so this can be
            # used as a smoke test in scripts / CI.
            if args.settle:
                info(f"*** Test mode: letting flows settle for {args.settle}s\n")
                time.sleep(args.settle)
            info("*** Running pingall\n")
            loss = net.pingAll()
            if loss == 0.0:
                info("*** TEST PASSED: 0% packet loss\n")
            else:
                info(f"*** TEST FAILED: {loss:.1f}% packet loss\n")
                exit_code = 1
        else:
            info("*** Network is up. Dropping to Mininet CLI.\n")
            info("*** Forwarding flows are installed; 'pingall' should pass "
                 "immediately.\n")
            CLI(net)
    finally:
        # Always tear down, even if startup or the CLI raises, so we never
        # leave dangling interfaces behind.
        net.stop()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
