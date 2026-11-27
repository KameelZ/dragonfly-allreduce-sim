# Closing the Dragonfly+ Performance Gap

**Adapting the g-PAARD all-reduce from Dragonfly to Dragonfly+**

Computer Communication & Networking Project (236340), Technion – Israel Institute of Technology
Team: Ramez Dahla, Kameel Zaid, Abd Alhadi Aghbaria · Advisor: Eran Tavor

📄 [Final report](docs/DF_vs_DF_Plus_Final_Report.pdf) · 🖼️ [Poster](docs/DF_vs_DF_Plus_Poster.pdf) · 🧪 [Test plan and investigation log](docs/DF_vs_DF_Plus_testplan.md) · 📋 [Assignment](docs/Sp2025-2026_DragonFly_Allreduce_Evaluation_v1.pdf)

---

## Overview

g-PAARD ([Ma et al., NPC 2021](docs/references/2022-Evaluation_of_Topology-Aware_All-Reduce_Algorithm_for_Dragonfly_Networks.pdf)) is a topology-aware all-reduce. On Dragonfly it finishes in six steps of at most one hop each. The paper studied it only on Dragonfly.

We did the following:

1. Built **Dragonfly** and **Dragonfly+** from scratch in Mininet.
2. Implemented g-PAARD and adapted it to Dragonfly+.
3. Measured real TCP traffic on both networks.

Adapted as is, g-PAARD took **up to 30% longer on Dragonfly+**. We traced the whole 33.5 s gap to the routing: all traffic between leaves went through a single spine. Two fixes closed it, **ECMP routing** and a **hierarchical schedule**.

## Key results

Setup: 4 groups × 8 hosts = 32 hosts, 10 Mbit/s links, 4 MB messages, 30 repeats.

| Metric | Dragonfly | Dragonfly+ before | Dragonfly+ after fixes |
|---|---|---|---|
| Total time (one all-reduce) | 110.4 s | 143.9 s | **103.1 s** |
| Flow completion time (per message) | 14.7 s | 19.6 s | **10.5 s** |
| Latency (ping RTT) | baseline | +33% to +67% | +33% to +67% |

With the same fixes, Dragonfly takes 103.3 s and 10.5 s per message, so the two networks match. The latency gap comes from the extra hops and amounts to milliseconds; the fixes don't change any path.

- **Small messages:** up to 256 KB there is no significant difference. The gap grows with message size and with lower bandwidth.
- **Root cause:** shortest-path routing kept one next hop per switch, so every leaf used spine 1. On the busiest leaf-to-spine link that meant 40.3 s against 23.5 s for Dragonfly's busiest link: 16.8 s × 2 steps = 33.5 s, which is the measured gap.
- **More spines alone:** 2× or 4× as many spines did not help (still +30%).
- **ECMP alone:** +25% with 4 spines, +4% with 8, +2% with 16.
- **Hierarchical schedule (with or without ECMP):** parity at every spine count, with no significant difference. Its 4 extra steps cost time below about 2 MB, so an adaptive selector picks the schedule from size ÷ bandwidth (threshold 0.24 s).
- **Why Dragonfly+ at all:** with 36-port switches it reaches 105,300 hosts against 29,412 for Dragonfly, 3.6× more at the same cost per host (calculated from the formulas in Kang et al., 2024).

## Repository layout

```text
.
├── dragonfly_topo.py          # Dragonfly topology, CLI, and routing (shortest path + ECMP)
├── dragonfly_plus_topo.py     # Dragonfly+ (leaf-spine groups, global links on spines)
├── gpaard.py                  # g-PAARD schedules (flat and hierarchical), Dragonfly+ role assignment
├── benchmark_gpaard.py        # Runs a schedule as real iperf/TCP flows; FCT; adaptive selector
├── verify_gpaard_correctness.py  # Checks the all-reduce result on 54 configurations
├── visualize_dragonfly_topo.py   # Draws the built topologies
├── run_full_testplan.py       # Runs tests 0-6 in order
├── run_tonight.py             # Runs tests 8, 1, 2, 10, 9 (hierarchical schedule + selector)
├── run_tonight_multipath.py   # Runs tests 11-12 (ECMP)
├── test_*.py                  # Unit tests (pytest)
├── experiments/
│   ├── test_0 … test_12_*.py  # One script per test-plan section (configs hard-coded)
│   ├── sweep_common.py        # Shared sweep engine (repeats, 95% CI, FCT, link utilization)
│   ├── link_utilization.py    # Per-link throughput sampler
│   └── benchmark_*.py, sweep_big_topology.py   # Early exploratory sweeps
├── results/
│   ├── results_test0 … test12_*.txt, results_*_summary.txt
│   ├── gpaard_correctness_report.txt, gpaard_small_case_detail.txt
│   └── early/                 # Early low-repeat sweeps (before the test plan)
├── plots/                     # Topology drawings produced by each test
└── docs/
    ├── DF_vs_DF_Plus_Final_Report.pdf
    ├── DF_vs_DF_Plus_Poster.pdf
    ├── DF_vs_DF_Plus_testplan.md
    ├── Sp2025-2026_DragonFly_Allreduce_Evaluation_v1.pdf
    └── references/            # The g-PAARD paper
```

