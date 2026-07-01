#!/usr/bin/env python3
"""
visualize_dragonfly_topo.py

A manual inspection script that instantiates DragonflyTopo (dragonfly_topo.py)
and DragonflyPlusTopo (dragonfly_plus_topo.py), prints their structure for
visual verification, and generates plots using matplotlib and networkx.
Dragonfly images are saved under DRAGONFLY_PLOTS_DIR ("dragonfly_plots/");
Dragonfly+ images are saved under DRAGONFLY_PLUS_PLOTS_DIR
("dragonfly_plus_plots/") -- kept separate since the two topologies aren't
directly comparable image-for-image (different node roles, layouts).

It also contains pytest-discoverable `test_*` functions that check topology
construction (link/node counts, classification) across a range of group,
router, and host configurations for both topologies:

    pytest visualize_dragonfly_topo.py

Both mininet (imported transitively via dragonfly_topo.py) and
matplotlib/networkx are required to run this script -- there is no
lighter-weight path that skips either:

    pip install matplotlib networkx pytest
"""

import math
import os
import re

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches  # For legend
import matplotlib.lines as mlines      # For legend
import networkx as nx

from dragonfly_topo import DragonflyTopo
from dragonfly_plus_topo import DragonflyPlusTopo


def _natural_sort_key(name):
    """Sort key that treats digit runs as numbers, so 's0_10' sorts after
    's0_9' instead of lexicographically between 's0_1' and 's0_2'."""
    return [int(chunk) if chunk.isdigit() else chunk for chunk in re.split(r"(\d+)", name)]


def _link_sort_key(link):
    n1, n2 = link
    return (_natural_sort_key(n1), _natural_sort_key(n2))


def classify_link(n1, n2, host_set, switch_group):
    """Classify a link as 'host', 'local', or 'global' using the topology's
    own structure (host_set, switch_group) rather than parsing node-name
    strings, so it stays correct even if the naming convention changes."""
    if n1 in host_set or n2 in host_set:
        return "host"
    if switch_group[n1] == switch_group[n2]:
        return "local"
    return "global"


def build_topology_view(topo):
    """Gather sorted node/link data and classification maps once, so
    print_topology_details() and plot_topology() can share it instead of
    each re-deriving it from scratch."""
    host_set = set(topo.hosts())
    switch_group = {sw_name: group_id for (group_id, _router_id), sw_name in topo.router_map.items()}

    return {
        "nodes": sorted(topo.nodes(), key=_natural_sort_key),
        "links": sorted(topo.links(), key=_link_sort_key),
        "switches": sorted(topo.switches(), key=_natural_sort_key),
        "hosts": sorted(topo.hosts(), key=_natural_sort_key),
        "host_set": host_set,
        "switch_group": switch_group,
    }


def print_topology_details(topo, view):
    """
    Prints a detailed summary of the generated topology object.
    """
    print(f"--- Topology Summary ---")
    print(f"Parameters: {topo.topology_label()}")
    print("-" * 24)

    print(f"\nTotal Routers (Switches): {len(view['switches'])}")
    print(f"Total Hosts: {len(view['hosts'])}")
    print(f"Total Nodes: {len(view['nodes'])}")
    print(f"Total Links: {len(view['links'])}")

    print("\nRouters (Switches):")
    for sw in view["switches"]:
        print(f"  - {sw}")

    print("\nHosts:")
    for h in view["hosts"]:
        print(f"  - {h}")

    # Classify links for better readability
    host_links = []
    local_links = []
    global_links = []
    by_kind = {"host": host_links, "local": local_links, "global": global_links}

    for n1, n2 in view["links"]:
        kind = classify_link(n1, n2, view["host_set"], view["switch_group"])
        by_kind[kind].append(f"{n1} <--> {n2}")

    print("\nHost-to-Router Links:")
    for link in host_links:
        print(f"  {link}")

    print("\nlocal-Group (Local) Links:")
    for link in local_links:
        print(f"  {link}")

    print("\nglobal-Group (Global) Links:")
    for link in global_links:
        print(f"  {link}")

    print("\n--- End of Summary ---")


