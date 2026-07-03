"""Probe output parsing and parallel gathering."""

import base64
import concurrent.futures as cf
import json
import re
import subprocess

from .nodes import _exec
from .probes import DYNAMIC_PROBE, PROBE

STATIC_KEYS = ("CPU_MODEL", "CORES", "THREADS", "MHZ_MAX", "MEM_KB", "ARCH", "KERNEL", "OS_NAME")


def gather_monitor(nodes, cache, timeout):
    with cf.ThreadPoolExecutor(max_workers=max(4, len(nodes))) as ex:
        return list(ex.map(lambda n: monitor_probe_one(n, cache, timeout), nodes))


def monitor_probe_one(node, cache, timeout):
    cached = cache.get(node["host"])
    script = DYNAMIC_PROBE if cached else PROBE
    try:
        r = _exec(node, script, timeout)
    except subprocess.TimeoutExpired:
        return {**node, "status": "timeout"}
    except subprocess.SubprocessError as e:
        return {**node, "status": "unreachable", "error": str(e)}
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return {**node, "status": "unreachable", "error": err[-1] if err else f"exit {r.returncode}"}
    info = parse(r.stdout)
    gdyn = info.pop("GPU_DYN", [])
    gstatic = info.pop("GPU_STATIC", [])
    if cached is None:
        cached = {"scalars": {k: info[k] for k in STATIC_KEYS if k in info}, "gpus": gstatic}
        cache[node["host"]] = cached
    merged = {**cached["scalars"], **info}
    gpus = merge_gpus(cached["gpus"], gdyn)
    try:
        ram_mib = int(merged["MEM_KB"]) // 1024
    except (KeyError, TypeError, ValueError):
        ram_mib = None
    if ram_mib:
        for g in gpus:
            if "mem_used" in g and "mem_total" not in g:
                g["mem_total"] = str(ram_mib)
    return {**node, "status": "ok", "GPUS": gpus, **merged}


def parse(text):
    info, gstatic, gdyn = {}, [], []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        k, _, v = line.partition("\t")
        if k == "GPU_STATIC":
            f = [x.strip() for x in v.split(",")]
            gstatic.append({"name": f[0], "mem_total": f[1], "driver": f[2]} if len(f) >= 3 else {"name": v})
        elif k == "GPU_DYN":
            f = [x.strip() for x in v.split(",")]
            if len(f) >= 3:
                gdyn.append({"mem_used": f[0], "util": f[1], "temp": f[2]})
        elif k == "GPU_PCI":
            gstatic.append({"name": v, "pci": True})
        elif k == "INTEL_B64":
            d = parse_intel(v)
            if d:
                gdyn.append(d)
        elif k == "GPUTOP_B64":
            d = parse_gputop(v)
            if d:
                gdyn.append(d)
        else:
            info[k] = v
    info["GPU_STATIC"] = gstatic
    info["GPU_DYN"] = gdyn
    return info


def parse_intel(b64):
    try:
        raw = base64.b64decode(b64).decode(errors="replace").strip()
    except (ValueError, TypeError):
        return None
    if not raw:
        return None
    raw = raw.rstrip().rstrip(",")
    if raw.startswith("["):
        if not raw.endswith("]"):
            raw += "]"
    else:
        raw = "[" + raw + "]"
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not arr:
        return None
    s = arr[-1]
    engines = s.get("engines") or {}
    busy = [e.get("busy", 0) for e in engines.values() if isinstance(e, dict)]
    d = {}
    if busy:
        d["util"] = str(round(max(busy)))
    freq = (s.get("frequency") or {}).get("actual")
    if freq is not None:
        d["freq"] = str(round(freq))
    return d or None


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


_SZ = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _parse_size(tok):
    m = re.fullmatch(r"([\d.]+)([BKMGT])i?B?", tok)
    return float(m.group(1)) * _SZ[m.group(2)] if m else None


def parse_gputop(b64):
    try:
        raw = base64.b64decode(b64).decode(errors="replace")
    except (ValueError, TypeError):
        return None
    iters = [s for s in raw.split("\x1b[H\x1b[J") if "DRM minor" in s]
    if not iters:
        return None
    last = _ANSI.sub("", iters[-1])
    best = None
    for b in last.split("DRM minor"):
        if "Frequency" not in b:
            continue
        sums = [0.0] * 5
        for line in b.splitlines():
            pcts = re.findall(r"([\d.]+)%", line)
            if len(pcts) >= 5:
                for i in range(5):
                    sums[i] += float(pcts[i])
        util = min(100.0, max(sums)) if sums else 0.0
        fm = re.search(r"GT0-(\d+)/\d+", b)
        if best is None or util > best[0]:
            best = (util, fm.group(1) if fm else None)
    if best is None:
        return None
    d = {"util": str(round(best[0]))}
    if best[1]:
        d["freq"] = best[1]
    mem_bytes = 0.0
    for line in last.splitlines():
        if "|" not in line:
            continue
        left = line.split("|", 1)[0].split()
        if len(left) >= 2 and left[0].isdigit():
            sz = _parse_size(left[1])
            if sz:
                mem_bytes += sz
    if mem_bytes > 0:
        d["mem_used"] = str(round(mem_bytes / 1024 / 1024))
    return d


def merge_gpus(static, dyn):
    out = []
    for i, s in enumerate(static):
        g = dict(s)
        if i < len(dyn):
            g.update(dyn[i])
        out.append(g)
    return out
