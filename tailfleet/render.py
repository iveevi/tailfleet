"""Per-node card rendering: slim inline gauges + plotext sparklines."""

import re
from datetime import datetime

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


def _gpu_name(gpus):
    if not gpus:
        return "none", ""
    g = next((x for x in gpus if "util" in x), gpus[0])
    stats = " · ".join(filter(None, [
        f"{g['freq']}MHz" if "freq" in g else "",
        f"{g['temp']}C" if "temp" in g else "",
    ]))
    return gname(g), stats


def _name_line(name, stats, width):
    if not stats:
        return name if len(name) <= width else name[:max(1, width - 1)] + "…"
    room = width - len(stats) - 3
    if room < 4:
        line = "· " + stats
        return line if len(line) <= width else line[:max(1, width - 1)] + "…"
    n = name if len(name) <= room else name[:room - 1] + "…"
    return f"{n} · {stats}"


def node_body(r, hist, width, section_h=6):
    t = Text()

    def clip(s):
        return s if len(s) <= width else s[:max(1, width - 1)] + "…"

    if r["status"] != "ok":
        t.append(clip(r.get("status") or "?") + "\n", style="red")
        if r.get("error"):
            t.append(clip(r["error"]), style="dim")
        return t

    gpus = r.get("GPUS") or []
    g = next((x for x in gpus if "util" in x), None)
    vp = next((vram_pct(x) for x in gpus if vram_pct(x) is not None), None)
    vd = next((vram(x) for x in gpus if vram_pct(x) is not None), None)

    cpu_stats = f"{r.get('CORES', '?')}c/{r.get('THREADS', '?')}t · {ghz(r)}"
    cpu_model = str(r.get("CPU_MODEL") or "").strip() or "CPU"
    gpu_model, gpu_stats = _gpu_name(gpus)
    sections = [
        ("CPU", r.get("CPU_UTIL"), None, (cpu_model, cpu_stats), "cpu"),
        ("RAM", mem_pct(r), mem_col(r), None, "mem"),
        ("GPU", g["util"] if g else None, None, (gpu_model, gpu_stats), "gpu"),
        ("VRAM", vp, vd, None, "vram"),
    ]

    spark_h = max(1, section_h - 2)
    for i, (label, pct, detail, name, key) in enumerate(sections):
        if i:
            t.append("─" * width + "\n", style="bright_black")
        if name:
            t.append(_name_line(name[0], name[1], width) + "\n", style="dim")
        t.append_text(gauge_line(label, pct, detail, width))
        sh = max(1, spark_h - 1) if name else spark_h
        h = (hist.get(r["host"]) or {}).get(key) if hist else None
        if h:
            t.append_text(Text.from_ansi(plotext_graph(h, width, sh)))
            t.append("\n")
        else:
            t.append("\n" * sh)

    t.rstrip()
    return t


def _cpu_temp(r):
    try:
        return f"{round(int(r['CPU_TEMP']) / 1000)}C"
    except (KeyError, TypeError, ValueError):
        return ""


def _cpu_model(r):
    m = str(r.get("CPU_MODEL") or "").strip()
    if not m:
        return "CPU"
    m = re.sub(r"\((R|TM|r|tm)\)", "", m)
    m = re.sub(r"\s+@.*$", "", m)
    m = re.sub(r"\s+\d+-Core\b", "", m)
    m = re.sub(r"\b\d+th Gen\s+", "", m)
    m = m.replace(" CPU", "").replace(" Processor", "").replace(" Core", "")
    return re.sub(r"\s+", " ", m).strip() or "CPU"


_COL_N = 9
_COL_A = 40
_COL_B = 22
_COL_C = 46
_BORDER = "bright_black"
_LABEL = "bright_black"
_DIM = "bright_black"


def _clip(s, w):
    s = str(s)
    return s if len(s) <= w else s[:max(1, w - 1)] + "…"


def _mem_aligned(r):
    try:
        used = int(r["MEM_USED_KB"]) / 1024 / 1024
        total = int(r["MEM_KB"]) / 1024 / 1024
        return f"{used:>4.1f} / {total:>4.1f}G"
    except (KeyError, TypeError, ValueError):
        return fmt_mem(r.get("MEM_KB"))


def _vram_aligned(g):
    if not g:
        return f"{'-':>5} / {'':<5}"
    try:
        used = int(g["mem_used"]) / 1024
        total = int(g["mem_total"]) / 1024
        return f"{used:>4.1f} / {total:>4.1f}G"
    except (KeyError, TypeError, ValueError):
        return f"{'-':>5} / {'':<5}"


def _cell(width, *segs):
    t = Text()
    for s, st in segs:
        if isinstance(s, Text):
            t.append_text(s)
        else:
            t.append(s, style=st or "")
    if t.cell_len > width:
        t = t[:width]
    if t.cell_len < width:
        t.append(" " * (width - t.cell_len))
    return t


def _bar(pct, w):
    t = Text()
    try:
        p = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        return _cell(w, ("░" * w, _DIM))
    n = round(p / 100 * w)
    t.append("█" * n, style=_band(pct))
    t.append("░" * (w - n), style=_DIM)
    return t


def _rule(left, mid, right, ch, cols):
    n, a, b, c = cols
    return Text(left + ch * (n + 2) + mid + ch * (a + 2) + mid
                + ch * (b + 2) + mid + ch * (c + 2) + right, style=_BORDER)