def plot_topology(topo, view, filename="topology.png"):
    """
    Generates and saves a visual representation of the topology graph using
    matplotlib and networkx.

    Uses a MultiGraph (not Graph) so that parallel links between the same
    switch pair -- which happen when global_links_per_pair's round-robin
    wraps back onto the same routers -- are preserved instead of silently
    collapsed into a single edge.
    """
    G = nx.MultiGraph()
    G.add_nodes_from(view["nodes"])
    G.add_edges_from(view["links"])

    # --- Node coloring: hosts vs routers ---
    node_colors = ["lightgreen" if node in view["host_set"] else "skyblue" for node in G.nodes()]

    # --- Edge coloring: host, local, vs global links ---
    kind_color = {"host": "gray", "local": "blue", "global": "red"}
    edge_colors = [
        kind_color[classify_link(u, v, view["host_set"], view["switch_group"])]
        for u, v, _key in G.edges(keys=True)
    ]

    # --- Plotting ---
    print("*** Generating topology plot... ***")
    plt.figure(figsize=(20, 16))  # Slightly larger figure to accommodate legend
    # Use a spring layout for a force-directed graph visualization
    pos = nx.spring_layout(G, seed=42, iterations=100)

    nx.draw(G, pos,
            with_labels=True,
            node_color=node_colors,
            edge_color=edge_colors,
            node_size=1200,  # Increased node size
            font_size=12,    # Increased font size for labels
            font_weight="bold",
            width=2.0,       # Increased edge width
            connectionstyle="arc3,rad=0.12")  # curve parallel edges apart so they stay visible

    # --- Add Legend ---
    host_patch = mpatches.Patch(color="lightgreen", label="Host")
    router_patch = mpatches.Patch(color="skyblue", label="Router")

    host_link_line = mlines.Line2D([], [], color="gray", linewidth=2.0, label="Host Link")
    local_link_line = mlines.Line2D([], [], color="blue", linewidth=2.0, label="Local-Group Link")
    global_link_line = mlines.Line2D([], [], color="red", linewidth=2.0, label="Global-Group Link")

    plt.legend(handles=[host_patch, router_patch, host_link_line, local_link_line, global_link_line],
               loc="upper left", bbox_to_anchor=(1, 1), title="Legend",
               fontsize=10, title_fontsize=12)

    plt.title(topo.topology_label(), fontsize=16)
    plt.savefig(filename, bbox_inches="tight")  # Ensure legend is not cut off
    plt.close()
    print(f"*** Topology plot saved to {filename} ***")


def _grouped_layout(topo, group_radius=12.0, router_radius=2.6, host_offset=1.1):
    """Position nodes so each Dragonfly group forms its own visually distinct
    cluster arranged around a big circle, with hosts orbiting their router.

    This reads directly from topo.router_map/host_map (not node-name
    strings), so it naturally handles asymmetric groups -- each group's
    cluster is only as big as that group's own router/host count.
    """
    pos = {}
    group_center = {}
    for g in range(topo.num_groups):
        angle = 2 * math.pi * g / topo.num_groups
        group_center[g] = (group_radius * math.cos(angle), group_radius * math.sin(angle))

    routers_by_group = {g: [] for g in range(topo.num_groups)}
    for (g, r), name in topo.router_map.items():
        routers_by_group[g].append((r, name))

    for g, routers in routers_by_group.items():
        routers.sort()
        cx, cy = group_center[g]
        n = len(routers)
        for i, (_r, name) in enumerate(routers):
            angle = 2 * math.pi * i / n
            pos[name] = (cx + router_radius * math.cos(angle),
                         cy + router_radius * math.sin(angle))

    hosts_by_router = {}
    for host_name, (g, r, h) in topo.host_map.items():
        hosts_by_router.setdefault((g, r), []).append((h, host_name))

    for (g, r), hosts in hosts_by_router.items():
        hosts.sort()
        sx, sy = pos[topo.router_map[(g, r)]]
        cx, cy = group_center[g]
        dx, dy = sx - cx, sy - cy
        norm = math.hypot(dx, dy) or 1.0
        dx, dy = dx / norm, dy / norm       # unit vector: group center -> router
        px, py = -dy, dx                    # perpendicular, for spreading hosts sideways
        n = len(hosts)
        for i, (_h, host_name) in enumerate(hosts):
            spread = (i - (n - 1) / 2) * 0.55
            pos[host_name] = (sx + dx * host_offset + px * spread,
                              sy + dy * host_offset + py * spread)

    return pos


