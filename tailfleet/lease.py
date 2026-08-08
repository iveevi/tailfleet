"""Per-session node leases: a tailnet node reserved by an explicit /lease, never automatically."""

import hashlib
import json
import sys
import time
from pathlib import Path

from .nodes import all_nodes

BASE = Path.home() / ".tailfleet"
LEASE_DIR = BASE / "leases"
SESSION_DIR = BASE / "sessions"
TTL = 8 * 3600

WORDS = """
amber anchor anvil arbor archer arrow ash aspen atlas aurora badger balsam banjo
basalt beacon beetle birch bison blaze bramble brass briar bronze burrow cactus
canyon cedar cinder cirrus clover cobalt comet copper coral cove crag crane crater
cricket cypress dagger delta dune ember falcon fathom fern fjord flint forge fossil
gale garnet geyser glacier granite grotto grove gully harbor hawk heather hemlock
heron hollow indigo iris ivory jasper jetty juniper kelp kestrel lagoon lantern
larch ledge lichen lily lumen lupine lynx magnet mallow maple marlin marsh mesa
meteor mica midge mint moraine moss nectar nettle nimbus oak obsidian onyx opal
orchid osprey otter pebble petal pewter pine pinion plover pollen prairie quarry
quartz quill rapid raven reef reed ridge rill ripple rowan rune saffron sage sable
sandpiper sapling sedge shale shoal sienna silt sleet slate sorrel spruce squall
stellar sumac summit talon tamarisk teal thicket thistle thorn tidal timber topaz
torrent trellis tundra umber vale vellum verbena vireo walnut warbler willow
wisteria wren yarrow zephyr
""".split()


def _sweep():
    now = time.time()
    for d in (LEASE_DIR, SESSION_DIR):
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("*"):
            if f.is_file() and now - f.stat().st_mtime > TTL:
                f.unlink(missing_ok=True)


def codename(sid, taken=()):
    i = int(hashlib.sha256(sid.encode()).hexdigest()[:8], 16) % len(WORDS)
    for k in range(len(WORDS)):
        w = WORDS[(i + k) % len(WORDS)]
        if w not in taken:
            return w
    return sid[:8]


def leases():
    _sweep()
    out = {}
    for f in sorted(LEASE_DIR.glob("*")):
        if not f.is_file():
            continue
        parts = (f.read_text().strip().split("\n") + [""])[:2]
        out[f.name] = {"sid": parts[0], "name": parts[1] or parts[0][:8]}
    return out


def held_by(sid):
    return next((h for h, v in leases().items() if v["sid"] == sid), None)


def _ancestors():
    pid = str(Path("/proc/self").resolve().name)
    seen = []
    while pid and pid != "1" and len(seen) < 32:
        seen.append(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        pid = stat.rsplit(")", 1)[1].split()[1]
    return seen


def register(sid):
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    for pid in _ancestors():
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            continue
        if comm == "claude":
            (SESSION_DIR / pid).write_text(sid + "\n")
            return pid
    return None


def current_sid():
    _sweep()
    for pid in _ancestors():
        f = SESSION_DIR / pid
        if f.is_file():
            return f.read_text().strip()
    return None


def take(new):
    sid = current_sid()
    if not sid:
        raise SystemExit("cannot tell which session this is; run /lease from inside Claude Code")
    held = leases()
    old = held_by(sid)
    if old == new:
        print(f"already holding {new}")
        return
    if new not in {n["host"] for n in all_nodes()}:
        raise SystemExit(f"node not found or offline: {new}")
    if new in held:
        raise SystemExit(f"{new} is held by {held[new]['name']}; release it first")
    name = held[old]["name"] if old else codename(sid, {v["name"] for v in held.values()})
    (LEASE_DIR / new).write_text(f"{sid}\n{name}\n")
    if old:
        (LEASE_DIR / old).unlink(missing_ok=True)
        print(f"{name}: {old} -> {new}")
    else:
        print(f"{name}: leased {new}")


def release(host=None):
    sid = current_sid()
    if host is None:
        host = held_by(sid) if sid else None
        if not host:
            raise SystemExit("this session holds no lease")
    (LEASE_DIR / host).unlink(missing_ok=True)
    print(f"released {host}")


def show():
    held = leases()
    mine = held_by(current_sid() or "")
    if not held:
        print("no leases")
    for h, v in held.items():
        print(f"{h}\t{v['name']}{'  (this session)' if h == mine else ''}")


def hook():
    try:
        payload = json.load(sys.stdin) or {}
    except (json.JSONDecodeError, ValueError):
        payload = {}
    sid = payload.get("session_id") or "unknown"
    register(sid)
    host = held_by(sid)
    if not host:
        free = [n["host"] for n in all_nodes() if n["host"] not in leases()]
        print("No tailfleet node is leased to this session. Do not run tailfleet routines on any "
              f"node until the user leases one with /lease <node>. Free right now: "
              f"{', '.join(free) or 'none'}.")
        return
    print(f"Your tailfleet node for this session: {host} (codename {leases()[host]['name']}). "
          f"Use only that node; never change a lease yourself — the user does that with /lease.")
