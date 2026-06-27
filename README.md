# tailfleet

Hardware inventory and job control across your Tailscale network. One file, no
install. Discovers online Linux nodes via `tailscale status`, probes them over
SSH, and runs tracked jobs with directory sync.

## Requirements

- `tailscale` (nodes discovered from `tailscale status --json`)
- `ssh` and `rsync` for remote nodes
- `uv` (the script self-installs `rich` + `textual` via its inline metadata)
- Key-based SSH to each node (`BatchMode=yes`; no password prompts)

## Usage

```
./tailfleet.py [--json] [--timeout SECS] <command>
```

Run with no command to print the hardware inventory table.

### Commands

| Command | Description |
| --- | --- |
| `(none)` | Inventory table: CPU, RAM, GPU, util, temp per node |
| `monitor` | Live-refreshing table with braille usage graphs + running jobs |
| `run <node> <dir> -- <cmd>` | Sync `dir` to the node, run `cmd` as a tracked job, tail it, pull results back |
| `exec <node> -- <cmd>` | One-off command on a node (no sync, no tracking) |
| `sync <node> <dir>` | Rsync a directory to/from a node's workdir (no job) |
| `jobs` | List jobs across the fleet |
| `logs <id> [-f]` | Show (or follow) a job's log |
| `fetch <id>` | Pull a job's results back into its origin dir |
| `kill <id> [--force]` | Signal a running job's process group (TERM then KILL) |
| `rm <id> [--force]` | Remove a job's marker/log |

The local node is marked `*` in tables.

## Examples

```
# inventory
./tailfleet.py

# live dashboard
./tailfleet.py monitor

# run a training job on node "renoir", stream output, auto-pull results
./tailfleet.py run renoir ./project -- python train.py --epochs 10

# detach and follow later
./tailfleet.py run renoir ./project --detach -- python train.py
./tailfleet.py logs renoir-1a2b3c -f
./tailfleet.py fetch renoir-1a2b3c

# one-off command, no sync
./tailfleet.py exec renoir -- nvidia-smi
```

## How it works

- **Discovery**: `all_nodes()` reads `tailscale status --json` and keeps online
  Linux peers plus self.
- **Probing**: a shell snippet (`PROBE`) is piped to `bash -s` on each node in
  parallel, emitting tab-separated CPU/RAM/GPU fields parsed back into a table.
- **Jobs**: `run` rsyncs the directory to `~/.tailfleet/<project>` on the node,
  launches the command under `setsid` with a PID/exit/log marker, then tails it.
  Results rsync back on completion. Re-running an identical job (same dir+cmd)
  is refused unless `--force`.
- **Workdir**: each project maps to `~/.tailfleet/<basename>-<hash>` on the
  node. Markers live in `~/.tailfleet/jobs/`. Exited markers older than 24h are
  pruned automatically.

## Notes

- Push is additive by default. Pass `--mirror` for `rsync --delete`.
- Pull keeps newer local files by default. Pass `--overwrite` to force.
- Add a `.tailfleetignore` (gitignore-style) to a directory to extend the
  default excludes (`.git`, `.venv`, `__pycache__`, `node_modules`, `*.pyc`).
- Put PATH setup for `nvcc`/`uv`/`conda` in `~/.tailfleet/env` on a node; both
  `run` and `exec` source it.
