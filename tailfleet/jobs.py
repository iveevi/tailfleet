"""Workspace sync and remote routine dispatch."""

import base64
import concurrent.futures as cf
import glob
import os
import subprocess
import sys
import tempfile

from .config import ConfigError
from .nodes import SSH_OPTS, _exec, all_nodes

REMOTE_BASE = ".tailfleet/work"
EXEC_TIMEOUT = 30


def _remote_dir(cfg):
    return f"{REMOTE_BASE}/{cfg['workspace']}"


def _ssh_e():
    return "ssh " + " ".join(SSH_OPTS)


def _fill(template, **kw):
    for k, v in kw.items():
        template = template.replace(f"@{k}@", str(v))
    return template


def resolve(names):
    avail = {n["host"]: n for n in all_nodes()}
    if names == ["*"]:
        return list(avail.values())
    out = []
    for h in names:
        if h not in avail:
            raise ConfigError(f"node not found or offline: {h}")
        out.append(avail[h])
    return out


def routine_nodes(cfg, name):
    r = cfg["routines"].get(name)
    if r is None:
        have = ", ".join(cfg["routines"]) or "none"
        raise ConfigError(f"unknown routine: {name} (have: {have})")
    return resolve(r["nodes"])


def all_config_nodes(cfg):
    seen, nodes = set(), []
    for r in cfg["routines"].values():
        for n in resolve(r["nodes"]):
            if n["host"] not in seen:
                seen.add(n["host"])
                nodes.append(n)
    return nodes


def expand_push(cfg):
    root = cfg["root"]
    return sorted({
        m for g in cfg["push"]
        for m in glob.glob(g, root_dir=root, recursive=True)
        if (root / m).is_file()
    })


def push(cfg, nodes):
    files = expand_push(cfg)
    if not files:
        print("push: no files matched", file=sys.stderr)
        return
    with tempfile.NamedTemporaryFile("w", suffix=".tfsync", delete=False) as f:
        f.write("\n".join(files) + "\n")
        listfile = f.name
    try:
        for node in nodes:
            _exec(node, f'mkdir -p "$HOME/{_remote_dir(cfg)}/.tf"', EXEC_TIMEOUT)
            src = str(cfg["root"]) + "/"
            if node["self"]:
                cmd = ["rsync", "-a", f"--files-from={listfile}", src,
                       os.path.expanduser(f"~/{_remote_dir(cfg)}") + "/"]
            else:
                cmd = ["rsync", "-az", "-e", _ssh_e(), f"--files-from={listfile}", src,
                       f"{node['ip']}:{_remote_dir(cfg)}/"]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise ConfigError(f"push {node['host']}: {(r.stderr or '').strip() or 'rsync failed'}")
            print(f"push {node['host']}: {len(files)} files")
    finally:
        os.unlink(listfile)


PULL_LIST = r"""
ws="$HOME/@DIR@"
cd "$ws" 2>/dev/null || exit 0
shopt -s globstar nullglob
for f in @GLOBS@; do
  [ -f "$f" ] && printf '%s\n' "$f"
done
true
"""


def pull(cfg, nodes):
    if not cfg["pull"]:
        print("pull: no pull globs configured", file=sys.stderr)
        return
    script = _fill(PULL_LIST, DIR=_remote_dir(cfg), GLOBS=" ".join(cfg["pull"]))
    for node in nodes:
        r = _exec(node, script, EXEC_TIMEOUT)
        files = [l for l in (r.stdout or "").splitlines() if l.strip()]
        if not files:
            print(f"pull {node['host']}: nothing to pull")
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".tfsync", delete=False) as f:
            f.write("\n".join(files) + "\n")
            listfile = f.name
        try:
            dst = str(cfg["root"]) + "/"
            if node["self"]:
                cmd = ["rsync", "-a", f"--files-from={listfile}",
                       os.path.expanduser(f"~/{_remote_dir(cfg)}") + "/", dst]
            else:
                cmd = ["rsync", "-az", "-e", _ssh_e(), f"--files-from={listfile}",
                       f"{node['ip']}:{_remote_dir(cfg)}/", dst]
            rr = subprocess.run(cmd, capture_output=True, text=True)
            if rr.returncode != 0:
                raise ConfigError(f"pull {node['host']}: {(rr.stderr or '').strip() or 'rsync failed'}")
            print(f"pull {node['host']}: {len(files)} files")
        finally:
            os.unlink(listfile)


