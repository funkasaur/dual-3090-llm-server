---
name: configure-inference-host
description: Review and apply the documented example setup for a dual-GPU inference host.
---

# Review and apply the documented example setup for a dual-GPU inference host

1. Read `docs/cpu-topology.md` and derive placement for the target machine with `lscpu -e=CPU,CORE,CACHE`.
2. Review prerequisites and dry runs in `scripts/README.md` before installing and enabling the host scripts and units.
3. Follow the root README to create the shared Docker network and build the pinned backend with `./llamampere/build.sh`.
4. Prepare the local model and per-service configuration. Review bind addresses and authentication before starting Compose services.
5. Compare measurements and observations against the audit when validating the setup.
