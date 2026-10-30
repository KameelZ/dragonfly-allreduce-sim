#!/usr/bin/env python3
"""
link_utilization.py

DF_vs_DF_Plus_testplan.md instrumentation item #3: per-link bandwidth
utilization. No earlier script in this project reads link-level byte
counters at all -- every sweep only ever timed the application-level
all-reduce. This gives test_3_mechanism_isolating.py a way to answer a
different question than "who finished first": does DF+'s extra leaf-spine
fabric actually spread load across more links, even on runs where total
completion time doesn't move?

Approach: find the real veth interface pair for a named link (by the two
switch names on either end, via Mininet's own net.linksBetween), snapshot
/sys/class/net/<iface>/statistics/{rx,tx}_bytes before and after a timed
run, and turn the delta into an achieved Mbit/s and a utilization fraction
against the link's nominal bandwidth.

NOT exercised against a live Mininet network in this sandbox (no root
available here) -- the Mininet API calls this relies on (net.linksBetween,
Link.intf1/intf2, Intf.name/.node) were confirmed by reading the installed
mininet package's source directly, but this file's actual byte-counter
behavior should be smoke-tested against a real run before being trusted.
"""


def find_global_link_switches(view, g1, g2):
    """Find the two switch names of a direct global link between groups g1
    and g2, using the already-built topology `view` (see
    visualize_dragonfly_topo.build_topology_view) rather than re-deriving
    the round-robin global-link assignment. None if not found (shouldn't
    happen for any group pair in a connected Dragonfly/Dragonfly+)."""
    from visualize_dragonfly_topo import classify_link
    for n1, n2 in view["links"]:
        if classify_link(n1, n2, view["host_set"], view["switch_group"]) != "global":
            continue
        if {view["switch_group"][n1], view["switch_group"][n2]} == {g1, g2}:
            return n1, n2
    return None


def find_local_link_switches(view, group_id):
    """Find the two switch names of a representative intra-group ("local")
    link within `group_id`. For DragonflyTopo this is a flat router-mesh
    link; for DragonflyPlusTopo every local link is leaf-spine (leaves
    never connect directly to each other), so this always returns a
    leaf-spine link there -- exactly the comparison point test_3 wants."""
    from visualize_dragonfly_topo import classify_link
    for n1, n2 in view["links"]:
        if classify_link(n1, n2, view["host_set"], view["switch_group"]) != "local":
            continue
        if view["switch_group"][n1] == group_id and view["switch_group"][n2] == group_id:
            return n1, n2
    return None


def find_link_interfaces(net, sw1_name, sw2_name):
    """Return (if_on_sw1, if_on_sw2) interface names for the direct link
    between the two named switches, or None if they aren't directly linked
    (or more than one parallel link exists -- global_links_per_pair>1 means
    multiple links per group pair; this returns only the first one found,
    which is fine for a representative utilization sample)."""
    n1 = net.get(sw1_name)
    n2 = net.get(sw2_name)
    links = net.linksBetween(n1, n2)
    if not links:
        return None
    link = links[0]
    if link.intf1.node is n1:
        return link.intf1.name, link.intf2.name
    return link.intf2.name, link.intf1.name


def read_iface_bytes(ifname):
    """(rx_bytes, tx_bytes) for a network interface, read from sysfs."""
    base = f"/sys/class/net/{ifname}/statistics"
    with open(f"{base}/rx_bytes") as f:
        rx = int(f.read().strip())
    with open(f"{base}/tx_bytes") as f:
        tx = int(f.read().strip())
    return rx, tx


class LinkUtilizationSampler:
    """Tracks total bytes (both directions, both ends) seen on a named set
    of links, so the caller can diff two snapshots across a timed run."""

    def __init__(self, net, link_specs):
        """link_specs: {label: (sw1_name, sw2_name)}"""
        self.interfaces = {
            label: find_link_interfaces(net, sw1, sw2)
            for label, (sw1, sw2) in link_specs.items()
        }

    def snapshot(self):
        """label -> total bytes seen on both ends of that link right now
        (None if the link's interfaces couldn't be resolved)."""
        snap = {}
        for label, ifaces in self.interfaces.items():
            if ifaces is None:
                snap[label] = None
                continue
            if1, if2 = ifaces
            rx1, tx1 = read_iface_bytes(if1)
            rx2, tx2 = read_iface_bytes(if2)
            snap[label] = rx1 + tx1 + rx2 + tx2
        return snap

    def utilization_since(self, prev_snapshot, duration_s, link_bw_mbit):
        """label -> {"achieved_mbit": ..., "utilization": ...} since
        prev_snapshot (a dict previously returned by .snapshot()).
        utilization = achieved_mbit / link_bw_mbit; can read >1 since byte
        counts are summed over both directions of a full-duplex veth pair."""
        now = self.snapshot()
        result = {}
        for label, prev_bytes in prev_snapshot.items():
            now_bytes = now.get(label)
            if prev_bytes is None or now_bytes is None or duration_s <= 0:
                result[label] = None
                continue
            delta_bytes = now_bytes - prev_bytes
            achieved_mbit = (delta_bytes * 8) / duration_s / 1e6
            result[label] = {
                "achieved_mbit": achieved_mbit,
                "utilization": achieved_mbit / link_bw_mbit if link_bw_mbit > 0 else float("nan"),
            }
        return result
