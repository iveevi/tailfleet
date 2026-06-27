#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich", "textual"]
# ///
import argparse
import base64
import concurrent.futures as cf
import getpass
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from collections import deque

from rich import box
from rich.console import Console
from rich.table import Table
from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=accept-new",
]

DEFAULT_EXCLUDES = [".git", ".venv", "__pycache__", "node_modules", "*.pyc"]
ID_RE = re.compile(r"^[\w.-]+$")

PROBE = r"""
emit() { printf '%s\t%s\n' "$1" "$2"; }
cpu_sample() { awk '/^cpu /{idle=$5+$6; tot=0; for(i=2;i<=NF;i++) tot+=$i; print tot, idle}' /proc/stat; }
read t1 i1 < <(cpu_sample); sleep 0.3; read t2 i2 < <(cpu_sample)
emit CPU_UTIL   "$(awk -v t1=$t1 -v i1=$i1 -v t2=$t2 -v i2=$i2 'BEGIN{dt=t2-t1;di=i2-i1; if(dt>0) printf "%.0f",(1-di/dt)*100; else printf "0"}')"
emit MEM_USED_KB "$(awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t-a}' /proc/meminfo)"
emit CPU_MODEL  "$(lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1)"
emit CORES      "$(nproc)"
emit THREADS    "$(lscpu | sed -n 's/^CPU(s):[[:space:]]*//p' | head -1)"
emit MHZ_MAX    "$(lscpu | sed -n 's/^CPU max MHz:[[:space:]]*//p' | head -1)"
emit MEM_KB     "$(awk '/MemTotal/{print $2}' /proc/meminfo)"
emit ARCH       "$(uname -m)"
emit KERNEL     "$(uname -r)"
emit OS_NAME    "$(. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu,driver_version \
    --format=csv,noheader,nounits 2>/dev/null | while IFS= read -r line; do
      emit GPU "$line"
    done
else
  lspci 2>/dev/null | grep -iE 'vga|3d|display' | sed 's/^[^ ]* //' | while IFS= read -r line; do
    emit GPU_PCI "$line"
  done
fi
"""

JOBS_PROBE = r"""
base="$HOME/.tailfleet"; jobs="$base/jobs"
shopt -s nullglob
now=$(date +%s)
for ef in "$jobs/"*.exit; do
  end=$(awk '{print $2}' "$ef" 2>/dev/null)
  if [ -n "$end" ] && [ $((now-end)) -gt 86400 ]; then
    id=$(basename "$ef" .exit); rm -f "$jobs/$id."*
  fi
done
for f in "$jobs/"*.json; do
  id=$(basename "$f" .json)
  pid=$(cat "$jobs/$id.pid" 2>/dev/null)
  alive=""
  if [ -n "$pid" ]; then
    if command -v pgrep >/dev/null 2>&1; then
      pgrep -g "$pid" >/dev/null 2>&1 && alive=1
    elif kill -0 "$pid" 2>/dev/null; then alive=1; fi
  fi
  if [ -n "$alive" ]; then live=running; ce="";
  elif [ -f "$jobs/$id.exit" ]; then live=exited; ce=$(cat "$jobs/$id.exit");
  else live=stale; ce=""; fi
  printf 'JOB\t%s\t%s\t%s\n' "$live" "$ce" "$(tr -d '\n' < "$f")"
done
"""

LAUNCH = r"""
set -e
id="$1"; proj="$2"; node="$3"; oh="$4"; ou="$5"; started="$6"; cmd_b64="$7"; dir_b64="$8"
base="$HOME/.tailfleet"; jobs="$base/jobs"; wd="$base/$proj"
mkdir -p "$jobs" "$wd"
ef="$jobs/$id.exit"; pf="$jobs/$id.pid"; log="$jobs/$id.log"; runner="$jobs/$id.run.sh"
rm -f "$ef" "$pf"
{
  echo '#!/bin/bash'
  echo "echo \$\$ > \"$pf\""
  echo "trap 'rc=\$?; echo \"\$rc \$(date +%s)\" > \"$ef\"' EXIT"
  echo "trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM"
  echo "cd \"$wd\""
  echo "[ -f \"$base/env\" ] && . \"$base/env\""
  printf '%s\n' "$cmd_b64" | base64 -d
} > "$runner"
cat > "$jobs/$id.json" <<JSON
{"id":"$id","node":"$node","project":"$proj","cmd_b64":"$cmd_b64","dir_b64":"$dir_b64","origin_host":"$oh","origin_user":"$ou","started":$started,"cuda":"${CUDA_VISIBLE_DEVICES:-}"}
JSON
setsid bash -l "$runner" >"$log" 2>&1 </dev/null &
for i in $(seq 1 30); do [ -s "$pf" ] && break; sleep 0.1; done
cat "$pf" 2>/dev/null || echo 0
"""