def _row(cn, ca, cb, cc):
    t = Text()
    for cell in (cn, ca, cb, cc):
        t.append("│ ", style=_BORDER)
        t.append_text(cell)
        t.append(" ", style=_BORDER)
    t.append("│", style=_BORDER)
    return t


def _cols_for(width):
    n, b = _COL_N, _COL_B
    remaining = max(_COL_A + _COL_C, width - (n + b + 13))
    a = max(_COL_A, int(remaining * 0.42))
    c = max(_COL_C, remaining - a)
    return n, a, b, c


def snapshot_text(results, width=116, interval=None):
    ncolor, _ = palette_for([r["host"] for r in results])
    cols = _cols_for(width)
    n, ca, cb, cc = cols
    inner = n + ca + cb + cc + 9
    up = sum(1 for r in results if r["status"] == "ok")

    clock = datetime.now().strftime("%H:%M:%S")
    updown = f"{up}/{len(results)} up"
    rate = f"⟳ {interval:g}s" if interval else ""
    right_plain = f"{(rate + '    ') if rate else ''}{clock}    {updown}"
    title = Text()
    title.append("│ ", style=_BORDER)
    title.append("tailfleet", style="bold white")
    title.append(" " * max(1, inner - len("tailfleet") - len(right_plain)))
    if rate:
        title.append(rate, style=_LABEL)
        title.append("    ")
    title.append(clock, style=_LABEL)
    title.append("    ")
    title.append(updown, style="green" if up == len(results) else "yellow")
    title.append(" │", style=_BORDER)

    lines = [
        _rule("╭", "─", "╮", "─", cols),
        title,
        _rule("├", "┬", "┤", "─", cols),
        _row(_cell(n, ("Node", _LABEL)),
             _cell(ca, ("CPU", _LABEL)),
             _cell(cb, ("Memory-Usage", _LABEL)),
             _cell(cc, ("GPU   Name", _LABEL))),
        _row(_cell(n, ("", _LABEL)),
             _cell(ca, ("Temp   Util   Cores · Clock", _LABEL)),
             _cell(cb, ("Used / Total", _LABEL)),
             _cell(cc, ("Temp   GPU-Util · VRAM-Usage", _LABEL))),
        _rule("╞", "╪", "╡", "═", cols),
    ]

    for i, r in enumerate(results):
        host = r["host"] or "?"
        acc = ncolor.get(host, "white")
        is_self = bool(r.get("self"))

        if r["status"] != "ok":
            n1 = _node_cell(host, _DIM, is_self, n)
            lines.append(_row(n1, _cell(ca, (_clip(r.get("status") or "?", ca), _DIM)),
                              _cell(cb), _cell(cc)))
            lines.append(_row(_cell(n), _cell(ca), _cell(cb), _cell(cc)))
            if i < len(results) - 1:
                lines.append(_rule("├", "┼", "┤", "─", cols))
            continue

        n1 = _node_cell(host, f"bold {acc}", is_self, n)
        n2 = _cell(n)

        gpus = r.get("GPUS") or []
        g = next((x for x in gpus if "util" in x), None)
        gv = next((x for x in gpus if vram_pct(x) is not None), None)
        vp = vram_pct(gv) if gv else None
        gm, _ = _gpu_name(gpus)
        temp = next((f"{x['temp']}C" for x in gpus if "temp" in x), "")
        cu, gu = r.get("CPU_UTIL"), (g["util"] if g else None)

        ct = _cpu_temp(r)
        cores = f"{str(r.get('CORES', '?')):>2}c/{str(r.get('THREADS', '?')):>2}t · {ghz(r):>6}"
        a1 = _cell(ca, (_clip(_cpu_model(r), ca), "default"))
        a2 = _cell(ca,
                   (ct.rjust(4), _LABEL), ("  ", ""),
                   (_fmtpct(cu).rjust(4), _band(cu)), ("  ", ""), *_seg_pair(_bar(cu, 8)),
                   ("  ", ""), (cores, _LABEL))
        b1 = _cell(cb, (_mem_aligned(r), "default"))
        b2 = _cell(cb, (_fmtpct(mem_pct(r)).rjust(4), _band(mem_pct(r))),
                   ("  ", ""), *_seg_pair(_bar(mem_pct(r), 8)))
        c1 = _cell(cc, (_clip(gm, cc), "default"))
        c2 = _cell(cc,
                   (temp.rjust(4), _LABEL), ("  ", ""),
                   *_seg_pair(_bar(gu, 6)), (_fmtpct(gu).rjust(4), _band(gu)),
                   ("  ", ""), (_vram_aligned(gv), _LABEL), (" ", ""),
                   *_seg_pair(_bar(vp, 6)), (_fmtpct(vp).rjust(4), _band(vp)))

        lines.append(_row(n1, a1, b1, c1))
        lines.append(_row(n2, a2, b2, c2))
        if i < len(results) - 1:
            lines.append(_rule("├", "┼", "┤", "─", cols))

    lines.append(_rule("╰", "┴", "╯", "─", cols))

    out = Text()
    for j, ln in enumerate(lines):
        if j:
            out.append("\n")
        out.append_text(ln)
    return out


def _seg_pair(text):
    return [(text, None)]


def _node_cell(host, name_style, is_self, width):
    t = Text()
    style = f"{name_style} italic" if is_self else name_style
    t.append(_clip(host, width), style=style)
    if t.cell_len < width:
        t.append(" " * (width - t.cell_len))
    return t


def print_snapshot(results):
    from rich.console import Console

    con = Console(highlight=False)
    con.print(snapshot_text(results, con.width), soft_wrap=True)
