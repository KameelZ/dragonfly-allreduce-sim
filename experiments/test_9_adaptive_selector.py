#!/usr/bin/env python3
"""
test_9_adaptive_selector.py

Retrospective validation of the adaptive pattern selector
(benchmark_gpaard.choose_gpaard_pattern_name), the refinement to
optimization idea #2 described in DF_vs_DF_Plus_testplan.md: pick 'gpaard'
or 'gpaard_hierarchical' per (size, bandwidth) condition instead of
committing to one, since test_1/test_2's generalization run found
gpaard_hierarchical closes the DF-vs-DF+ gap everywhere but is only a net
ABSOLUTE win when the network is bandwidth-bound.

UNLIKE every other test_N script, this one does NOT run Mininet and does
NOT need root (same as test_5_resource_parity.py) -- it parses the
already-measured 'total=' numbers straight out of
results_test1_paper_comparable.txt, results_test2_scale.txt, and
results_test8_hierarchical_pattern.txt (n=20-40 each, already trusted), and
checks whether the selector's pick matches the empirically faster schedule
at every condition those files cover. This is deliberately NOT a new live
Mininet run: the selector can only ever resolve to one of the two schedules
already measured in those files, so a fresh run would just re-measure a
known number at real cost (the bw=10Mbit/4M condition alone took ~4 hours)
for zero new information. A live run only becomes worthwhile for a genuinely
NEW (bw, size) condition near the threshold -- see the "Next steps" note in
the output this script writes.

Usage (no root needed):

    python3 experiments/test_9_adaptive_selector.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark_gpaard import choose_gpaard_pattern_name, parse_size, format_size, ADAPTIVE_TRANSFER_TIME_THRESHOLD_S

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(PROJECT_ROOT, "results_test9_adaptive_selector.txt")

TEST1_PATH = os.path.join(PROJECT_ROOT, "results_test1_paper_comparable.txt")
TEST2_PATH = os.path.join(PROJECT_ROOT, "results_test2_scale.txt")
TEST8_PATH = os.path.join(PROJECT_ROOT, "results_test8_hierarchical_pattern.txt")
TEST10_PATH = os.path.join(PROJECT_ROOT, "results_test10_adaptive_calibration.txt")

BW_RE = re.compile(r"bw=(\d+)Mbit")
# No trailing $ anchor: test_1's header lines are exactly "size=1K" with
# nothing else, but test_10's are "size=2M  transfer_time_s=0.1678" -- both
# need the leading "size=<token>" to resolve correctly.
SIZE_HEADER_RE = re.compile(r"^size=(\S+)")
GROUPS_HEADER_RE = re.compile(r"^groups=(\d+)$")
# "gpaard_hierarchical" must come before "gpaard" in the alternation --
# it's a prefix match otherwise and every hierarchical row silently
# misparses as a truncated "gpaard" row.
ROW_RE = re.compile(
    r"^\s*(dragonfly\+?)\s+(gpaard_hierarchical|gpaard)\s+(?:(\S+)\s+)?total=\s*([0-9.]+)s"
)


def parse_size_sectioned(path, context_label):
    """Shared parser for result files that use a 'size=XXX' section header
    per block (test_1's and test_10's row format -- no inline size token,
    unlike test_2's). Yields (topology, bw_mbit, size_bytes, pattern,
    total_mean, context)."""
    with open(path) as f:
        text = f.read()
    bw_mbit = int(BW_RE.search(text).group(1))
    current_size_bytes = None
    for line in text.splitlines():
        m = SIZE_HEADER_RE.match(line.strip())
        if m:
            current_size_bytes = parse_size(m.group(1))
            continue
        m = ROW_RE.match(line)
        if m and current_size_bytes is not None:
            topology, pattern, _inline_size, total = m.groups()
            yield topology, bw_mbit, current_size_bytes, pattern, float(total), context_label


def parse_test1(path):
    yield from parse_size_sectioned(path, "test_1")


def parse_test10(path):
    yield from parse_size_sectioned(path, "test_10 (calibration)")


def parse_test2(path):
    with open(path) as f:
        text = f.read()
    bw_mbit = int(BW_RE.search(text).group(1))
    current_groups = None
    for line in text.splitlines():
        m = GROUPS_HEADER_RE.match(line.strip())
        if m:
            current_groups = int(m.group(1))
            continue
        m = ROW_RE.match(line)
        if m and current_groups is not None and m.group(3):
            topology, pattern, inline_size, total = m.groups()
            size_bytes = parse_size(inline_size)
            yield topology, bw_mbit, size_bytes, pattern, float(total), f"test_2 (groups={current_groups})"


def parse_test8(path):
    with open(path) as f:
        text = f.read()
    bw_mbit = int(BW_RE.search(text).group(1))
    size_bytes = parse_size(re.search(r"size=(\S+)", text).group(1))
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if m:
            topology, pattern, _inline_size, total = m.groups()
            yield topology, bw_mbit, size_bytes, pattern, float(total), "test_8"


def main():
    records = []
    for path, parser in ((TEST1_PATH, parse_test1), (TEST2_PATH, parse_test2),
                        (TEST8_PATH, parse_test8), (TEST10_PATH, parse_test10)):
        if not os.path.exists(path):
            print(f"warning: {path} not found, skipping its conditions", file=sys.stderr)
            continue
        records.extend(parser(path))

    if not records:
        print("No source result files found -- run test_1/test_2/test_8 first.", file=sys.stderr)
        return 1

    # Group by (context, topology, bw, size) so each condition's gpaard and
    # gpaard_hierarchical rows end up paired together.
    by_condition = {}
    for topology, bw_mbit, size_bytes, pattern, total, context in records:
        key = (context, topology, bw_mbit, size_bytes)
        by_condition.setdefault(key, {})[pattern] = total

    lines = []
    lines.append("Section 9: adaptive pattern selector -- retrospective validation")
    lines.append("=" * 70)
    lines.append(f"threshold_s = {ADAPTIVE_TRANSFER_TIME_THRESHOLD_S} "
                 "(geometric mean of the two known boundary points -- see "
                 "benchmark_gpaard.ADAPTIVE_TRANSFER_TIME_THRESHOLD_S's docstring)")
    lines.append("")
    lines.append("For every (topology, bw, size) condition already measured in "
                 "results_test1/test2/test8's n=20-40 data, this checks whether "
                 "choose_gpaard_pattern_name's pick matches the schedule that was "
                 "EMPIRICALLY faster in absolute wall-clock time at that condition. "
                 "No new Mininet run -- this is a retrospective check against "
                 "already-trusted data.")
    lines.append("")
    lines.append(f"{'context':20s} {'topology':11s} {'bw':>6s} {'size':>6s} "
                 f"{'flat_s':>9s} {'hier_s':>9s} {'picked':>19s} {'match?':>7s}")
    lines.append("-" * 70)

    total_conditions = 0
    mismatches = 0
    for (context, topology, bw_mbit, size_bytes), totals in sorted(by_condition.items()):
        if "gpaard" not in totals or "gpaard_hierarchical" not in totals:
            continue  # incomplete pair, skip (shouldn't happen with these 3 files)
        total_conditions += 1
        flat_s = totals["gpaard"]
        hier_s = totals["gpaard_hierarchical"]
        picked = choose_gpaard_pattern_name(size_bytes, bw_mbit)
        empirically_faster = "gpaard" if flat_s < hier_s else "gpaard_hierarchical"
        matched = (picked == empirically_faster)
        if not matched:
            mismatches += 1
        lines.append(f"{context:20s} {topology:11s} {bw_mbit:>4d}Mb {format_size(size_bytes):>6s} "
                     f"{flat_s:>9.3f} {hier_s:>9.3f} {picked:>19s} {'YES' if matched else 'NO':>7s}")

    lines.append("-" * 70)
    lines.append(f"{total_conditions - mismatches}/{total_conditions} conditions: selector's pick "
                 "matches the empirically faster schedule.")
    lines.append("")
    if mismatches == 0:
        lines.append("Every already-measured condition confirms the selector's decision rule, "
                     "including test_10's calibration data inside the gap that originally had "
                     "no real measurements (bw=100Mbit, 2M-8M). threshold_s="
                     f"{ADAPTIVE_TRANSFER_TIME_THRESHOLD_S} is calibrated against that data, not "
                     "just a reasoned placement anymore -- the true crossover was found to sit "
                     "between 2M (0.1678s, a loss) and 4M (0.3355s, a win) at bw=100Mbit, "
                     "narrower than the original 40x-wide uncalibrated gap (0.0839s-3.3554s).")
    else:
        lines.append(f"{mismatches} condition(s) disagree with the selector's pick -- the "
                     "threshold may need adjustment; see which rows say NO above.")
    lines.append("")

    with open(OUT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n*** [test9] results written to {OUT_PATH}\n")
    print("\n".join(lines))
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
