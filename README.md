# tailfleet

Live hardware monitor and remote job runner for the Linux machines on your Tailscale network. No agents, no daemons: everything runs over `tailscale status` + SSH, with state kept in plain files under `~/.tailfleet/` on each node.

## Requirements

- `tailscale` CLI on the host; SSH access to peers (Tailscale SSH or plain keys)
- `bash` and `rsync` on host and nodes
- `nvidia-smi`, `intel_gpu_top`, or `gputop` on nodes for GPU stats (optional)

## Install

```sh
uv tool install .        # or: uv run tailfleet
```

## Status

```sh
tailfleet                # equivalent to: tailfleet status
```

A one-shot `nvidia-smi`-style table of every online Linux node on the tailnet, rendered in your terminal's ANSI palette. One node per two-line entry, four column groups:

- **Node** — hostname; the local node's name is *italicized*
- **CPU** — model, then temp · util (bar gauge) · cores/threads · clock
- **Memory** — used / total and a util bar
- **GPU** — name, then temp · util (bar) · VRAM used/total (bar)

Utilization is band-colored (green < 40% < yellow < 80% < red). VRAM is reported for NVIDIA (`nvidia-smi`) and for shared-memory Intel iGPUs (`gputop`, counted against system RAM). CPU temp comes from `thermal_zone`/`hwmon`.

Flag: `--timeout` (per-node probe seconds, default 20).

## Monitor

```sh
tailfleet monitor
```

The same table, live-refreshing in place at the top of the screen:

- `-` faster, `+` slower (refresh rate shown as `⟳ Ns` in the header), `q` quits
- header also shows the current time and nodes-up count

Flags: `--interval` (refresh seconds, default 1), `--rediscover` (re-scan tailnet, default 15), `--timeout` (per-node probe, default 20).

## Jobs

Describe a workspace in a `tailfleet.yaml` at your project root:

```yaml
workspace: nanogpt            # remote dir name; defaults to the local dir basename
push: [src/**/*.py, pyproject.toml, uv.lock]     # host → nodes
pull: [out/**, logs/*.log]                       # nodes → host

routines:
  train:
    nodes: [gpubox, minipc]   # or ["*"] for every online node
    run: |
      uv sync --frozen
      uv run python train.py --shard $TF_NODE_INDEX/$TF_NODE_COUNT

  eval:
    nodes: [homelab]
    run: |
      uv run python eval.py > out/eval.txt
```

Then, from anywhere inside the project:

```sh
tailfleet run train            # push files, dispatch on gpubox + minipc
tailfleet ps                   # routine × node: running / exit code / duration
tailfleet logs train@gpubox -f # tail a routine's log (@node optional if single-node)
tailfleet kill train          # TERM the routine's process group
tailfleet pull                # fetch pull-globs back into the project
tailfleet sync                # push only, no dispatch
```

### Semantics

- `run` is executed as one `bash -e` script in the remote workspace, detached with `setsid`; it survives disconnects.
- Injected environment: `TF_NODE`, `TF_ROUTINE`, `TF_NODE_INDEX`, `TF_NODE_COUNT` — free data parallelism across a routine's nodes.
- A routine already running on a node refuses to start again; `kill` it first.
- Sync is delete-free `rsync` in both directions; `push`/`pull` globs support `**`.
- Remote layout: `~/.tailfleet/work/<workspace>/` mirrors pushed files; run state (`.sh`, `.pid`, `.start`, `.exit`, `.log`) lives in `.tf/` inside it.

## Layout

```
tailfleet/
  cli.py      argparse subcommands, entry point
  monitor.py  Textual live-table app
  render.py   fleet table, bar gauges, alignment
  nodes.py    tailnet discovery, remote exec
  probes.py   shell probes piped to bash -s
  parse.py    probe output parsing, parallel gather
  config.py   tailfleet.yaml loading/validation
  jobs.py     sync, dispatch, ps/logs/kill
```
