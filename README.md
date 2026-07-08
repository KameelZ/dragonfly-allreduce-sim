# dragonfly-allreduce-sim

> Performance analysis and optimization of Dragonfly network topologies and routing algorithms under dynamic traffic loads using Mininet.
>
> Technion course **236340**

## Overview

This project simulates and evaluates **Dragonfly** and **Dragonfly+** network
topologies under dynamic traffic loads. A core objective is implementing and
analyzing the **g-PAARD** routing algorithm within this architecture and
comparing its performance against baseline routing.

The simulation is built on [Mininet](http://mininet.org/) and Python 3, running
on an Ubuntu host.

## Goals

- Build a custom, parameterized Dragonfly topology in Mininet.
- Implement and analyze the **g-PAARD** routing algorithm.
- Drive the network with dynamic / synthetic traffic loads.
- Collect and analyze performance metrics (throughput, latency, load balance).

## Requirements

- Ubuntu (or other Linux with kernel support for Mininet)
- [Mininet](http://mininet.org/download/)
- Python 3
- Root privileges (Mininet manipulates network namespaces and interfaces)

## Installation

Install Mininet and its dependencies (skip if already installed):

```bash
sudo apt-get update
sudo apt-get install -y mininet
```

Verify the installation:

```bash
sudo mn --test pingall
```

## Usage

Run the base topology and drop into the Mininet CLI:

```bash
sudo python3 dragonfly_topo.py
```

Once at the `mininet>` prompt, verify baseline connectivity:

```text
mininet> pingall
```

Exit and tear down the network:

```text
mininet> exit
```

### Configuration

The topology is configurable from the command line:

| Flag | Default | Description |
| --- | --- | --- |
| `-g`, `--groups` | `2` | Number of Dragonfly groups |
| `-r`, `--routers` | `2` | Routers per group |
| `-H`, `--hosts` | `1` | Hosts per router |
| `--bw` | `10` | Per-link bandwidth (Mbit/s) |
| `--delay` | `1ms` | Per-link delay |
| `--global-links` | `1` | Global links per group pair |
| `--test` | off | Run a non-interactive `pingall` and exit pass/fail |
| `--settle` | `1` | Seconds to wait after installing flows before the `--test` ping |

Example — a larger network with richer global bandwidth:

```bash
sudo python3 dragonfly_topo.py --groups 4 --routers 3 --hosts 2 --global-links 2
```

### Smoke test

Verify end-to-end connectivity without the interactive CLI. This installs the
forwarding flows, runs an all-pairs ping, and exits non-zero on any packet loss:

```bash
sudo python3 dragonfly_topo.py --test
echo $?   # 0 = all hosts reachable, 1 = packet loss
```

## Project Structure

```text
.
├── dragonfly_topo.py   # Custom Dragonfly topology + g-PAARD / traffic injection points
└── README.md
```

## Roadmap

- [x] Foundational Mininet boilerplate and modular Dragonfly topology skeleton
- [x] Full Dragonfly global-link assignment
- [x] Command-line configuration (groups / routers / hosts / bandwidth)
- [x] Deterministic shortest-path forwarding (flow-based, controller-less)
- [ ] g-PAARD adaptive, load-aware routing policy
- [x] Dragonfly+ topology variant
- [ ] Dynamic traffic load generators (iperf / custom)
- [ ] Metrics collection and analysis

## Status

Pre–Critical Design Review (CDR). The current codebase provides a clean,
modular foundation that boots in Mininet so the environment can be verified
before the routing and traffic-generation logic are implemented.
