#!/usr/bin/env python3
"""
test_dragonfly_topo.py

Pytest suite for dragonfly_topo.py's routing/path-selection layer
(_shortest_path_next_hops, select_path_next_hop) -- pure graph algorithms, no
Mininet/root needed, same spirit as test_benchmark_gpaard.py. Built on
synthetic adjacency dicts (the same {switch: {neighbor: intf}} shape
_build_switch_graph produces from a live network, except intf values are
replaced with plain strings here since neither function under test touches
them).

This is the correctness gate for idea #5 (ECMP multipath across spines):
before trusting any performance number from it, every path produced by the
new multipath-aware selection must still be shortest-path and loop-free, and
selection must be deterministic ACROSS separate process invocations (not
just within one) -- see choose_next_hop's docstring on why plain hash() was
rejected.

    pytest test_dragonfly_topo.py
"""

import subprocess
import sys

import pytest

from dragonfly_topo import _shortest_path_next_hops, select_path_next_hop


def _bipartite_group(leaves, spines):
    """A single Dragonfly+-style group: `leaves` leaf switches, each wired to
    all `spines` spine switches (full leaf-spine bipartite fabric), no hosts.
    """
    leaf_names = [f"l{i}" for i in range(leaves)]
    spine_names = [f"sp{i}" for i in range(spines)]
    adj = {name: {} for name in leaf_names + spine_names}
    for leaf in leaf_names:
        for spine in spine_names:
            adj[leaf][spine] = f"{leaf}-{spine}"
            adj[spine][leaf] = f"{spine}-{leaf}"
    return adj, leaf_names, spine_names


def _full_mesh_group(routers):
    """A single canonical-Dragonfly-style group: every router directly wired
    to every other router (all-to-all mesh), no hosts."""
    names = [f"r{i}" for i in range(routers)]
    adj = {name: {} for name in names}
    for a in names:
        for b in names:
            if a != b:
                adj[a][b] = f"{a}-{b}"
    return adj, names


def _walk_path(adj, candidates_by_dst, src, dst, max_hops=50):
    """Follow select_path_next_hop from src to dst, returning the full path
    (list of switch names, src first, dst last). Raises if it loops or fails
    to terminate within max_hops -- the loop-freedom/termination check."""
    path = [src]
    current = src
    for _ in range(max_hops):
        if current == dst:
            return path
        next_hop = select_path_next_hop(current, dst, adj, candidates_by_dst[dst])
        assert next_hop is not None, f"no next hop from {current} toward {dst}"
        assert next_hop not in path, f"loop detected: {path + [next_hop]}"
        path.append(next_hop)
        current = next_hop
    raise AssertionError(f"did not reach {dst} from {src} within {max_hops} hops: {path}")


def _bfs_distance(adj, src, dst):
    """Reference shortest-path distance via plain BFS, independent of the
    code under test, to check chosen paths are truly minimal."""
    from collections import deque
    visited = {src}
    queue = deque([(src, 0)])
    while queue:
        node, d = queue.popleft()
        if node == dst:
            return d
        for n in adj[node]:
            if n not in visited:
                visited.add(n)
                queue.append((n, d + 1))
    raise AssertionError(f"{dst} unreachable from {src}")


# ---------------------------------------------------------------------------
# Multipath candidate set: the core idea #5 regression test.
# ---------------------------------------------------------------------------

def test_bipartite_leaf_has_all_spines_as_candidates():
    # Before this fix: _shortest_path_next_hops gave each leaf exactly ONE
    # next hop toward a cross-leaf destination (always the same spine,
    # regardless of how many existed). This is the direct regression test:
    # every spine in the group must be a valid candidate.
    adj, leaves, spines = _bipartite_group(leaves=4, spines=4)
    candidates = _shortest_path_next_hops(adj, dst_switch="l0")
    # l1, l2, l3 are all 2 hops from l0 (via any spine) -- every spine must
    # appear as a candidate for each of them.
    for src_leaf in ["l1", "l2", "l3"]:
        assert set(candidates[src_leaf]) == set(spines), (
            f"{src_leaf} should have all {len(spines)} spines as equal-cost "
            f"candidates, got {candidates[src_leaf]}")


def test_bipartite_traffic_actually_spreads_across_spines():
    # The point of idea #5: DIFFERENT source leaves sending to the SAME
    # destination leaf must land on DIFFERENT spines (not all converge on
    # spine 0 as before the fix) -- this is what lets DF+'s extra spine
    # hardware actually carry traffic.
    adj, leaves, spines = _bipartite_group(leaves=8, spines=8)
    candidates = _shortest_path_next_hops(adj, dst_switch="l0")
    chosen = {
        select_path_next_hop(src_leaf, "l0", adj, candidates)
        for src_leaf in leaves if src_leaf != "l0"
    }
    assert len(chosen) > 1, (
        f"all {len(leaves) - 1} source leaves chose the same spine "
        f"({chosen}) -- multipath is not actually spreading traffic")