def plot_topology_grouped(topo, view, filename="topology_grouped.png"):
    """A Dragonfly-styled visualizer: groups are arranged around a ring, each
    group's routers cluster together with their hosts orbiting outward, and
    every group gets its own color -- so cluster structure and inter-group
    connectivity both read clearly at a glance, even with many/asymmetric
    groups. Unlike plot_topology(), this is not the general-purpose renderer;
    it's a purpose-built, "nicer" view for showcasing bigger topologies.
    """
    G = nx.MultiGraph()
    G.add_nodes_from(view["nodes"])
    G.add_edges_from(view["links"])

    pos = _grouped_layout(topo)

    cmap = plt.get_cmap("tab20")
    group_color = {g: cmap(g % 20) for g in range(topo.num_groups)}

    # Dragonfly+ topologies expose spine_map; canonical Dragonfly doesn't, so
    # spine_names is empty and every switch renders as a plain "router" node
    # below -- this function works unchanged for either topology.
    spine_names = set(getattr(topo, "spine_map", {}).values())
    host_nodes = [n for n in G.nodes() if n in view["host_set"]]
    spine_nodes = [n for n in G.nodes() if n in spine_names]
    router_nodes = [n for n in G.nodes() if n not in view["host_set"] and n not in spine_names]

    edge_style = {
        "host": dict(edge_color="#9aa0a6", width=0.8, alpha=0.55, style="dotted"),
        "local": dict(edge_color="#4c78a8", width=1.6, alpha=0.85, style="solid"),
        "global": dict(edge_color="#e45756", width=2.6, alpha=0.9, style="solid"),
    }

    fig, ax = plt.subplots(figsize=(16, 16))
    fig.patch.set_facecolor("#11141a")
    ax.set_facecolor("#11141a")

    for kind, style in edge_style.items():
        edges = [(u, v) for u, v, _key in G.edges(keys=True)
                 if classify_link(u, v, view["host_set"], view["switch_group"]) == kind]
        nx.draw_networkx_edges(G, pos, ax=ax, edgelist=edges,
                                connectionstyle="arc3,rad=0.15", **style)

    nx.draw_networkx_nodes(G, pos, ax=ax, nodelist=host_nodes, node_shape="o", node_size=160,
                            node_color=[group_color[topo.host_map[n][0]] for n in host_nodes],
                            edgecolors="white", linewidths=0.5)
    nx.draw_networkx_nodes(G, pos, ax=ax, nodelist=router_nodes, node_shape="o", node_size=750,
                            node_color=[group_color[view["switch_group"][n]] for n in router_nodes],
                            edgecolors="white", linewidths=0.5)
    if spine_nodes:
        # Squares (vs. leaves'/routers' circles) so the leaf-spine fabric
        # structure that makes Dragonfly+ different is visible at a glance.
        nx.draw_networkx_nodes(G, pos, ax=ax, nodelist=spine_nodes, node_shape="s", node_size=950,
                                node_color=[group_color[view["switch_group"][n]] for n in spine_nodes],
                                edgecolors="white", linewidths=1.2)

    router_labels = {name: name for name in view["switches"]}
    nx.draw_networkx_labels(G, pos, ax=ax, labels=router_labels,
                             font_size=8, font_color="white", font_weight="bold")

    legend_handles = [
        mlines.Line2D([], [], color=edge_style["global"]["edge_color"], linewidth=2.6, label="Global-Group Link"),
        mlines.Line2D([], [], color=edge_style["local"]["edge_color"], linewidth=1.6, label="Local-Group Link"),
        mlines.Line2D([], [], color=edge_style["host"]["edge_color"], linewidth=0.8, label="Host Link"),
    ]
    if spine_nodes:
        legend_handles.append(mlines.Line2D([], [], color="white", marker="o", linestyle="None",
                                             markersize=10, label="Leaf (circle)"))
        legend_handles.append(mlines.Line2D([], [], color="white", marker="s", linestyle="None",
                                             markersize=10, label="Spine (square)"))
    else:
        legend_handles.append(mlines.Line2D([], [], color="white", marker="o", linestyle="None",
                                             markersize=10, label="Router (circle)"))
    legend_handles.append(mlines.Line2D([], [], color="white", marker="o", linestyle="None",
                                         markersize=5, label="Host (small dot)"))
    legend = ax.legend(handles=legend_handles, loc="upper left", bbox_to_anchor=(1, 1),
                        title="Legend", fontsize=10, title_fontsize=12, facecolor="#11141a",
                        edgecolor="white", labelcolor="white")
    legend.get_title().set_color("white")

    ax.set_title(topo.topology_label(), color="white", fontsize=15)
    ax.axis("off")
    plt.savefig(filename, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"*** Grouped topology plot saved to {filename} ***")


