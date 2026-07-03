"""Live-refreshing Textual dashboard: full-width fleet table."""

import time

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Center
from textual.widgets import Static

from .nodes import all_nodes
from .parse import gather_monitor
from .render import snapshot_text


class MonitorApp(App):
    CSS = """
    Screen { align: center top; }
    #table { width: auto; height: auto; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("minus,underscore", "rate(-1)", "Faster"),
        ("plus,equals_sign,equal", "rate(1)", "Slower"),
    ]

    def __init__(self, interval, rediscover, timeout):
        super().__init__()
        self.interval = interval
        self.rediscover = rediscover
        self.timeout = timeout
        self.static = {}
        self.nodes = []
        self.last_disc = 0.0
        self.results = []

    def compose(self) -> ComposeResult:
        with Center():
            yield Static(id="table")

    def on_mount(self):
        self.ansi_color = True
        self.theme = "ansi-dark"
        self.query_one("#table", Static).update("discovering…")
        self.refresh_data()

    @work(thread=True, exclusive=True)
    def refresh_data(self):
        t0 = time.time()
        try:
            if not self.nodes or time.time() - self.last_disc > self.rediscover:
                self.nodes = all_nodes()
                self.last_disc = time.time()
                self.static.clear()
            results = sorted(gather_monitor(self.nodes, self.static, self.timeout),
                             key=lambda r: (not r["self"], r["host"] or ""))
        except Exception:
            results = None
        self.call_from_thread(self.after_refresh, results, time.time() - t0)

    def after_refresh(self, results, elapsed):
        if results is not None:
            self.results = results
            self.render_table()
        self.set_timer(max(0.2, self.interval - elapsed), self.refresh_data)

    def action_rate(self, direction):
        step = 0.1 if self.interval < 1 else 0.5
        self.interval = round(min(10.0, max(0.2, self.interval + direction * step)), 2)
        self.render_table()

    def render_table(self):
        if not self.results:
            return
        width = min(self.size.width, 160)
        self.query_one("#table", Static).update(
            snapshot_text(self.results, width, self.interval))
