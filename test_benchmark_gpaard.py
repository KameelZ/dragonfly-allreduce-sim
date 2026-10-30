#!/usr/bin/env python3
"""
test_benchmark_gpaard.py

Pytest suite for benchmark_gpaard.py's adaptive pattern selector
(choose_gpaard_pattern_name). Pure arithmetic, no root/Mininet needed -- the
selector only returns which already-verified schedule NAME to use; the
schedules themselves are verified separately in test_gpaard.py.

    pytest test_benchmark_gpaard.py
"""

import pytest

from benchmark_gpaard import choose_gpaard_pattern_name, PATTERNS, ADAPTIVE_TRANSFER_TIME_THRESHOLD_S


def test_matches_known_contention_bound_win():
    # test_8_hierarchical_pattern.py's condition: gpaard_hierarchical was a
    # decisive absolute win here (DF+ 144.1s -> 103.5s).
    assert choose_gpaard_pattern_name(4194304, 10) == "gpaard_hierarchical"


@pytest.mark.parametrize("size_bytes", [1024, 4096, 16384, 65536, 262144, 1048576])
def test_matches_known_overhead_bound_losses(size_bytes):
    # test_1_paper_comparable.py's full size sweep at bw=100Mbit: flat
    # gpaard won in absolute terms at every one of these sizes.
    assert choose_gpaard_pattern_name(size_bytes, 100) == "gpaard"


@pytest.mark.parametrize("size_bytes,bw_mbit", [
    (1024, 100), (1048576, 100), (4194304, 10), (1048576, 10), (4194304, 100),
])
def test_always_returns_a_valid_pattern_name(size_bytes, bw_mbit):
    assert choose_gpaard_pattern_name(size_bytes, bw_mbit) in PATTERNS


def test_monotonic_in_size_at_fixed_bandwidth():
    # Larger messages take longer to cross the link, so they should be at
    # least as likely to trigger the hierarchy as smaller ones.
    small = choose_gpaard_pattern_name(1024, 10)
    large = choose_gpaard_pattern_name(100 * 1024 * 1024, 10)
    assert small == "gpaard"
    assert large == "gpaard_hierarchical"


def test_monotonic_in_bandwidth_at_fixed_size():
    # Lower bandwidth makes the same message take longer to cross the link,
    # so it should be at least as likely to trigger the hierarchy as higher
    # bandwidth.
    size_bytes = 4194304
    slow_link = choose_gpaard_pattern_name(size_bytes, 1)
    fast_link = choose_gpaard_pattern_name(size_bytes, 1000)
    assert slow_link == "gpaard_hierarchical"
    assert fast_link == "gpaard"


def test_threshold_boundary_is_inclusive_toward_hierarchical():
    # size_bytes chosen so transfer_time_s lands exactly on the threshold:
    # size_bytes * 8 / (bw_mbit * 1e6) == threshold_s
    bw_mbit = 10
    threshold_s = ADAPTIVE_TRANSFER_TIME_THRESHOLD_S
    size_at_boundary = int(threshold_s * bw_mbit * 1e6 / 8)
    assert choose_gpaard_pattern_name(size_at_boundary, bw_mbit) == "gpaard_hierarchical"
    assert choose_gpaard_pattern_name(size_at_boundary - 1024, bw_mbit) == "gpaard"


def test_custom_threshold_override():
    # A caller-supplied threshold must actually take effect, not just the
    # module default.
    assert choose_gpaard_pattern_name(1024, 10, threshold_s=0.0) == "gpaard_hierarchical"
    assert choose_gpaard_pattern_name(100 * 1024 * 1024, 10, threshold_s=1e9) == "gpaard"