# ---------------------------------------------------------------------------
# Tests (pytest-discoverable: run `pytest visualize_dragonfly_topo.py`)
#
# Each test also saves a plot of the topology it builds, into
# DRAGONFLY_PLOTS_DIR/ for DragonflyTopo cases or DRAGONFLY_PLUS_PLOTS_DIR/
# for DragonflyPlusTopo cases, so every test case's structure can be
# inspected visually alongside its assertions.
# ---------------------------------------------------------------------------
DRAGONFLY_PLOTS_DIR = "dragonfly_plots"
DRAGONFLY_PLUS_PLOTS_DIR = "dragonfly_plus_plots"


def _plots_dir_for(topo):
    """DragonflyPlusTopo images go in their own directory, separate from
    DragonflyTopo's, so each topology's outputs stay easy to find."""
    return DRAGONFLY_PLUS_PLOTS_DIR if isinstance(topo, DragonflyPlusTopo) else DRAGONFLY_PLOTS_DIR


def _plot_case(name, topo, view):
    directory = _plots_dir_for(topo)
    os.makedirs(directory, exist_ok=True)
    plot_topology(topo, view, os.path.join(directory, f"{name}.png"))


def _grouped_plot_case(name, topo, view):
    directory = _plots_dir_for(topo)
    os.makedirs(directory, exist_ok=True)
    plot_topology_grouped(topo, view, os.path.join(directory, f"{name}.png"))


def _counts_by_kind(view):
    counts = {"host": 0, "local": 0, "global": 0}
    for n1, n2 in view["links"]:
        counts[classify_link(n1, n2, view["host_set"], view["switch_group"])] += 1
    return counts


def _expected_host_links(num_groups, routers_per_group, hosts_per_router):
    return num_groups * routers_per_group * hosts_per_router


def _expected_local_links(num_groups, routers_per_group):
    # all-to-all within each group: C(routers_per_group, 2) per group.
    return num_groups * routers_per_group * (routers_per_group - 1) // 2


def _expected_global_links(num_groups, global_links_per_pair):
    # global_links_per_pair links between every pair of groups.
    return num_groups * (num_groups - 1) // 2 * global_links_per_pair


def test_default_topology_counts():
    topo = DragonflyTopo()  # g=2, r=2, h=1
    view = build_topology_view(topo)
    _plot_case("default_topology", topo, view)
    counts = _counts_by_kind(view)
    assert counts["host"] == _expected_host_links(2, 2, 1)
    assert counts["local"] == _expected_local_links(2, 2)
    assert counts["global"] == _expected_global_links(2, 1)
    assert len(view["switches"]) == 2 * 2
    assert len(view["hosts"]) == 2 * 2 * 1