# ---------- discovery / exec ----------

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


def find_node(name):
    for n in all_nodes():
        if n["host"] == name:
            return n
    sys.exit(f"no online linux node named {name!r}")


def _exec(node, script, timeout, args=None):
    cmd = ["bash", "-s"] if node["self"] else ["ssh", *SSH_OPTS, node["ip"], "bash", "-s"]
    if args:
        cmd += ["--", *args]
    return subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)


def ssh_e():
    return "ssh " + " ".join(SSH_OPTS)


def rsync(node, src, dst, delete=False, excludes=(), update=False):
    args = ["rsync", "-az"]
    if delete:
        args.append("--delete")
    if update:
        args.append("--update")
    for e in excludes:
        args += ["--exclude", e]
    if not node["self"]:
        args += ["-e", ssh_e()]
    return subprocess.run(args + [src, dst])


def project_for(src):
    h = hashlib.sha1(src.encode()).hexdigest()[:6]
    return f"{os.path.basename(src.rstrip('/')) or 'root'}-{h}"


def excludes_for(src):
    excludes = list(DEFAULT_EXCLUDES)
    ignore = os.path.join(src, ".tailfleetignore")
    if os.path.isfile(ignore):
        with open(ignore) as fh:
            excludes += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    return excludes


def remote_path(node, rel):
    if node["self"]:
        return os.path.expanduser("~/" + rel)
    return f"{node['ip']}:{rel}"


# ---------- probing ----------

def gather(nodes, timeout, script):
    with cf.ThreadPoolExecutor(max_workers=max(4, len(nodes))) as ex:
        return list(ex.map(lambda n: probe_one(n, script, timeout), nodes))


def probe_one(node, script, timeout):
    try:
        r = _exec(node, script, timeout)
    except subprocess.TimeoutExpired:
        return {**node, "status": "timeout", "jobs": []}
    except subprocess.SubprocessError as e:
        return {**node, "status": "unreachable", "error": str(e), "jobs": []}
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return {**node, "status": "unreachable", "error": err[-1] if err else f"exit {r.returncode}", "jobs": []}
    info = parse(r.stdout)
    jobs = []
    for live, ce, blob in info.pop("JOBS_RAW", []):
        j = decode_job(live, ce, blob)
        if j:
            jobs.append(j)
    return {**node, "status": "ok", "jobs": jobs, **info}


def parse(text):
    info, gpus, jobs = {}, [], []
    for line in text.splitlines():
        if line.startswith("JOB\t"):
            parts = line.split("\t", 3)
            if len(parts) == 4:
                jobs.append((parts[1], parts[2], parts[3]))
            continue
        if "\t" not in line:
            continue
        k, _, v = line.partition("\t")
        if k == "GPU":
            f = [x.strip() for x in v.split(",")]
            if len(f) >= 6:
                gpus.append({"name": f[0], "mem_total": f[1], "mem_used": f[2],
                             "util": f[3], "temp": f[4], "driver": f[5]})
            else:
                gpus.append({"name": v})
        elif k == "GPU_PCI":
            gpus.append({"name": v, "pci": True})
        else:
            info[k] = v
    info["GPUS"] = gpus
    info["JOBS_RAW"] = jobs
    return info


def decode_job(live, ce, blob):
    try:
        m = json.loads(blob)
    except json.JSONDecodeError:
        return None
    m["live"] = live
    m["cmd"] = base64.b64decode(m.get("cmd_b64", "")).decode(errors="replace")
    m["dir"] = base64.b64decode(m.get("dir_b64", "")).decode(errors="replace")
    if ce:
        bits = ce.split()
        m["exit"] = int(bits[0]) if bits else None
        m["ended"] = int(bits[1]) if len(bits) > 1 else None
    return m


# ---------- formatting helpers ----------

def bar(pct):
    try:
        p = int(float(pct))
    except (TypeError, ValueError):
        return "?"
    color = "red" if p >= 80 else "yellow" if p >= 40 else "green"
    return f"[{color}]{p}%[/{color}]"