def test_full_mesh_group_has_no_fan_out():
    # Canonical Dragonfly's intra-group router mesh is a direct 1-hop link
    # between any two routers -- there is no equal-cost fan-out to exploit,
    # so this change must be a mathematically provable no-op there: every
    # candidate list has exactly 1 entry (the destination itself).
    adj, routers = _full_mesh_group(routers=6)
    candidates = _shortest_path_next_hops(adj, dst_switch="r0")
    for src in routers:
        if src == "r0":
            continue
        assert candidates[src] == ["r0"], (
            f"full-mesh intra-group routing must stay single-hop/single-"
            f"candidate, got {candidates[src]} for {src}")


# ---------------------------------------------------------------------------
# Safety invariants: shortest-path + loop-free, regardless of which multipath
# candidate gets picked.
# ---------------------------------------------------------------------------

def test_multi_group_paths_are_shortest_and_loop_free():
    # Chain 3 Dragonfly+-style groups together via their spines (global
    # links), then verify EVERY (src, dst) switch pair across the whole
    # network: the path select_path_next_hop actually walks (a) terminates
    # at the destination, (b) has no repeated switch, (c) has exactly the
    # BFS-minimal hop count -- independent of which multipath candidate was
    # picked at each hop.
    adj = {}
    all_switches = []
    for g in range(3):
        g_adj, leaves, spines = _bipartite_group(leaves=3, spines=3)
        for name, neighbors in g_adj.items():
            renamed = f"g{g}_{name}"
            adj[renamed] = {f"g{g}_{n}": v for n, v in neighbors.items()}
        all_switches.extend(f"g{g}_{name}" for name in g_adj)
    # Global links: connect each group's spine 0 to the next group's spine 0
    # (a simple chain, enough to require multi-group routing).
    for g in range(2):
        a, b = f"g{g}_sp0", f"g{g+1}_sp0"
        adj[a][b] = "global"
        adj[b][a] = "global"

    for dst in all_switches:
        candidates = _shortest_path_next_hops(adj, dst_switch=dst)
        for src in all_switches:
            if src == dst:
                continue
            path = _walk_path(adj, {dst: candidates}, src, dst)
            assert path[-1] == dst
            assert len(set(path)) == len(path), f"repeated switch in path: {path}"
            expected = _bfs_distance(adj, src, dst)
            assert len(path) - 1 == expected, (
                f"{src}->{dst}: walked {len(path) - 1} hops, "
                f"shortest is {expected}: {path}")


# ---------------------------------------------------------------------------
# Determinism: must hold across separate process invocations, not just
# within one -- the exact bug class plain hash() would introduce silently.
# ---------------------------------------------------------------------------

def test_selection_deterministic_within_process():
    adj, leaves, spines = _bipartite_group(leaves=5, spines=5)
    candidates = _shortest_path_next_hops(adj, dst_switch="l0")
    first = select_path_next_hop("l1", "l0", adj, candidates)
    for _ in range(10):
        assert select_path_next_hop("l1", "l0", adj, candidates) == first


def test_selection_deterministic_across_process_invocations():
    # Spawn a fresh `python3` subprocess (a different PYTHONHASHSEED every
    # time unless explicitly disabled) and confirm it picks the same next
    # hop as this process. If select_path_next_hop used Python's builtin
    # hash() instead of zlib.crc32, this test would fail intermittently.
    script = (
        "from dragonfly_topo import _shortest_path_next_hops, select_path_next_hop\n"
        "adj = {}\n"
        "leaves = [f'l{i}' for i in range(5)]\n"
        "spines = [f'sp{i}' for i in range(5)]\n"
        "for name in leaves + spines:\n"
        "    adj[name] = {}\n"
        "for leaf in leaves:\n"
        "    for spine in spines:\n"
        "        adj[leaf][spine] = 'x'\n"
        "        adj[spine][leaf] = 'x'\n"
        "candidates = _shortest_path_next_hops(adj, dst_switch='l0')\n"
        "print(select_path_next_hop('l1', 'l0', adj, candidates))\n"
    )
    outputs = set()
    for _ in range(5):
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=".", capture_output=True, text=True,
            check=True)
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, (
        f"selection changed across process invocations: {outputs} -- "
        "likely using Python's randomized builtin hash() instead of a "
        "stable hash")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
