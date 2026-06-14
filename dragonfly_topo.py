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

This file intentionally provides ONLY a clean, modular skeleton that boots in
Mininet. The g-PAARD routing logic and traffic generators are stubbed out and
marked with explicit injection points so they can be implemented incrementally.
"""

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSBridge, RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.clean import cleanup


# ---------------------------------------------------------------------------
# Topology definition
# ---------------------------------------------------------------------------
class DragonflyTopo(Topo):
    """Custom Dragonfly topology.

    A Dragonfly network is organized into `num_groups` groups. Each group
    contains `routers_per_group` routers connected by intra-group (local) links.
    Groups are connected to one another by inter-group (global) links. Each
    router hosts `hosts_per_router` end hosts.

    The current implementation wires up a minimal, fully-modular skeleton so the
    network boots cleanly. Exact Dragonfly wiring (all-to-all intra-group and the
    global link assignment) is built in the helper methods below and can be
    refined as the design matures.
    """

    def __init__(self, num_groups=2, routers_per_group=2, hosts_per_router=1,
                 link_bw=10, link_delay="1ms", **kwargs):
        # Store design parameters before Topo.__init__ triggers build().
        self.num_groups = num_groups
        self.routers_per_group = routers_per_group
        self.hosts_per_router = hosts_per_router
        self.link_bw = link_bw
        self.link_delay = link_delay

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
            for r in range(self.routers_per_group):
                sw_name = self._add_router(g, r)
                for h in range(self.hosts_per_router):
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
            routers = [self.router_map[(g, r)] for r in range(self.routers_per_group)]
            for i in range(len(routers)):
                for j in range(i + 1, len(routers)):
                    self.addLink(routers[i], routers[j],
                                 bw=self.link_bw, delay=self.link_delay)

    def _add_global_links(self):
        """Wire groups together with inter-group (global) links.

        Minimal scheme: connect router 0 of each group to router 0 of every
        other group. Replace with the full Dragonfly global-link assignment
        (and Dragonfly+ variant) as the design is finalized.
        """
        for g1 in range(self.num_groups):
            for g2 in range(g1 + 1, self.num_groups):
                r1 = self.router_map[(g1, 0)]
                r2 = self.router_map[(g2, 0)]
                self.addLink(r1, r2, bw=self.link_bw, delay=self.link_delay)


# ---------------------------------------------------------------------------
# g-PAARD routing — INJECTION POINT
# ---------------------------------------------------------------------------
def install_gpaard_routing(net):
    """Install the g-PAARD routing algorithm onto the running network.

    TODO (CDR deliverable): Implement g-PAARD here.
        - Compute / install forwarding rules (flows) per router.
        - Apply the adaptive, load-aware path selection that g-PAARD specifies.
        - Hook into the controller if using a RemoteController/SDN approach.

    For now this is a no-op placeholder so the network boots cleanly.
    """
    info("*** [g-PAARD] routing not yet implemented (placeholder)\n")
    # >>> g-PAARD routing logic will be injected here <<<
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
def build_network():
    """Instantiate the Mininet network with the Dragonfly topology."""
    topo = DragonflyTopo(
        num_groups=2,
        routers_per_group=2,
        hosts_per_router=1,
    )

    # OVSBridge runs each switch as a standalone OVS learning bridge with NO
    # external controller, so the network boots without any controller binary
    # installed. STP is enabled (below) so the loops in the Dragonfly mesh do
    # not create broadcast storms.
    #
    # When the g-PAARD/SDN controller is ready, swap OVSBridge for OVSSwitch and
    # add a RemoteController so g-PAARD can install flows programmatically.
    net = Mininet(
        topo=topo,
        switch=OVSBridge,
        link=TCLink,
        controller=None,  # standalone bridges need no controller
        autoSetMacs=True,
    )
    return net


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    setLogLevel("info")

    # Clear any leftover Mininet state (dangling interfaces, stale OVS bridges,
    # orphaned controllers) from a previous run that may not have shut down
    # cleanly. This prevents "RTNETLINK answers: File exists" errors on startup.
    info("*** Cleaning up any stale Mininet state\n")
    cleanup()

    net = build_network()

    try:
        net.start()

        # Enable STP on every bridge so the redundant local/global links in the
        # Dragonfly mesh do not cause L2 broadcast storms while running in
        # standalone (controller-less) mode.
        info("*** Enabling STP on switches\n")
        for switch in net.switches:
            switch.cmd(f"ovs-vsctl set bridge {switch.name} stp_enable=true")

        # Injection points (currently no-ops):
        install_gpaard_routing(net)
        start_traffic_load(net)

        info("*** Network is up. Dropping to Mininet CLI.\n")
        info("*** Note: with STP enabled, allow a few seconds for the spanning\n")
        info("***       tree to converge before the first 'pingall'.\n")
        CLI(net)
    finally:
        # Always tear down, even if startup or the CLI raises, so we never
        # leave dangling interfaces behind.
        net.stop()


if __name__ == "__main__":
    main()
