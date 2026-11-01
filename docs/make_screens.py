#!/usr/bin/env python3
"""Render the README's screenshots from fixture.json, with no screen or Proxmox.

    python3 docs/make_screens.py
"""
import copy
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import vocore_status as v

OUT = ROOT / "docs" / "screens"
os.environ["TZ"] = "UTC"
time.tzset()
fixture = json.loads((ROOT / "fixture.json").read_text())
now = fixture["now"]


def five_hosts():
    """The summary view is for more hosts than the fixture has."""
    data = copy.deepcopy(fixture)
    for number, cpu, mem in ((3, 0.34, 0.52), (4, 0.81, 0.78), (5, 0.05, 0.31)):
        name = f"pve{number}"
        data["status"].append({"type": "node", "name": name, "online": 1})
        data["resources"].append({"type": "node", "node": name, "cpu": cpu, "mem": mem * 3e10,
                                  "maxmem": 3e10, "uptime": 4000000})
        data["zfs"][name] = fixture["zfs"]["pve1"]
    return data


def problems():
    """One of each kind of bad news, to show what it looks like."""
    data = copy.deepcopy(fixture)
    data["status"][0]["quorate"] = 0
    data["status"][2]["online"] = 0
    data["zfs"]["pve1"][0]["health"] = "DEGRADED"
    data["tasks"][0]["status"] = "job errors"
    data["replication"]["pve1"][0]["fail_count"] = 2
    data["pressure"]["pve1"]["io"] = 38.0
    data["temps"]["pve1"]["nvme"] = 79
    data["resources"][0]["mem"] = 31500000000
    return data


for name, data, view, page, paused in (
        ("hosts", fixture, "hosts", 1, False),
        ("summary", five_hosts(), "summary", 1, False),
        ("host-guests", fixture, "guests", 1, False),
        ("storage", fixture, "guests", 2, False),
        ("guest-vm", fixture, "guests", 3, False),
        ("guest-stopped", fixture, "guests", 4, False),
        ("guest-container", fixture, "guests", 7, False),
        ("paused", fixture, "guests", 3, True),
        ("problems", problems(), "hosts", 1, False)):
    image = v.render(data, {**v.DEFAULTS, "view": view}, now, page - 1, paused)
    image.save(OUT / f"{name}.png", optimize=True)
