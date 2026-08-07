"""Argument parsing and entry point."""

import argparse
import json
import sys


def build_parser():
    ap = argparse.ArgumentParser(prog="tailfleet", description="tailnet live monitor and job runner")
    sub = ap.add_subparsers(dest="cmd")

    m = sub.add_parser("monitor", help="live fleet dashboard")
    m.add_argument("--interval", type=float, default=1, help="seconds between refreshes (default 1)")
    m.add_argument("--rediscover", type=float, default=15, help="seconds between tailnet re-discovery (default 15)")
    m.add_argument("--timeout", type=float, default=20, help="per-node probe timeout seconds (default 20)")

    t = sub.add_parser("status", help="one-shot fleet table (default)")
    t.add_argument("--timeout", type=float, default=20, help="per-node probe timeout seconds (default 20)")
    t.add_argument("--json", action="store_true", help="emit the raw probe results as JSON")

    sub.add_parser("sync", help="push whitelisted files to all routine nodes")
    sub.add_parser("pull", help="fetch pull-globs back from all routine nodes")
    sub.add_parser("ps", help="show routine state across nodes")

    j = sub.add_parser("jobs", help="show jobs running on every node, any workspace")
    j.add_argument("-a", "--all", action="store_true", help="include exited and stale jobs")

    r = sub.add_parser("run", help="sync, then dispatch a routine on its nodes")
    r.add_argument("routine")
    r.add_argument("--wait", action="store_true", help="block until the routine exits everywhere")
    r.add_argument("--timeout", type=float, help="seconds to wait before giving up")
    r.add_argument("--poll", type=float, default=5, help="remote poll interval seconds (default 5)")
    r.add_argument("--tail", type=int, default=0, help="print the last N log lines when it exits")

    w = sub.add_parser("wait", help="block until a routine exits on every node")
    w.add_argument("routine")
    w.add_argument("--timeout", type=float, help="seconds to wait before giving up")
    w.add_argument("--poll", type=float, default=5, help="remote poll interval seconds (default 5)")
    w.add_argument("--tail", type=int, default=0, help="print the last N log lines when it exits")

    l = sub.add_parser("logs", help="tail a routine's log")
    l.add_argument("target", help="<routine> or <routine>@<node>")
    l.add_argument("-f", "--follow", action="store_true")
    l.add_argument("-n", "--lines", type=int, default=120)

    k = sub.add_parser("kill", help="TERM a routine's process group on its nodes")
    k.add_argument("routine")
    return ap


def main():
    argv = sys.argv[1:]
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["status", *argv]
    args = build_parser().parse_args(argv)

    if args.cmd == "monitor":
        from .monitor import MonitorApp
        MonitorApp(args.interval, args.rediscover, args.timeout).run()
        return

    if args.cmd == "status":
        from .nodes import all_nodes
        from .parse import gather_monitor
        from .render import print_snapshot
        nodes = sorted(all_nodes(), key=lambda n: (not n["self"], n["host"] or ""))
        results = sorted(gather_monitor(nodes, {}, args.timeout),
                         key=lambda r: (not r["self"], r["host"] or ""))
        if args.json:
            print(json.dumps(results))
        else:
            print_snapshot(results)
        return

    from . import jobs

    if args.cmd == "jobs":
        jobs.jobs(args.all)
        return

    from .config import ConfigError, load_config
    try:
        cfg = load_config()
        if args.cmd == "sync":
            jobs.push(cfg, jobs.all_config_nodes(cfg))
        elif args.cmd == "pull":
            jobs.pull(cfg, jobs.all_config_nodes(cfg))
        elif args.cmd == "ps":
            jobs.ps(cfg)
        elif args.cmd == "run":
            jobs.run_routine(cfg, args.routine)
            if args.wait:
                sys.exit(jobs.wait(cfg, args.routine, args.timeout, args.poll, args.tail))
        elif args.cmd == "wait":
            sys.exit(jobs.wait(cfg, args.routine, args.timeout, args.poll, args.tail))
        elif args.cmd == "logs":
            sys.exit(jobs.logs(cfg, args.target, args.follow, args.lines))
        elif args.cmd == "kill":
            jobs.kill(cfg, args.routine)
    except ConfigError as e:
        print(f"tailfleet: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