def test_larger_topology_counts():
    topo = DragonflyTopo(num_groups=3, routers_per_group=3, hosts_per_router=2, global_links_per_pair=2)
    view = build_topology_view(topo)
    _plot_case("larger_topology", topo, view)
    counts = _counts_by_kind(view)
    assert counts["host"] == _expected_host_links(3, 3, 2)
    assert counts["local"] == _expected_local_links(3, 3)
    assert counts["global"] == _expected_global_links(3, 2)


def test_single_router_per_group_has_no_local_links():
    # With one router per group there's nothing to wire "all-to-all" within
    # a group, so local links should be zero while global links still exist.
    topo = DragonflyTopo(num_groups=4, routers_per_group=1, hosts_per_router=1, global_links_per_pair=1)
    view = build_topology_view(topo)
    _plot_case("single_router_per_group", topo, view)
    counts = _counts_by_kind(view)
    assert counts["local"] == 0
    assert counts["global"] == _expected_global_links(4, 1)


def test_single_group_has_no_global_links():
    # With only one group there's no other group to connect to.
    topo = DragonflyTopo(num_groups=1, routers_per_group=4, hosts_per_router=2, global_links_per_pair=3)
    view = build_topology_view(topo)
    _plot_case("single_group", topo, view)
    counts = _counts_by_kind(view)
    assert counts["global"] == 0
    assert counts["local"] == _expected_local_links(1, 4)


def test_zero_hosts_per_router_has_no_hosts_or_host_links():
    topo = DragonflyTopo(num_groups=2, routers_per_group=2, hosts_per_router=0)
    view = build_topology_view(topo)
    _plot_case("zero_hosts_per_router", topo, view)
    assert view["hosts"] == []
    assert _counts_by_kind(view)["host"] == 0


def test_parallel_global_links_are_preserved_not_collapsed():
    # global_links_per_pair >= routers_per_group forces the round-robin
    # cursor in _add_global_links to wrap and reconnect the same router
    # pair twice. plot_topology() used to collapse these into one edge by
    # using a plain nx.Graph(); it must now use a MultiGraph and keep both.
    topo = DragonflyTopo(num_groups=2, routers_per_group=1, hosts_per_router=1, global_links_per_pair=2)
    view = build_topology_view(topo)
    _plot_case("parallel_global_links", topo, view)
    assert _counts_by_kind(view)["global"] == 2  # both parallel links present in topo.links()

    G = nx.MultiGraph()
    G.add_nodes_from(view["nodes"])
    G.add_edges_from(view["links"])
    assert G.number_of_edges() == len(view["links"])


def test_natural_sort_orders_double_digit_ids_numerically():
    topo = DragonflyTopo(num_groups=1, routers_per_group=11, hosts_per_router=1)
    view = build_topology_view(topo)
    _plot_case("natural_sort_double_digit_ids", topo, view)
    assert view["switches"] == [f"s0_{i}" for i in range(11)]


def test_classify_link_all_three_kinds():
    topo = DragonflyTopo(num_groups=2, routers_per_group=2, hosts_per_router=1, global_links_per_pair=1)
    view = build_topology_view(topo)
    _plot_case("classify_link_kinds", topo, view)
    host_set, switch_group = view["host_set"], view["switch_group"]

    assert classify_link("h0_0_0", "s0_0", host_set, switch_group) == "host"
    assert classify_link("s0_0", "s0_1", host_set, switch_group) == "local"
    assert classify_link("s0_0", "s1_0", host_set, switch_group) == "global"