def fmt_mem(kb):
    try:
        return f"{int(kb) / 1024 / 1024:.1f} GiB"
    except (TypeError, ValueError):
        return "?"


def mem_col(r):
    total = fmt_mem(r.get("MEM_KB"))
    try:
        return f"{int(r['MEM_USED_KB'])/1024/1024:.1f}/{total}"
    except (KeyError, TypeError, ValueError):
        return total


def ghz(r):
    try:
        return f"{float(r['MHZ_MAX'])/1000:.1f} GHz"
    except (KeyError, TypeError, ValueError):
        return "?"


def cell(r, fn):
    return fn() if r["status"] == "ok" else ""


def gcell(r, fn):
    if r["status"] != "ok":
        return ""
    gpus = r.get("GPUS") or []
    return "\n".join(fn(g) for g in gpus) if gpus else "[dim]none[/dim]"


def gname(g):
    n = g["name"]
    if g.get("pci"):
        n = re.sub(r"^[^:]*:\s*", "", n)
        n = re.sub(r"\s*\(rev [^)]*\)", "", n)
        n = n.replace(" Corporation", "").replace(" Inc.", "")
        return n + " [no nvidia-smi]"
    return n


def vram(g):
    try:
        return f"{int(g['mem_used'])/1024:.1f}/{int(g['mem_total'])/1024:.1f}G"
    except (KeyError, ValueError):
        return "-"


def dur(secs):
    s = int(secs)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


BRAILLE_DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def _clamp(v):
    try:
        return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


PALETTE = ["cyan", "magenta", "green", "yellow", "blue", "bright_red", "bright_magenta", "bright_green"]


def braille_graph(vals, width, height, color):
    cols, rows = width * 2, height * 4
    data = [_clamp(v) for v in list(vals)[-cols:]]
    data = [0.0] * (cols - len(data)) + data
    fills = [max(1, round(v / 100 * rows)) for v in data]
    lines = []
    for cr in range(height):
        chars = []
        for cc in range(width):
            ch = 0x2800
            for dr in range(4):
                gr = cr * 4 + dr
                for sc in range(2):
                    if rows - gr <= fills[cc * 2 + sc]:
                        ch |= BRAILLE_DOTS[dr][sc]
            chars.append(chr(ch))
        lines.append(f"[{color}]" + "".join(chars) + "[/]")
    return "\n".join(lines)


def hw_table(results, hist=None, graph_width=24, graph_height=3):
    ncolor = {r["host"]: PALETTE[i % len(PALETTE)] for i, r in enumerate(results)}

    def ugraph(r, key):
        if not hist:
            return ""
        h = hist.get(r["host"], {}).get(key)
        return "\n" + braille_graph(h, graph_width, graph_height, ncolor[r["host"]]) if h else ""

    t = Table(show_lines=True, title="tailnet hardware", title_style="bold", box=box.ROUNDED)
    t.add_column("", style="bold")
    for r in results:
        name = r["host"] + (" *" if r["self"] else "")
        if hist:
            t.add_column(f"[{ncolor[r['host']]}]{name}[/]", overflow="fold", width=graph_width)
        else:
            t.add_column(name, overflow="fold")

    def row(label, fn):
        t.add_row(label, *[fn(r) for r in results])

    def cpu_cell(r):
        if r["status"] != "ok":
            return f"[red]{r['status']}[/red]"
        return f"{r.get('CPU_MODEL', '?')}\n[dim]{r.get('CORES','?')}c/{r.get('THREADS','?')}t @ {ghz(r)}[/dim]"

    row("os", lambda r: cell(r, lambda: f"{r.get('OS_NAME') or '?'}\n[dim]{r.get('KERNEL', '?')}[/dim]"))
    row("cpu", cpu_cell)
    row("cpu util", lambda r: cell(r, lambda: bar(r.get("CPU_UTIL")) + ugraph(r, "cpu")))
    row("ram", lambda r: cell(r, lambda: mem_col(r) + ugraph(r, "mem")))
    row("gpu", lambda r: gcell(r, gname))
    row("gpu util", lambda r: cell(r, lambda: gcell(r, lambda g: bar(g["util"]) if "util" in g else "-") + ugraph(r, "gpu")))
    row("vram", lambda r: cell(r, lambda: gcell(r, vram) + ugraph(r, "vram")))
    row("temp", lambda r: gcell(r, lambda g: f"{g['temp']}C" if "temp" in g else "-"))
    return t


