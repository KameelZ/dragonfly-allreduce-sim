#!/usr/bin/env python3
"""
run_full_testplan.py

Single entry point for DF_vs_DF_Plus_testplan.md: run every section with no
arguments. Each test_N_*.py file under experiments/ already hardcodes that
section's exact parameters from the plan, so this script's only job is to
run them in a sensible order (cheapest/fastest first, so a broken pipeline
fails fast instead of burning hours before anyone notices), enforce the
plan's operational rules between sections, tolerate an individual section
failing without losing the rest of the run, and leave one summary file
behind pointing at everything that was produced.

Operational rules enforced here (DF_vs_DF_Plus_testplan.md, "Operational
rules" -- a real incident this session was exactly this: two Mininet
benchmark scripts running at once, one's cleanup() killing the other's live
network):
  - Sections run strictly ONE AT A TIME via subprocess, never concurrently.
  - Before every section (and once at the very start): mininet.clean.cleanup()
    plus a safety-net `pkill -9 -f iperf` to clear any leaked iperf servers
    from a prior interrupted run (a ~2,300-process leak was found and fixed
    manually earlier this session -- this automates that check).

Usage:

    sudo python3 run_full_testplan.py
"""

import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# (script, expected output file, human description), cheapest/fastest first.
SECTIONS = [
    ("experiments/test_5_resource_parity.py", "results_test5_resource_parity.txt",
     "Section 5: resource-parity documentation (no root needed, seconds)"),
    ("experiments/test_0_sanity_identical.py", "results_test0_sanity.txt",
     "Section 0: measurement-floor sanity check"),
    ("experiments/test_6_latency_microbenchmark.py", "results_test6_latency.txt",
     "Section 6: per-hop latency microbenchmark (ping-based)"),
    ("experiments/test_4_adversarial_patterns.py", "results_test4_adversarial.txt",
     "Section 4: adversarial-pattern sweep (shift, nearest_neighbor)"),
    ("experiments/test_1_paper_comparable.py", "results_test1_paper_comparable.txt",
     "Section 1: paper-comparable sweep (headline result)"),
    ("experiments/test_2_scale_sweep.py", "results_test2_scale.txt",
     "Section 2: scale sweep (groups 4/6/8)"),
    ("experiments/test_3_mechanism_isolating.py", "results_test3_mechanism.txt",
     "Section 3: mechanism-isolating sweep + per-link utilization"),
]

SUMMARY_PATH = os.path.join(PROJECT_ROOT, "results_testplan_summary.txt")


def hygiene():
    """Tear down any leftover Mininet network and clear leaked iperf
    processes from a prior interrupted run, before every section."""
    try:
        from mininet.clean import cleanup
        cleanup()
    except Exception as e:
        print(f"  (hygiene: mininet cleanup() raised {type(e).__name__}: {e} -- continuing)")
    subprocess.run(["pkill", "-9", "-f", "iperf"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def run_section(script, expected_output, description, run_hygiene=True):
    """Run one section as its own process, clearing any stale output file
    first so it can't masquerade as a fresh success. Returns
    (description, script, expected_output, status, elapsed_s)."""
    print("=" * 70)
    print(description)
    print(f"  script: {script}")
    print("=" * 70)

    if run_hygiene:
        hygiene()

    output_path = os.path.join(PROJECT_ROOT, expected_output)
    if os.path.exists(output_path):
        os.remove(output_path)  # so a stale file from a previous run can't masquerade as success

    start = time.time()
    proc = subprocess.run([sys.executable, script], cwd=PROJECT_ROOT)
    elapsed = time.time() - start

    output_exists = os.path.exists(output_path)
    if proc.returncode == 0 and output_exists:
        status = "OK"
    elif proc.returncode == 0 and not output_exists:
        status = "SUSPECT (exit 0 but no output file)"
    else:
        status = f"FAILED (exit {proc.returncode})"

    print(f"  -> {status}  ({elapsed:.1f}s)\n")
    return (description, script, expected_output, status, elapsed)


def write_summary(results, total_elapsed, summary_path=None, title="DF_vs_DF_Plus_testplan.md -- full run summary"):
    summary_path = summary_path or SUMMARY_PATH
    lines = []
    lines.append(title)
    lines.append("=" * 70)
    lines.append(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"total wall-clock time: {total_elapsed / 60:.1f} minutes")
    lines.append("")
    for description, script, expected_output, status, elapsed in results:
        lines.append(f"[{status}] {description}")
        lines.append(f"    script: {script}")
        lines.append(f"    output: {expected_output}")
        lines.append(f"    time:   {elapsed:.1f}s")
        lines.append("")

    failed = [r for r in results if not r[3].startswith("OK")]
    if failed:
        lines.append(f"{len(failed)}/{len(results)} section(s) did not complete cleanly -- "
                     "check that section's own stdout (scroll back) for the actual error "
                     "before trusting any adjacent result file.")
    else:
        lines.append("All sections completed. Per DF_vs_DF_Plus_testplan.md's verification "
                     "step: sanity-check results_test0_sanity.txt's speedup numbers land "
                     "close to 1.0 with significant=False everywhere BEFORE trusting "
                     "results_test1/2/3/4's numbers -- if test_0 shows a significant "
                     "difference, the measurement pipeline itself is biased.")

    with open(summary_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return failed


def main():
    if os.geteuid() != 0:
        print("run_full_testplan.py must run as root (every section manipulates Mininet "
              "network namespaces) -- re-run with sudo.", file=sys.stderr)
        return 1

    print(f"DF_vs_DF_Plus_testplan.md -- running all {len(SECTIONS)} sections, "
         f"no further input needed.")
    print("Each section is a separate process, run strictly one at a time, with a "
         "cleanup pass before every section (see DF_vs_DF_Plus_testplan.md, "
         "'Operational rules').\n")

    overall_start = time.time()
    results = [run_section(script, expected_output, description)
              for script, expected_output, description in SECTIONS]
    total_elapsed = time.time() - overall_start

    hygiene()  # leave the host clean after the run too

    failed = write_summary(results, total_elapsed)

    print("=" * 70)
    print(f"Done. {len(results) - len(failed)}/{len(results)} sections OK. "
         f"Summary written to {SUMMARY_PATH}")
    print("=" * 70)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
