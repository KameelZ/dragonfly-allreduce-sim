#!/usr/bin/env python3
"""
run_tonight.py

Focused overnight run: the complete hierarchical-vs-flat (optimization idea
#2) evidence trail, regenerated fresh in one pass -- for presenting the
result to the professor with every number freshly measured, not mixed
old/new.

  1. test_8_hierarchical_pattern.py -- the headline result: worst-case
     condition (groups=4, bw=10Mbit, size=4M), decisive absolute win for
     DF+ (144.1s -> 103.5s), gap eliminated (0.754 -> 0.999, not
     significant).
  2. test_1_paper_comparable.py -- generalization across message size
     (1K..1M, bw=100Mbit): does the gap stay closed at every size?
  3. test_2_scale_sweep.py -- generalization across scale (groups=4/6/8):
     does it hold as the topology grows?
  4. test_10_adaptive_calibration.py -- calibration run for the adaptive
     pattern selector (benchmark_gpaard.choose_gpaard_pattern_name): real
     data (2M/4M/8M at bw=100Mbit) inside the previously-uncalibrated gap
     between the known loss and the known win, to confirm or correct the
     selector's threshold placement.
  5. test_9_adaptive_selector.py -- runs LAST (no root needed, but depends
     on the four files above): retrospective check that the selector's
     pick matches the empirically faster schedule at every condition,
     now including test_10's fresh calibration data.

Correctness of gpaard_hierarchical itself was already verified before any
performance claim was trusted (test_gpaard.py's test_hierarchical_* tests,
40 topology configs, 31/31 pytest) -- this run is purely about regenerating
the PERFORMANCE evidence fresh, not about correctness again.

idea #1 (spine oversubscription, test_7) already ran and showed hardware
alone does nothing (no ECMP in this project's routing) -- not re-run here.

Reuses run_full_testplan.py's hygiene()/run_section()/write_summary() as-is
(same one-at-a-time execution, same pre-section cleanup, same per-section
failure tolerance) with a different SECTIONS list and a separate summary
file so this run's summary doesn't overwrite run_full_testplan.py's.

Usage:

    sudo python3 run_tonight.py
"""

import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from run_full_testplan import hygiene, run_section, write_summary

# (script, expected output file, human description).
# Order matters: test_9 must run LAST since it parses the result files the
# other four sections write -- running it earlier would read stale data.
SECTIONS = [
    ("experiments/test_8_hierarchical_pattern.py", "results_test8_hierarchical_pattern.txt",
     "Idea #2 headline: worst-case condition, gpaard vs gpaard_hierarchical"),
    ("experiments/test_1_paper_comparable.py", "results_test1_paper_comparable.txt",
     "Section 1 + idea #2: gpaard vs gpaard_hierarchical across the full size sweep"),
    ("experiments/test_2_scale_sweep.py", "results_test2_scale.txt",
     "Section 2 + idea #2: gpaard vs gpaard_hierarchical across groups=4/6/8"),
    ("experiments/test_10_adaptive_calibration.py", "results_test10_adaptive_calibration.txt",
     "Adaptive selector calibration: real data inside the uncalibrated threshold gap"),
    ("experiments/test_9_adaptive_selector.py", "results_test9_adaptive_selector.txt",
     "Adaptive selector retrospective validation (no root needed, runs last on purpose)"),
]

SUMMARY_PATH = os.path.join(PROJECT_ROOT, "results_tonight_summary.txt")


def main():
    if os.geteuid() != 0:
        print("run_tonight.py must run as root (every section manipulates Mininet "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    print(f"run_tonight.py -- running {len(SECTIONS)} sections (the full hierarchical-vs-flat "
         "evidence trail, regenerated fresh: headline result, size/scale generalization, "
         "adaptive-selector calibration, and retrospective validation), no further input "
         "needed.")
    print("Each section is a separate process, run strictly one at a time, with a "
         "cleanup pass before every section.\n")

    overall_start = time.time()
    results = [run_section(script, expected_output, description)
              for script, expected_output, description in SECTIONS]
    total_elapsed = time.time() - overall_start

    hygiene()  # leave the host clean after the run too

    failed = write_summary(
        results, total_elapsed, summary_path=SUMMARY_PATH,
        title="run_tonight.py -- full hierarchical-vs-flat evidence trail (test_8, test_1, test_2, test_10, test_9)",
    )

    print("=" * 70)
    print(f"Done. {len(results) - len(failed)}/{len(results)} sections OK. "
         f"Summary written to {SUMMARY_PATH}")
    print("=" * 70)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