def jobs_table(jobs):
    t = Table(show_lines=True, title="jobs", title_style="bold", box=box.ROUNDED, expand=True)
    for c in ("job", "node", "state", "elapsed", "cuda", "cmd", "from"):
        t.add_column(c, overflow="fold", ratio=1 if c == "cmd" else None)
    if not jobs:
        t.add_row("[dim]none[/dim]", "", "", "", "", "", "")
        return t
    now = time.time()
    for j in sorted(jobs, key=lambda j: -j.get("started", 0)):
        live = j["live"]
        if live == "running":
            state = "[green]running[/green]"
            el = dur(now - j.get("started", now))
        elif live == "exited":
            code = j.get("exit")
            state = "[dim]done[/dim]" if code == 0 else f"[red]fail({code})[/red]"
            el = dur(j.get("ended", now) - j.get("started", now))
        else:
            state = "[red]stale[/red]"
            el = dur(now - j.get("started", now))
        t.add_row(j["id"], j["node"], state, el, j.get("cuda") or "all",
                  j["cmd"], f"{j.get('origin_user','?')}@{j.get('origin_host','?')}")
    return t


# ---------- subcommands ----------

def cmd_inventory(args):
    results = sorted(gather(all_nodes(), args.timeout, PROBE),
                     key=lambda r: (not r["self"], r["host"] or ""))
    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return
    con = Console()
    con.print(hw_table(results))
    con.print("[dim]* = local node[/dim]")


def cmd_jobs(args):
    results = gather(all_nodes(), args.timeout, JOBS_PROBE)
    jobs = [j for r in results for j in r.get("jobs", [])]
    if args.json:
        print(json.dumps(jobs, indent=2, default=str))
        return
    Console().print(jobs_table(jobs))


def find_job(jobid, timeout):
    if not ID_RE.match(jobid):
        sys.exit(f"invalid job id {jobid!r}")
    results = gather(all_nodes(), timeout, JOBS_PROBE)
    for r in results:
        for j in r.get("jobs", []):
            if j["id"] == jobid:
                return j
    sys.exit(f"no job {jobid!r} found on the fleet")


ENV_PREFIX = '[ -f "$HOME/.tailfleet/env" ] && . "$HOME/.tailfleet/env"; '


def cmd_exec(args):
    node = find_node(args.node)
    tokens = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not tokens:
        sys.exit("no command given (use: exec <node> -- <cmd>)")
    cmdline = ENV_PREFIX + " ".join(tokens)
    if node["self"]:
        rc = subprocess.run(["bash", "-lc", cmdline])
    else:
        rc = subprocess.run(["ssh", *SSH_OPTS, node["ip"], f"bash -lc {shlex.quote(cmdline)}"])
    sys.exit(rc.returncode)


