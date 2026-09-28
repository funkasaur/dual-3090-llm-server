#!/bin/sh
# Run once after 02:05 on 2026-09-27 (HA must have compiled sensor.home_energy's first own hour, 01:00):
# imports whole-home energy + cost history into HA's Energy panel. Log: ha_import_home.log
cd /home/aiuser/monitoring/smartlife-exporter || exit 1
{ date
  .venv/bin/python ha_import_stats.py sensor.home_energy BILLED
  .venv/bin/python ha_import_stats.py sensor.home_energy_cost BILLED 0.15
  date; } 2>&1 | tee -a ha_import_home.log
