# Agent instructions for dual-3090-llm-server

## Scope

Public build notes and example configurations for a dual-CCD Ryzen inference host with two
NVLink-bridged RTX 3090 GPUs.

## Repository layout

- `docs/` contains the host topology, interrupt placement, measurements and audit.
- `scripts/`, `systemd/` and `drivers/` contain host setup and service examples.
- `llamampere/` pins the inference backend and carries local build patches.
- `docker/` contains per-service Compose files and example configuration.
- `grafana/` contains alert rules and dashboards. `veeam/` contains guest snapshot hooks.
- `qwen-queue/README.md` points to the standalone queue.

## Build and deployment

- Start with `docs/cpu-topology.md` and derive local placement with
  `lscpu -e=CPU,CORE,CACHE`.
- The backend build entry point is `./llamampere/build.sh`. See its README for
  prerequisites.
- Read the host-script notes before enabling the example units.
- Follow the root README for the shared Docker network and per-service Compose startup.
- Fill in local configuration separately before starting a service.
- This checkout documents and configures multiple services. It has no top-level test runner.

## Conventions

- Keep host-dependent sample values identifiable as examples.
- Record dated measurements and retain audit findings that explain inactive or ineffective
  tuning.
- Prefer observed measurements when revising claims about topology, performance and energy
  use.
- Keep service parameters in configuration examples. Keep credentials and private host
  identifiers out of public files.
- The Compose examples assume a trusted LAN. Follow the README's bind-address and
  authentication guidance before changing exposure.

## Reviewing changes

- Check the hardware and inference sections when changing details that depend on the CPU,
  memory or GPU configuration.
- Keep public links grounded in this checkout and preserve the README's references to the
  related standalone projects.
- Use the audit and driver notes to check proposed changes to IRQ pinning, power settings,
  or network interfaces.
