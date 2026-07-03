"""Tailnet discovery and remote exec."""

import json
import subprocess

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/cm-tailfleet-%C",
    "-o", "ControlPersist=60",
]


def all_nodes():
    raw = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True, text=True, check=True,
    ).stdout
    d = json.loads(raw)
    out = []
    items = [(d.get("Self"), True)] + [(p, False) for p in (d.get("Peer") or {}).values()]
    for entry, is_self in items:
        if not entry or entry.get("OS") != "linux":
            continue
        if not is_self and not entry.get("Online"):
            continue
        ips = entry.get("TailscaleIPs") or []
        out.append({"host": entry.get("HostName"), "ip": ips[0] if ips else None, "self": is_self})
    return out


def _exec(node, script, timeout, args=None):
    cmd = ["bash", "-s"] if node["self"] else ["ssh", *SSH_OPTS, node["ip"], "bash", "-s"]
    if args:
        cmd += ["--", *args]
    return subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
