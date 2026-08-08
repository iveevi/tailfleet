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
tailfleet run train --wait --tail 40   # dispatch, block until it exits, print the tail
tailfleet wait train           # block on an already-running routine
tailfleet ps                   # routine × node: running / exit code / duration
tailfleet logs train@gpubox -f # tail a routine's log (@node optional if single-node)
tailfleet kill train          # TERM the routine's process group
tailfleet pull                # fetch pull-globs back into the project
tailfleet sync                # push only, no dispatch
tailfleet sync --prune        # push, then delete remote files the globs no longer match
```

### Leases

A node is reserved per Claude Code session, so parallel sessions stop landing on the same machine.
Nothing is leased automatically — the human decides.

```sh
tailfleet lease                # who holds what, by session codename
tailfleet lease take monoco    # lease a node for this session (what /lease runs)
tailfleet lease release        # give it back (defaults to this session's)
tailfleet lease hook           # report this session's node, reads hook JSON on stdin
```

Wire `lease hook` into a `UserPromptSubmit` hook in `~/.claude/settings.json` and every session is
told its node — or told it has none — each turn:

```json
{ "type": "command",
  "command": "uv run --project $HOME/tools/orion/tailfleet tailfleet lease hook 2>/dev/null || true" }
```

A lease is `~/.tailfleet/leases/<node>` holding the session id and a codename picked from a wordlist
by hashing that id, probing to the next word if it collides with a live lease. The codename is what
`status`, `monitor`, `lease list` and the statusline show, because a session has no human-readable
name of its own and several sessions can share a working directory. A lease expires after 8h without
a touch, so a session that dies never strands a node, and `take` refuses a node another session
holds.

Reassignment stays with the human: `~/.claude/commands/lease.md` makes `/lease` (no args: show;
`/lease monoco`: take) run the CLI through a slash command's `!` bash prefix, so the shell runs it
directly and the agent only sees the result. The shell has no access to the session id, so `lease
hook` records `~/.tailfleet/sessions/<claude-pid>` and `take` walks its own `/proc` parent chain to
find which session it belongs to — exact even when two sessions run in one directory.

### Semantics

- `run` is executed as one `bash -eo pipefail` script in the remote workspace, detached with `setsid`; it survives disconnects. `pipefail` matters because a routine that pipes a test runner into `grep` would otherwise always report success, which silently defeats `wait`'s exit code.
- Injected environment: `TF_NODE`, `TF_ROUTINE`, `TF_NODE_INDEX`, `TF_NODE_COUNT` — free data parallelism across a routine's nodes.
- A routine already running on a node refuses to start again; `kill` it first.
- A workspace is keyed only by name, so two checkouts sharing a directory basename — or two sessions
  driving the same repo — land in the same remote directory and quietly overwrite each other. Two
  warnings mark that boundary rather than enforcing it. `push` stamps `.tf/owner` with
  `user@host:/local/path` and warns when the previous stamp is somebody else's. `run` warns when a
  *different* routine is live in the same workspace, since the per-routine pid guard above only
  catches a collision under the same name, and routines that clean shared state (`rm -f` on goldens,
  say) will corrupt each other's inputs mid-flight. Both are warnings, not refusals: sharing a
  workspace on purpose is legitimate, and the failure it produces otherwise looks like a code bug.
- Sync is delete-free `rsync` in both directions; `push`/`pull` globs support `**`. Push expands its
  globs locally and sends the result with `--files-from`, so the remote workspace is the **union of
  every push ever made**: a file deleted from the repo, or one that stops matching a glob, stays on
  the node forever. That is silent and it bites — a test runner walking a directory will happily
  collect deleted test files and report failures that do not reproduce locally.
- `--prune` on `sync` and `run` fixes that. It sends the same manifest push just used, re-expands the
  push globs **remotely**, and deletes anything matching a glob that is not in the manifest.
  `--files-from` cannot do this itself, hence the separate pass.
- Two safeguards, both learned the hard way. Pruning is **scoped to the push globs**, never the whole
  workspace, because a routine's `.venv`, its outputs and its `pull` artifacts all live there and a
  whole-tree prune would delete them. And a glob that matches **nothing locally** is skipped, so an
  optional or gitignored asset (a dataset symlinked into one checkout but not another) is not deleted
  off the node just because you ran from the wrong worktree. The skipped globs are printed.
- `--prune` is opt-in rather than the default: deleting remote files during a routine `run` is a
  surprise the first time it removes something you wanted.
- Remote layout: `~/.tailfleet/work/<workspace>/` mirrors pushed files; run state (`.sh`, `.pid`, `.start`, `.exit`, `.log`) lives in `.tf/` inside it.
- `wait` blocks *remotely*: one SSH per node runs `pgrep -g` in a loop and returns when the process group dies, rather than the client reconnecting on a timer. It emits a `.` per poll so a long silent run does not hit an SSH idle timeout.
- `wait` exits `0` only if every node exited `0`; otherwise the first nonzero exit code, `124` on `--timeout`, `3` if the routine has no pid or left no exit marker. That makes `run --wait && next-step` safe in a script.
- `wait` will not report a previous run's result: it compares the exit marker's timestamp against `.start` and keeps waiting if the marker is older, which covers the window between dispatch clearing `.exit` and the new process group appearing.

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
  jobs.py     sync, dispatch, ps/logs/wait/kill
```
