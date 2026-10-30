#!/usr/bin/env python3
"""
run_tonight_multipath.py

Overnight run for optimization ideas #5 and #6: the ECMP multipath routing
fix (dragonfly_topo.py), tested alone (test_11) and combined with idea #2's
hierarchical schedule (test_12).

  1. test_11_multipath_spines.py -- idea #5 alone: re-runs
     test_7_spine_oversubscription.py's exact config (same 4 variants, same
     worst-case bw=10Mbit/4M condition) with flat gpaard, now that the
     routing layer can actually reach every spine instead of funneling all
     cross-leaf traffic through one.
  2. test_12_hierarchical_plus_multipath.py -- idea #6: the same 4 variants
     and condition, but gpaard_hierarchical instead of flat gpaard -- idea
     #2 and idea #5 combined, the most promising remaining path to an
     outright DF+ win (not just parity) this project has identified.

No dependency between the two -- same underlying routing fix, different
message pattern -- so order between them doesn't matter; they just both
need a clean Mininet state before each one, same as every other section.

Correctness of the routing fix itself (shortest-path, loop-free,
deterministic across process runs) was already verified in
test_dragonfly_topo.py (6/6 pytest) before either of these performance runs
was written -- this run is purely about regenerating PERFORMANCE evidence,
not about correctness again.

Does NOT re-run the earlier results_tonight_summary.txt trail (test_8/
test_1/test_2/test_10/test_9) -- that already completed and is unaffected
by this change (it measures the hierarchical schedule's own effect, not
routing). Only the two new sections run here.

Reuses run_full_testplan.py's hygiene()/run_section()/write_summary() as-is,
with its own separate summary file.

Usage:

    sudo python3 run_tonight_multipath.py
"""

import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from run_full_testplan import hygiene, run_section, write_summary

SECTIONS = [
    ("experiments/test_11_multipath_spines.py", "results_test11_multipath_spines.txt",
     "Idea #5: ECMP multipath across spines, flat gpaard, same condition as test_7"),
    ("experiments/test_12_hierarchical_plus_multipath.py", "results_test12_hierarchical_plus_multipath.txt",
     "Idea #6: idea #2 (hierarchical) + idea #5 (multipath) combined"),
]

SUMMARY_PATH = os.path.join(PROJECT_ROOT, "results_tonight_multipath_summary.txt")


def main():
    if os.geteuid() != 0:
        print("run_tonight_multipath.py must run as root (every section manipulates Mininet "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    print(f"run_tonight_multipath.py -- running {len(SECTIONS)} sections (idea #5 alone, then "
         "idea #2+#5 combined), no further input needed.")
    print("Each section is a separate process, run strictly one at a time, with a "
         "cleanup pass before every section.\n")

    overall_start = time.time()
    results = [run_section(script, expected_output, description)
              for script, expected_output, description in SECTIONS]
    total_elapsed = time.time() - overall_start

    hygiene()  # leave the host clean after the run too

    failed = write_summary(
        results, total_elapsed, summary_path=SUMMARY_PATH,
        title="run_tonight_multipath.py -- idea #5 (multipath) and idea #6 (hierarchical + multipath)",
    )

    print("=" * 70)
    print(f"Done. {len(results) - len(failed)}/{len(results)} sections OK. "
         f"Summary written to {SUMMARY_PATH}")
    print("=" * 70)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