def cmd_run(args):
    node = find_node(args.node)
    src = os.path.abspath(args.dir)
    if not os.path.isdir(src):
        sys.exit(f"{src} is not a directory")
    tokens = list(args.command)
    detach, mirror, force = args.detach, args.mirror, args.force
    flagset = {"--detach", "--mirror", "--force"}
    if "--" in tokens:
        i = tokens.index("--")
        pre = tokens[:i]
        detach = detach or ("--detach" in pre)
        mirror = mirror or ("--mirror" in pre)
        force = force or ("--force" in pre)
        tokens = tokens[i + 1:]
    else:
        while tokens and tokens[0] in flagset:
            detach = detach or tokens[0] == "--detach"
            mirror = mirror or tokens[0] == "--mirror"
            force = force or tokens[0] == "--force"
            tokens = tokens[1:]
    if not tokens:
        sys.exit("no command given (use: run <node> <dir> [--detach] [--mirror] -- <cmd>)")

    proj = project_for(src)
    jobid = f"{node['host']}-{os.urandom(3).hex()}"
    cmd_b64 = base64.b64encode(" ".join(tokens).encode()).decode()
    dir_b64 = base64.b64encode(src.encode()).decode()

    if not force:
        chk = probe_one(node, JOBS_PROBE, args.timeout)
        for ej in chk.get("jobs", []):
            if ej.get("live") == "running" and ej.get("project") == proj and ej.get("cmd_b64") == cmd_b64:
                sys.exit(
                    f"refusing: identical job already running as {ej['id']} on {node['host']} "
                    f"(same dir+cmd, would clobber its output). "
                    f"stop it (tailfleet kill {ej['id']}) or pass --force to launch anyway."
                )

    excludes = excludes_for(src)
    print(f"[push] {src} -> {node['host']}:~/.tailfleet/{proj}" + ("  (--delete)" if mirror else ""))
    _exec(node, f'mkdir -p "$HOME/.tailfleet/{proj}"', 30)
    if rsync(node, src + "/", remote_path(node, f".tailfleet/{proj}/"), delete=mirror, excludes=excludes).returncode != 0:
        sys.exit("rsync push failed")

    out = _exec(node, LAUNCH, 60, args=[
        jobid, proj, node["host"], socket.gethostname(), getpass.getuser(),
        str(int(time.time())), cmd_b64, dir_b64,
    ])
    if out.returncode != 0:
        sys.exit("launch failed:\n" + out.stderr)
    pid = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "0"
    print(f"[job] {jobid} (pid {pid}) on {node['host']}")

    if detach:
        print(f"detached. follow: tailfleet logs {jobid} -f   fetch: tailfleet fetch {jobid}")
        return

    print(f"[tail] {jobid} (Ctrl-C stops watching, job keeps running)\n" + "-" * 60)
    log = f".tailfleet/jobs/{jobid}.log"
    try:
        tail = (["tail", "-n", "+1", "-f", f"--pid={pid}", os.path.expanduser("~/" + log)]
                if node["self"] else
                ["ssh", *SSH_OPTS, node["ip"], f"tail -n +1 -f --pid={pid} {shlex.quote(log)}"])
        subprocess.run(tail)
    except KeyboardInterrupt:
        print(f"\n[detached] job still running. fetch later: tailfleet fetch {jobid}")
        return
    print("-" * 60)
    ce = _exec(node, f'cat "$HOME/.tailfleet/jobs/{jobid}.exit" 2>/dev/null', 30).stdout.split()
    print(f"[exit] {ce[0] if ce else '?'}")
    print(f"[pull] {node['host']}:~/.tailfleet/{proj} -> {src}  (--update: keeps newer local)")
    rsync(node, remote_path(node, f".tailfleet/{proj}/"), src + "/", update=True)


def cmd_logs(args):
    j = find_job(args.id, args.timeout)
    node = find_node(j["node"])
    log = f".tailfleet/jobs/{j['id']}.log"
    flag = "-f " if args.f else ""
    rcmd = f"tail -n +1 {flag}{shlex.quote(log)}" if args.f else f"cat {shlex.quote(log)}"
    if node["self"]:
        path = os.path.expanduser("~/" + log)
        subprocess.run((["tail", "-n", "+1", "-f", path] if args.f else ["cat", path]))
    else:
        try:
            subprocess.run(["ssh", *SSH_OPTS, node["ip"], rcmd])
        except KeyboardInterrupt:
            pass


def cmd_fetch(args):
    j = find_job(args.id, args.timeout)
    node = find_node(j["node"])
    dest = j["dir"]
    os.makedirs(dest, exist_ok=True)
    note = "  (--overwrite: replaces local)" if args.overwrite else "  (--update: keeps newer local)"
    print(f"[pull] {node['host']}:~/.tailfleet/{j['project']} -> {dest}{note}")
    rc = rsync(node, remote_path(node, f".tailfleet/{j['project']}/"), dest + "/", update=not args.overwrite)
    sys.exit(rc.returncode)


def cmd_kill(args):
    j = find_job(args.id, args.timeout)
    node = find_node(j["node"])
    if args.force:
        kl = 'kill -KILL -- -"$p" 2>/dev/null'
        sig = "KILL"
    else:
        kl = 'kill -TERM -- -"$p" 2>/dev/null; sleep 2; kill -KILL -- -"$p" 2>/dev/null'
        sig = "TERM then KILL"
    sh = (f'p=$(cat "$HOME/.tailfleet/jobs/{j["id"]}.pid" 2>/dev/null) || exit 0; '
          f'[ -n "$p" ] || exit 0; {kl}; exit 0')
    _exec(node, sh, 30)
    print(f"signalled {j['id']} process group ({sig}) on {node['host']}")


