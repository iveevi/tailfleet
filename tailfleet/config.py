"""tailfleet.yaml discovery, loading, validation."""

import os
import re
from pathlib import Path

import yaml

CONFIG_NAME = "tailfleet.yaml"
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class ConfigError(Exception):
    pass


def find_config(start=None):
    p = Path(start or os.getcwd()).resolve()
    for d in (p, *p.parents):
        f = d / CONFIG_NAME
        if f.is_file():
            return f
    raise ConfigError(f"no {CONFIG_NAME} found in this or any parent directory")


def _name(value, what):
    s = str(value)
    if not _NAME.match(s):
        raise ConfigError(f"invalid {what} {s!r}: use only [A-Za-z0-9._-]")
    return s


def _strlist(value, what):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{what} must be a list of strings")
    return value


def load_config(start=None):
    path = find_config(start)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {e}")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    routines = {}
    for rname, r in (raw.get("routines") or {}).items():
        rname = _name(rname, "routine name")
        if not isinstance(r, dict):
            raise ConfigError(f"routine {rname}: must be a mapping")
        nodes = _strlist(r.get("nodes"), f"routine {rname}: nodes")
        if not nodes:
            raise ConfigError(f"routine {rname}: nodes is required")
        run = r.get("run")
        if isinstance(run, list):
            run = "\n".join(run)
        if not isinstance(run, str) or not run.strip():
            raise ConfigError(f"routine {rname}: run is required")
        routines[rname] = {"nodes": nodes, "run": run}

    return {
        "root": path.parent,
        "workspace": _name(raw.get("workspace") or path.parent.name, "workspace name"),
        "push": _strlist(raw.get("push"), "push"),
        "pull": _strlist(raw.get("pull"), "pull"),
        "routines": routines,
    }