DISPATCH = r"""
set -e
ws="$HOME/@DIR@"; tf="$ws/.tf"
mkdir -p "$tf"
if [ -f "$tf/@R@.pid" ] && pgrep -g "$(cat "$tf/@R@.pid")" >/dev/null 2>&1; then
  echo "already running (pgid $(cat "$tf/@R@.pid"))" >&2
  exit 3
fi
printf '%s' @B64@ | base64 -d > "$tf/@R@.sh"
rm -f "$tf/@R@.exit"
date +%s > "$tf/@R@.start"
setsid env TF_NODE=@HOST@ TF_ROUTINE=@R@ TF_NODE_INDEX=@I@ TF_NODE_COUNT=@N@ \
  bash -c 'cd "$1" && bash ".tf/$2.sh"; echo "$? $(date +%s)" > "$1/.tf/$2.exit"' tf "$ws" @R@ \
  >"$tf/@R@.log" 2>&1 </dev/null &
echo $! >"$tf/@R@.pid"
"""


def run_routine(cfg, name):
    nodes = routine_nodes(cfg, name)
    push(cfg, nodes)
    body = "set -e\n" + cfg["routines"][name]["run"]
    b64 = base64.b64encode(body.encode()).decode()
    for i, node in enumerate(nodes):
        script = _fill(DISPATCH, DIR=_remote_dir(cfg), R=name, B64=b64,
                       HOST=node["host"], I=i, N=len(nodes))
        r = _exec(node, script, EXEC_TIMEOUT)
        if r.returncode != 0:
            err = (r.stderr or "").strip() or f"exit {r.returncode}"
            print(f"run {name}@{node['host']}: {err}", file=sys.stderr)
        else:
            print(f"run {name}@{node['host']}: started")


PS = r"""
tf="$HOME/@DIR@/.tf"
cd "$tf" 2>/dev/null || exit 0
shopt -s nullglob
now=$(date +%s)
for p in *.pid; do
  r="${p%.pid}"
  pg=$(cat "$p")
  start=$(cat "$r.start" 2>/dev/null || echo "")
  if pgrep -g "$pg" >/dev/null 2>&1; then
    printf '%s\trunning\t-\t%s\n' "$r" "${start:+$((now-start))}"
  elif [ -f "$r.exit" ]; then
    read -r code end < "$r.exit"
    printf '%s\texited\t%s\t%s\n' "$r" "$code" "${start:+$((end-start))}"
  else
    printf '%s\tstale\t-\t-\n' "$r"
  fi
done
true
"""


def _dur(s):
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "-"
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02}s"
    return f"{s}s"


def ps(cfg):
    from rich.console import Console
    from rich.table import Table

    nodes = all_config_nodes(cfg)
    script = _fill(PS, DIR=_remote_dir(cfg))
    with cf.ThreadPoolExecutor(max_workers=max(4, len(nodes))) as ex:
        outs = list(ex.map(lambda n: (n, _exec(n, script, EXEC_TIMEOUT)), nodes))

    table = Table(box=None, header_style="bold")
    for col in ("node", "routine", "state", "exit", "dur"):
        table.add_column(col)
    for node, r in outs:
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            rt, state, code, dur = parts
            style = {"running": "green", "exited": "red" if code not in ("0", "-") else "dim",
                     "stale": "yellow"}.get(state, "")
            table.add_row(node["host"], rt, f"[{style}]{state}[/]" if style else state,
                          code, _dur(dur))
    if table.row_count:
        Console().print(table)
    else:
        print("no jobs")


def _logs_target(cfg, spec):
    if "@" in spec:
        name, host = spec.split("@", 1)
        node = resolve([host])[0]
    else:
        name = spec
        nodes = routine_nodes(cfg, name)
        if len(nodes) != 1:
            hosts = ", ".join(n["host"] for n in nodes)
            raise ConfigError(f"routine {name} runs on multiple nodes ({hosts}); use {name}@<node>")
        node = nodes[0]
    if name not in cfg["routines"]:
        raise ConfigError(f"unknown routine: {name}")
    return name, node


def logs(cfg, spec, follow=False, lines=120):
    name, node = _logs_target(cfg, spec)
    path = f"{_remote_dir(cfg)}/.tf/{name}.log"
    tail = ["tail", "-n", str(lines)] + (["-F"] if follow else [])
    try:
        if node["self"]:
            return subprocess.call([*tail, os.path.expanduser(f"~/{path}")])
        return subprocess.call(["ssh", *SSH_OPTS, node["ip"], " ".join(["exec", *tail, f'"$HOME/{path}"'])])
    except KeyboardInterrupt:
        return 130


KILL = r"""
tf="$HOME/@DIR@/.tf"
pg=$(cat "$tf/@R@.pid" 2>/dev/null) || { echo "no pid"; exit 0; }
if kill -TERM -- -"$pg" 2>/dev/null; then echo killed; else echo "not running"; fi
"""


def kill(cfg, name):
    for node in routine_nodes(cfg, name):
        r = _exec(node, _fill(KILL, DIR=_remote_dir(cfg), R=name), EXEC_TIMEOUT)
        print(f"kill {name}@{node['host']}: {(r.stdout or '').strip() or (r.stderr or '').strip()}")