def cmd_rm(args):
    j = find_job(args.id, args.timeout)
    node = find_node(j["node"])
    if j.get("live") == "running" and not args.force:
        sys.exit(f"{j['id']} is still running on {node['host']}; "
                 f"stop it first (tailfleet kill {j['id']}) or pass --force to drop the marker only.")
    _exec(node, f'rm -f "$HOME/.tailfleet/jobs/{j["id"]}."*', 30)
    print(f"removed {j['id']} from {node['host']}")


def cmd_sync(args):
    node = find_node(args.node)
    src = os.path.abspath(args.dir)
    if not os.path.isdir(src):
        sys.exit(f"{src} is not a directory")
    proj = project_for(src)
    excludes = excludes_for(src)
    if args.pull:
        note = "  (--overwrite: replaces local)" if args.overwrite else "  (--update: keeps newer local)"
        print(f"[pull] {node['host']}:~/.tailfleet/{proj} -> {src}{note}")
        rc = rsync(node, remote_path(node, f".tailfleet/{proj}/"), src + "/", update=not args.overwrite)
    else:
        print(f"[push] {src} -> {node['host']}:~/.tailfleet/{proj}" + ("  (--delete)" if args.mirror else ""))
        _exec(node, f'mkdir -p "$HOME/.tailfleet/{proj}"', 30)
        rc = rsync(node, src + "/", remote_path(node, f".tailfleet/{proj}/"), delete=args.mirror, excludes=excludes)
    sys.exit(rc.returncode)


