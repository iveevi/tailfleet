"""Live-refreshing Textual dashboard: full-height node columns, arrow-key paging."""

import time
from collections import deque

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, HorizontalScroll
from textual.widgets import Static

from .nodes import all_nodes
from .parse import gather_monitor
from .render import node_body, palette_for, ticker_line, vram_pct


class NodeCard(Static):
    pass


class Ticker(HorizontalScroll):
    can_focus = False


class MonitorApp(App):
    CSS = """
    #head { height: 1; padding: 0 2; }
    #headleft { width: auto; }
    #headright { width: 1fr; text-align: right; }
    #row { height: 1fr; padding: 0 1; }
    #ticker { height: 1; padding: 0 1; scrollbar-size: 0 0; background: ansi_bright_black; }
    #tickertext { width: auto; background: ansi_bright_black; }
    NodeCard {
        border: round ansi_bright_black;
        border-title-style: bold;
        padding: 0 1;
        width: 1fr;
        height: 100%;
        margin: 0 1 0 0;
    }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("left", "page(-1)", "Prev"),
        ("right", "page(1)", "Next"),
    ]

    def __init__(self, interval, rediscover, timeout):
        super().__init__()
        self.interval = interval
        self.rediscover = rediscover
        self.timeout = timeout
        self.static = {}
        self.nodes = []
        self.last_disc = 0.0
        self.hist = {}
        self.results = []
        self.cards = {}
        self._window_hosts = None
        self.offset = 0
        self.per_page = 2
        self.tick_pos = 0.0
        self.ticker_speed = 11.0
        self._ticker_period = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="head"):
            yield Static(id="headleft")
            yield Static(id="headright")
        yield Horizontal(id="row")
        with Ticker(id="ticker"):
            yield Static(id="tickertext")

    TICK_DT = 0.05

    def on_mount(self):
        self.ansi_color = True
        self.theme = "ansi-dark"
        self.query_one("#headleft", Static).update("[b]tailfleet[/] [dim]· discovering…[/]")
        self.set_interval(self.TICK_DT, self._scroll_ticker)
        self.refresh_data()

    def _scroll_ticker(self):
        if self._ticker_period <= 0:
            return
        self.tick_pos = (self.tick_pos + self.ticker_speed * self.TICK_DT) % self._ticker_period
        self.query_one("#ticker", Ticker).scroll_to(int(self.tick_pos), 0, animate=False)

    @work(thread=True, exclusive=True)
    def refresh_data(self):
        try:
            if not self.nodes or time.time() - self.last_disc > self.rediscover:
                self.nodes = all_nodes()
                self.last_disc = time.time()
                self.static.clear()
            results = sorted(gather_monitor(self.nodes, self.static, self.timeout),
                             key=lambda r: (not r["self"], r["host"] or ""))
        except Exception:
            results = None
        self.call_from_thread(self.after_refresh, results)

    def after_refresh(self, results):
        if results is not None:
            for r in results:
                self._record_history(r)
            self.results = results
            self.render_cards()
        self.set_timer(max(0.2, self.interval), self.refresh_data)

    def action_page(self, direction):
        self.offset += direction
        self.render_cards()

    def _record_history(self, r):
        if r["status"] != "ok":
            return
        h = self.hist.setdefault(r["host"], {k: deque(maxlen=240) for k in ("cpu", "mem", "gpu", "vram")})
        h["cpu"].append(r.get("CPU_UTIL", 0))
        try:
            h["mem"].append(int(r["MEM_USED_KB"]) / int(r["MEM_KB"]) * 100)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass
        gutil = next((g["util"] for g in (r.get("GPUS") or []) if "util" in g), None)
        if gutil is not None:
            h["gpu"].append(gutil)
        vp = next((vram_pct(g) for g in (r.get("GPUS") or []) if vram_pct(g) is not None), None)
        if vp is not None:
            h["vram"].append(vp)

    def _graph_h(self, window):
        avail = self.size.height - 4
        ng = max([len(r.get("GPUS") or []) for r in window if r["status"] == "ok"] + [1])
        fixed = 5 + max(0, ng - 1)
        return max(3, (avail - fixed) // 4)

    def _render_head(self, results, lo, hi):
        up = sum(1 for r in results if r["status"] == "ok")
        self.query_one("#headleft", Static).update(
            f"[b]tailfleet[/] [dim]·[/] {len(results)} nodes [dim]·[/] "
            f"[{'green' if up == len(results) else 'yellow'}]{up} up[/]"
        )
        larr = "b" if self.offset > 0 else "dim"
        rarr = "b" if hi < len(results) else "dim"
        self.query_one("#headright", Static).update(
            f"[{larr}]◂[/] [dim]{lo}–{hi}/{len(results)}[/] [{rarr}]▸[/]"
        )

    def render_cards(self):
        if not self.results:
            return
        results = self.results
        self.per_page = max(1, min(3, self.size.width // 48))
        self.offset = min(max(0, self.offset), max(0, len(results) - self.per_page))
        window = results[self.offset:self.offset + self.per_page]
        hosts = [r["host"] for r in window]
        self._render_head(results, self.offset + 1, self.offset + len(window))

        row = self.query_one("#row", Horizontal)
        if hosts != self._window_hosts:
            for c in list(self.cards.values()):
                c.remove()
            self.cards = {}
            for host in hosts:
                card = NodeCard()
                self.cards[host] = card
                row.mount(card)
            self._window_hosts = hosts

        ncolor, _ = palette_for([r["host"] for r in results])
        inner = max(18, self.size.width // self.per_page - 6)
        gh = self._graph_h(window)
        for r in window:
            card = self.cards[r["host"]]
            host = r["host"] or "?"
            acc = ncolor.get(host, "white")
            ok = r["status"] == "ok"
            card.styles.border = ("round", f"ansi_{acc}" if ok else "ansi_red")
            dot = "[ansi_green]●[/]" if ok else "[ansi_red]●[/]"
            star = "★ " if r["self"] else ""
            card.border_title = f"{dot} [bold ansi_{acc}]{star}{host}[/]"
            card.update(node_body(r, self.hist, inner, gh))

        full = ticker_line(results, ncolor)
        gap = "          "
        self._ticker_period = full.cell_len + len(gap)
        content = full.copy()
        content.append(gap)
        content.append_text(full)
        content.append(gap)
        self.query_one("#tickertext", Static).update(content)