def test_many_groups_with_variant_node_counts():
    # Asymmetric Dragonfly: each of the 6 groups has its own router count and
    # its own hosts-per-router count, so total nodes per group vary widely
    # (group 4 alone has 5 routers x 1 host = 5 hosts; group 3 has 2 routers
    # x 3 hosts = 6 hosts) -- exactly the "many groups, variant node counts"
    # case the uniform DragonflyTopo couldn't express before.
    routers_per_group = [2, 3, 4, 2, 5, 3]
    hosts_per_router = [1, 2, 1, 3, 1, 2]
    num_groups = 6
    global_links_per_pair = 2

    topo = DragonflyTopo(num_groups=num_groups, routers_per_group=routers_per_group,
                          hosts_per_router=hosts_per_router, global_links_per_pair=global_links_per_pair)
    view = build_topology_view(topo)
    _grouped_plot_case("many_groups_variant_nodes", topo, view)

    # Each group really has its own router/host count.
    for g in range(num_groups):
        routers_in_group = [name for (gg, _r), name in topo.router_map.items() if gg == g]
        assert len(routers_in_group) == routers_per_group[g]
        hosts_in_group = [name for name, (gg, _r, _h) in topo.host_map.items() if gg == g]
        assert len(hosts_in_group) == routers_per_group[g] * hosts_per_router[g]

    counts = _counts_by_kind(view)
    assert counts["host"] == sum(r * h for r, h in zip(routers_per_group, hosts_per_router))
    assert counts["local"] == sum(r * (r - 1) // 2 for r in routers_per_group)
    assert counts["global"] == _expected_global_links(num_groups, global_links_per_pair)
    assert len(view["switches"]) == sum(routers_per_group)


def test_dragonfly_plus_default_counts():
    topo = DragonflyPlusTopo(num_groups=2, leaves_per_group=2, spines_per_group=2,
                              hosts_per_leaf=1, global_links_per_pair=1)
    view = build_topology_view(topo)
    _plot_case("dragonfly_plus_default", topo, view)
    counts = _counts_by_kind(view)

    # Local links are the full leaf-spine bipartite fabric per group:
    # leaves_per_group * spines_per_group links per group.
    assert counts["local"] == 2 * (2 * 2)
    assert counts["host"] == _expected_host_links(2, 2, 1)
    assert counts["global"] == _expected_global_links(2, 1)
    assert len(view["switches"]) == 2 * (2 + 2)  # (leaves + spines) per group


def test_dragonfly_plus_leaf_spine_is_fully_bipartite():
    # The whole point of Dragonfly+ vs. Dragonfly: every leaf must reach
    # every spine in its group directly (a non-blocking Clos fabric), with
    # no leaf-leaf or spine-only-partial wiring.
    topo = DragonflyPlusTopo(num_groups=2, leaves_per_group=3, spines_per_group=2, hosts_per_leaf=1)
    links = set(frozenset(link) for link in topo.links())

    for g in range(2):
        for l in range(3):
            for s in range(2):
                leaf = topo.leaf_map[(g, l)]
                spine = topo.spine_map[(g, s)]
                assert frozenset((leaf, spine)) in links


def test_dragonfly_plus_global_links_only_touch_spines():
    # Leaves must never carry a global (inter-group) link in Dragonfly+ --
    # only spines do. global_links_per_pair=3 > spines_per_group=2 also
    # forces the round-robin cursor to wrap, exercising the same
    # parallel-link scenario test_parallel_global_links_are_preserved_not_collapsed
    # checks for canonical Dragonfly, but here on spines instead of routers.
    topo = DragonflyPlusTopo(num_groups=3, leaves_per_group=2, spines_per_group=2,
                              hosts_per_leaf=1, global_links_per_pair=3)
    view = build_topology_view(topo)
    leaf_names = set(topo.leaf_map.values())

    global_links = [(n1, n2) for n1, n2 in view["links"]
                     if classify_link(n1, n2, view["host_set"], view["switch_group"]) == "global"]
    assert len(global_links) == _expected_global_links(3, 3)
    for n1, n2 in global_links:
        assert n1 not in leaf_names and n2 not in leaf_names


def test_dragonfly_plus_many_groups_with_variant_node_counts():
    # Asymmetric Dragonfly+: each of the 5 groups has its own leaf count,
    # spine count, and hosts-per-leaf -- the leaf-spine analogue of
    # test_many_groups_with_variant_node_counts, rendered with the same
    # "cool" grouped visualizer (which draws spines as squares, leaves as
    # circles, so the fat-tree structure is visible per group).
    num_groups = 5
    leaves_per_group = [2, 4, 3, 2, 5]
    spines_per_group = [2, 2, 3, 1, 2]
    hosts_per_leaf = [1, 2, 1, 3, 1]
    global_links_per_pair = 2

    topo = DragonflyPlusTopo(num_groups=num_groups, leaves_per_group=leaves_per_group,
                              spines_per_group=spines_per_group, hosts_per_leaf=hosts_per_leaf,
                              global_links_per_pair=global_links_per_pair)
    view = build_topology_view(topo)
    _grouped_plot_case("dragonfly_plus_many_groups_variant_nodes", topo, view)

    for g in range(num_groups):
        assert sum(1 for (gg, _l) in topo.leaf_map if gg == g) == leaves_per_group[g]
        assert sum(1 for (gg, _s) in topo.spine_map if gg == g) == spines_per_group[g]

    counts = _counts_by_kind(view)
    assert counts["host"] == sum(l * h for l, h in zip(leaves_per_group, hosts_per_leaf))
    assert counts["local"] == sum(l * s for l, s in zip(leaves_per_group, spines_per_group))
    assert counts["global"] == _expected_global_links(num_groups, global_links_per_pair)


if __name__ == "__main__":
    os.makedirs(DRAGONFLY_PLOTS_DIR, exist_ok=True)
    os.makedirs(DRAGONFLY_PLUS_PLOTS_DIR, exist_ok=True)

    print(">>> Creating and printing default Dragonfly topology (g=2, r=2, h=1)...")
    default_topo = DragonflyTopo()
    default_view = build_topology_view(default_topo)
    print_topology_details(default_topo, default_view)
    plot_topology(default_topo, default_view, os.path.join(DRAGONFLY_PLOTS_DIR, "dragonfly_default.png"))

    print("\n" + "=" * 50 + "\n")

    print(">>> Creating and printing a larger Dragonfly topology (g=3, r=3, h=2, global_links=2)...")
    large_topo = DragonflyTopo(
        num_groups=3,
        routers_per_group=3,
        hosts_per_router=2,
        global_links_per_pair=2
    )
    large_view = build_topology_view(large_topo)
    print_topology_details(large_topo, large_view)
    plot_topology(large_topo, large_view, os.path.join(DRAGONFLY_PLOTS_DIR, "dragonfly_large.png"))

    print("\n" + "=" * 50 + "\n")

    print(">>> Creating and printing default Dragonfly+ topology (g=2, leaves=2, spines=2, h=1)...")
    default_plus_topo = DragonflyPlusTopo()
    default_plus_view = build_topology_view(default_plus_topo)
    print_topology_details(default_plus_topo, default_plus_view)
    plot_topology(default_plus_topo, default_plus_view,
                  os.path.join(DRAGONFLY_PLUS_PLOTS_DIR, "dragonfly_plus_default.png"))

    print("\n" + "=" * 50 + "\n")

    print(">>> Creating and printing a larger Dragonfly+ topology "
          "(g=3, leaves=3, spines=2, h=2, global_links=2)...")
    large_plus_topo = DragonflyPlusTopo(
        num_groups=3,
        leaves_per_group=3,
        spines_per_group=2,
        hosts_per_leaf=2,
        global_links_per_pair=2
    )
    large_plus_view = build_topology_view(large_plus_topo)
    print_topology_details(large_plus_topo, large_plus_view)
    plot_topology(large_plus_topo, large_plus_view,
                  os.path.join(DRAGONFLY_PLUS_PLOTS_DIR, "dragonfly_plus_large.png"))