class MonitorApp(App):
    CSS = """
    #footer { padding: 0 1; height: 1; color: $text-muted; }
    #hw, #jobs { padding: 0 1; }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, interval, rediscover, timeout):
        super().__init__()
        self.interval = interval
        self.rediscover = rediscover
        self.timeout = timeout
        self.script = PROBE + "\n" + JOBS_PROBE
        self.nodes = []
        self.last_disc = 0.0
        self.hist = {}

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="hw")
            yield Static(id="jobs")
        yield Static("q / Ctrl-C to quit  ·  * = local", id="footer")

    def on_mount(self):
        self.theme = "ansi-dark"
        self.refresh_data()
        self.set_interval(self.interval, self.refresh_data)

    @work(thread=True, exclusive=True)
    def refresh_data(self):
        if not self.nodes or time.time() - self.last_disc > self.rediscover:
            self.nodes = all_nodes()
            self.last_disc = time.time()
        results = sorted(gather(self.nodes, self.timeout, self.script),
                         key=lambda r: (not r["self"], r["host"] or ""))
        jobs = [j for r in results for j in r.get("jobs", []) if j["live"] == "running"]
        self.call_from_thread(self.update_ui, results, jobs)

    def update_ui(self, results, jobs):
        for r in results:
            if r["status"] != "ok":
                continue
            h = self.hist.setdefault(r["host"], {k: deque(maxlen=240) for k in ("cpu", "mem", "gpu", "vram")})
            h["cpu"].append(r.get("CPU_UTIL", 0))
            try:
                h["mem"].append(int(r["MEM_USED_KB"]) / int(r["MEM_KB"]) * 100)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
            gutil = next((g["util"] for g in (r.get("GPUS") or []) if "util" in g), None)
            if gutil is not None:
                h["gpu"].append(gutil)
            g0 = next((g for g in (r.get("GPUS") or []) if "mem_total" in g), None)
            if g0:
                try:
                    h["vram"].append(int(g0["mem_used"]) / int(g0["mem_total"]) * 100)
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    pass
        n = max(1, len(results))
        gw = max(8, (self.size.width - 12) // n - 3)
        self.query_one("#hw", Static).update(hw_table(results, self.hist, graph_width=gw))
        jw = self.query_one("#jobs", Static)
        jw.display = bool(jobs)
        if jobs:
            jw.update(jobs_table(jobs))


def cmd_monitor(args):
    MonitorApp(args.interval, args.rediscover, args.timeout).run()


def main():
    ap = argparse.ArgumentParser(prog="tailfleet", description="tailnet hardware + job control")
    ap.add_argument("--json", action="store_true", help="raw JSON (inventory)")
    ap.add_argument("--timeout", type=float, default=20, help="per-node timeout seconds")
    sub = ap.add_subparsers(dest="cmd")

    pm = sub.add_parser("monitor", help="live-refreshing table + jobs")
    pm.add_argument("--interval", type=float, default=2, help="seconds between refreshes (default 2)")
    pm.add_argument("--rediscover", type=float, default=15, help="seconds between tailnet re-discovery (default 15)")
    pm.add_argument("--timeout", type=float, default=20, help="per-node probe timeout seconds (default 20)")

    pr = sub.add_parser(
        "run", help="run a tracked job on a node with directory sync",
        description="Mirror a local directory to a node, run a command in it as a tracked job, "
                    "stream output, and pull results back. The push is additive by default "
                    "(no --delete); pass --mirror for an exact mirror. Refuses an identical "
                    "running job (same dir+cmd) unless --force. For a quick command with no "
                    "file sync or job tracking, use `exec`. Both run and exec source "
                    "~/.tailfleet/env on the node if present (put nvcc/uv/conda PATH there).",
    )
    pr.add_argument("node", help="target node hostname (as shown in `tailfleet`)")
    pr.add_argument("dir", help="local directory to mirror up and pull results back into")
    pr.add_argument("command", nargs=argparse.REMAINDER,
                    help="after `--`, the shell command to run remotely (supports ; | $(...))")
    pr.add_argument("--detach", action="store_true",
                    help="return immediately with a jobid; don't tail or auto-pull")
    pr.add_argument("--mirror", action="store_true",
                    help="push with rsync --delete (delete remote-only files); off by default to "
                         "avoid wiping unfetched remote results")
    pr.add_argument("--force", action="store_true",
                    help="launch even if an identical job (same dir+cmd) is already running")

    pe = sub.add_parser(
        "exec", help="run a one-off command on a node (no sync, no tracking)",
        description="Run a command on a node over SSH, streaming output to your terminal and "
                    "returning its exit code. No directory sync, no job marker, dies with your "
                    "session. Sources ~/.tailfleet/env on the node if present.",
    )
    pe.add_argument("node", help="target node hostname (as shown in `tailfleet`)")
    pe.add_argument("command", nargs=argparse.REMAINDER,
                    help="after `--`, the shell command to run remotely (supports ; | $(...))")

    ps = sub.add_parser(
        "sync", help="rsync a directory to/from a node's workdir (no job launch)",
        description="Mirror a local directory to its ~/.tailfleet/<proj> workdir on a node, or "
                    "pull it back, without launching a job. Push is additive by default "
                    "(--mirror for --delete); pull keeps newer local files (--overwrite to force).",
    )
    ps.add_argument("node", help="target node hostname")
    ps.add_argument("dir", help="local directory (defines the remote workdir)")
    ps.add_argument("--pull", action="store_true", help="pull remote workdir -> local instead of pushing")
    ps.add_argument("--mirror", action="store_true", help="push with rsync --delete (exact mirror)")
    ps.add_argument("--overwrite", action="store_true",
                    help="on --pull, overwrite local even if newer (disables rsync --update)")

    pj = sub.add_parser("jobs", help="list jobs across the fleet")
    pj.add_argument("--json", action="store_true", help="emit raw JSON instead of a table")

    pl = sub.add_parser("logs", help="show a job's log")
    pl.add_argument("id", help="job id (from `tailfleet jobs`)")
    pl.add_argument("-f", action="store_true", help="follow the log (Ctrl-C to stop)")

    pf = sub.add_parser("fetch", help="pull a job's results back")
    pf.add_argument("id", help="job id; results rsync back into the job's origin dir")
    pf.add_argument("--overwrite", action="store_true",
                    help="overwrite local even if newer (disables rsync --update)")

    pk = sub.add_parser("kill", help="stop a running job (signals its process group)")
    pk.add_argument("id", help="job id to signal")
    pk.add_argument("--force", "-9", action="store_true", help="send SIGKILL immediately (default TERM then KILL)")

    prm = sub.add_parser("rm", help="remove a job's marker/log")
    prm.add_argument("id", help="job id to remove from its node's registry")
    prm.add_argument("--force", action="store_true", help="drop the marker even if the job is still running")

    args = ap.parse_args()
    if not hasattr(args, "timeout"):
        args.timeout = 20
    {
        None: cmd_inventory,
        "monitor": cmd_monitor,
        "run": cmd_run,
        "exec": cmd_exec,
        "sync": cmd_sync,
        "jobs": cmd_jobs,
        "logs": cmd_logs,
        "fetch": cmd_fetch,
        "kill": cmd_kill,
        "rm": cmd_rm,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