## Test index

| Test | Script | What it measures | Results |
|---|---|---|---|
| 0 | `experiments/test_0_sanity_identical.py` | Measurement floor: the same network vs itself | `results/results_test0_sanity.txt` |
| 1 | `experiments/test_1_paper_comparable.py` | Paper-comparable config, 1 KB – 8 MB, 100 Mbit/s | `results/results_test1_paper_comparable.txt` |
| 2 | `experiments/test_2_scale_sweep.py` | 4, 6 and 8 groups | `results/results_test2_scale.txt` |
| 3 | `experiments/test_3_mechanism_isolating.py` | Bandwidth × size × global links, plus link utilization | `results/results_test3_mechanism.txt` |
| 4 | `experiments/test_4_adversarial_patterns.py` | Adversarial patterns (shift, nearest neighbor) | `results/results_test4_adversarial.txt` |
| 5 | `experiments/test_5_resource_parity.py` | Switch and link counts of both networks | `results/results_test5_resource_parity.txt` |
| 6 | `experiments/test_6_latency_microbenchmark.py` | Ping RTT per path type | `results/results_test6_latency.txt` |
| 7 | `experiments/test_7_spine_oversubscription.py` | 1×, 2× and 4× spines | `results/results_test7_spine_oversubscription.txt` |
| 8 | `experiments/test_8_hierarchical_pattern.py` | Hierarchical schedule vs flat | `results/results_test8_hierarchical_pattern.txt` |
| 9 | `experiments/test_9_adaptive_selector.py` | Selector check against tests 1, 2, 8 and 10 (no root needed) | `results/results_test9_adaptive_selector.txt` |
| 10 | `experiments/test_10_adaptive_calibration.py` | Selector threshold calibration (2 – 8 MB) | `results/results_test10_adaptive_calibration.txt` |
| 11 | `experiments/test_11_multipath_spines.py` | ECMP with 1×, 2× and 4× spines | `results/results_test11_multipath_spines.txt` |
| 12 | `experiments/test_12_hierarchical_plus_multipath.py` | Hierarchical + ECMP | `results/results_test12_hierarchical_plus_multipath.txt` |

## Requirements

- Linux (tested on Ubuntu) with [Mininet](http://mininet.org/download/) and Open vSwitch
- Python 3, `iperf`, `matplotlib`, `networkx`, `pytest`
- Root privileges, because Mininet creates network namespaces

```bash
sudo apt-get update
sudo apt-get install -y mininet iperf
sudo mn --test pingall     # check the Mininet install
```

## Usage

Run every command from the repository root; output paths are relative to it.

**Build a topology and open the Mininet CLI:**

```bash
sudo python3 dragonfly_topo.py -t dragonfly  --groups 4 --routers 4 --hosts 2
sudo python3 dragonfly_topo.py -t dragonfly+ --groups 4 --leaves 4 --spines 4 --hosts 2
sudo python3 dragonfly_topo.py --test        # non-interactive pingall, exit code 0 = all reachable
```

| Flag | Default | Description |
|---|---|---|
| `-t`, `--topology` | `dragonfly` | `dragonfly` or `dragonfly+` |
| `-g`, `--groups` | `2` | Number of groups |
| `-r`, `--routers` | `2` | Routers per group (Dragonfly) |
| `--leaves` / `--spines` | `2` / `2` | Leaves and spines per group (Dragonfly+) |
| `-H`, `--hosts` | `1` | Hosts per router or leaf |
| `--bw` | `10` | Per-link bandwidth (Mbit/s) |
| `--delay` | `1ms` | Per-link delay |
| `--global-links` | `1` | Global links per group pair |
| `--test` | off | Run `pingall` and exit pass/fail |
| `--settle` | `1` | Seconds to wait after installing flows |

**Run one benchmark:**

```bash
sudo python3 benchmark_gpaard.py --groups 4 --routers 4 --leaves 4 --spines 4 --hosts 2 \
     --patterns gpaard,gpaard_hierarchical --sizes 1M,4M --bw 10 --repeat 5
```

**Reproduce the test plan** (results go to `results/`, plots to `plots/`):

```bash
sudo python3 run_full_testplan.py        # tests 0-6
sudo python3 run_tonight.py              # tests 8, 1, 2, 10, 9
sudo python3 run_tonight_multipath.py    # tests 11-12 (about 8 hours)
sudo python3 experiments/test_7_spine_oversubscription.py
```

**Correctness and unit tests:**

```bash
python3 verify_gpaard_correctness.py     # writes results/gpaard_correctness_report.txt (54/54 PASS)
python3 -m pytest test_gpaard.py test_dragonfly_topo.py test_benchmark_gpaard.py
```

## Limitations

- **Scale:** the results are for 32 hosts in Mininet on one machine, with link speeds of 10–100 Mbit/s. They are not for full-size InfiniBand systems.
- **Network model:** every message is a TCP flow, so absolute times include TCP effects and aren't comparable to the paper's simulator.
- **Selector threshold:** it was tuned on the same data it was checked against, so it's a consistency check, not an independent test.
- **"No significant difference" is not proof of equality.**
