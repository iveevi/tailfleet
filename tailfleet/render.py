"""Per-node card rendering: slim inline gauges + plotext sparklines."""

import re

import plotext as plt
from rich.text import Text

PALETTE = [
    ("cyan", 6),
    ("magenta", 5),
    ("green", 2),
    ("yellow", 3),
    ("blue", 4),
    ("red", 1),
    ("bright_cyan", 14),
    ("bright_magenta", 13),
]


def palette_for(hosts):
    nc, gc = {}, {}
    for i, h in enumerate(hosts):
        nc[h], gc[h] = PALETTE[i % len(PALETTE)]
    return nc, gc


def _clamp(v):
    try:
        return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def fmt_mem(kb):
    try:
        return f"{int(kb) / 1024 / 1024:.1f}G"
    except (TypeError, ValueError):
        return "?"


def mem_pct(r):
    try:
        return int(r["MEM_USED_KB"]) / int(r["MEM_KB"]) * 100
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def mem_col(r):
    try:
        return f"{int(r['MEM_USED_KB'])/1024/1024:.1f}/{fmt_mem(r.get('MEM_KB'))}"
    except (KeyError, TypeError, ValueError):
        return fmt_mem(r.get("MEM_KB"))


def ghz(r):
    try:
        return f"{float(r['MHZ_MAX'])/1000:.1f}GHz"
    except (KeyError, TypeError, ValueError):
        return "?"


def gname(g):
    n = g["name"]
    if g.get("pci"):
        n = re.sub(r"^[^:]*:\s*", "", n)
        n = re.sub(r"\s*\(rev [^)]*\)", "", n)
        n = n.replace(" Corporation", "").replace(" Inc.", "")
    return n


def vram(g):
    try:
        return f"{int(g['mem_used'])/1024:.1f}/{int(g['mem_total'])/1024:.1f}G"
    except (KeyError, ValueError):
        return "-"


def vram_pct(g):
    try:
        return int(g["mem_used"]) / int(g["mem_total"]) * 100
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _band(pct):
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return "dim"
    return "red" if p >= 80 else "yellow" if p >= 40 else "green"


def _fmtpct(pct):
    try:
        return f"{int(float(pct))}%"
    except (TypeError, ValueError):
        return "?"


def ticker_line(results, ncolor):
    t = Text()
    for r in results:
        if len(t):
            t.append("  │  ", style="dim")
        host = r["host"] or "?"
        ok = r["status"] == "ok"
        t.append("● ", style="green" if ok else "red")
        t.append(host + " ", style=f"bold {ncolor.get(host, 'white')}")
        if not ok:
            t.append(r["status"], style="red")
            continue
        for label, val in (("cpu", r.get("CPU_UTIL")), ("ram", mem_pct(r))):
            t.append(label + " ", style="dim")
            t.append(_fmtpct(val) + " ", style=_band(val))
        g = next((x for x in (r.get("GPUS") or []) if "util" in x), None)
        if g is not None:
            t.append("gpu ", style="dim")
            t.append(_fmtpct(g["util"]), style=_band(g["util"]))
            vp = vram_pct(g)
            if vp is not None:
                t.append(" vram ", style="dim")
                t.append(_fmtpct(vp), style=_band(vp))
    return t


BANDS = [(0, 40, 2), (40, 80, 3), (80, float("inf"), 1)]


def plotext_graph(vals, width, height):
    data = [_clamp(v) for v in vals]
    if not data:
        return ""
    n = len(data)
    plt.clf()
    for lo, hi, col in BANDS:
        xs = [i for i, v in enumerate(data) if lo <= v < hi]
        if xs:
            plt.plot(xs, [data[i] for i in xs], marker="braille", color=col, fillx=True)
    plt.xlim(0, max(1, n - 1))
    plt.plotsize(max(1, width), max(1, height))
    plt.theme("clear")
    plt.frame(False)
    plt.xticks([])
    plt.yticks([])
    plt.xaxes(False, False)
    plt.yaxes(False, False)
    plt.ylim(0, 100)
    return plt.build().rstrip("\n")


LABEL_W = 5
PCT_W = 5


def gauge_line(label, pct, detail, width):
    t = Text()
    t.append(f"{label:<{LABEL_W}}", style="bold")
    barw = max(4, min(12, width - LABEL_W - PCT_W - 4))
    try:
        p = max(0, min(100, int(float(pct))))
    except (TypeError, ValueError):
        p = None
    band = _band(pct)
    fill = 0 if p is None else round(p / 100 * barw)
    t.append("▰" * fill, style=band)
    t.append("▱" * (barw - fill), style="dim")
    t.append(f"{_fmtpct(pct):>{PCT_W}}", style=f"bold {band}" if p is not None else "dim")
    used = LABEL_W + barw + PCT_W
    if detail:
        room = width - used - 2
        if room >= 4:
            d = detail if len(detail) <= room else detail[:room - 1] + "…"
            t.append(" " * (width - used - len(d)))
            t.append(d, style="dim")
    t.append("\n")
    return t


def node_body(r, hist, width, graph_h=3):
    t = Text()

    def clip(s):
        return s if len(s) <= width else s[:max(1, width - 1)] + "…"

    if r["status"] != "ok":
        t.append(clip(r.get("status") or "?") + "\n", style="red")
        if r.get("error"):
            t.append(clip(r["error"]), style="dim")
        return t

    def spark(key):
        h = (hist.get(r["host"]) or {}).get(key) if hist else None
        if h:
            t.append_text(Text.from_ansi(plotext_graph(h, width, graph_h)))
            t.append("\n")

    t.append(clip(str(r.get("CPU_MODEL", "?"))) + "\n", style="dim")

    t.append_text(gauge_line("CPU", r.get("CPU_UTIL"),
                             f"{r.get('CORES', '?')}c/{r.get('THREADS', '?')}t · {ghz(r)}", width))
    spark("cpu")

    t.append_text(gauge_line("RAM", mem_pct(r), mem_col(r), width))
    spark("mem")

    gpus = r.get("GPUS") or []
    if not gpus:
        t.append(f"{'GPU':<{LABEL_W}}", style="bold")
        t.append("none\n", style="dim")
    for g in gpus:
        extra = " · ".join(filter(None, [
            f"{g['freq']}MHz" if "freq" in g else "",
            f"{g['temp']}C" if "temp" in g else "",
        ]))
        d = gname(g) + (f" · {extra}" if extra else "")
        if "util" in g:
            t.append_text(gauge_line("GPU", g["util"], d, width))
        else:
            t.append(f"{'GPU':<{LABEL_W}}", style="bold")
            t.append(clip(d + " · no stats") + "\n", style="dim")
    spark("gpu")

    vp = next((vram_pct(g) for g in gpus if vram_pct(g) is not None), None)
    if vp is not None:
        vd = next((vram(g) for g in gpus if vram_pct(g) is not None), None)
        t.append_text(gauge_line("VRAM", vp, vd, width))
        spark("vram")

    t.rstrip()
    return t
