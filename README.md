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

## Project Structure

```text
.
├── dragonfly_topo.py   # Custom Dragonfly topology + g-PAARD / traffic injection points
└── README.md
```

## Roadmap

- [x] Foundational Mininet boilerplate and modular Dragonfly topology skeleton
- [ ] Full Dragonfly global-link assignment
- [ ] Dragonfly+ topology variant
- [ ] g-PAARD routing algorithm implementation
- [ ] Dynamic traffic load generators (iperf / custom)
- [ ] Metrics collection and analysis

## Status

Pre–Critical Design Review (CDR). The current codebase provides a clean,
modular foundation that boots in Mininet so the environment can be verified
before the routing and traffic-generation logic are implemented.
