"""
dashboard.py - live terminal monitoring

Everything here is English on purpose: the Windows console does no
bidirectional reshaping, so right-to-left text comes out reversed and
breaks column alignment.
"""

from __future__ import annotations

import math
import time
from collections import deque

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .stats import rate

SPARK = "▁▂▃▄▅▆▇█"

STYLE = {
    "good": "bold green",
    "ok": "bold yellow",
    "bad": "bold red",
    "?": "dim",
}


def sparkline(values: list[float], width: int = 28) -> Text:
    """Compact trend line. NaN (a lost packet) shows as a red x."""
    vals = values[-width:]
    if not vals:
        return Text("-", style="dim")
    finite = [v for v in vals if not math.isnan(v)]
    if not finite:
        return Text("x" * len(vals), style="bold red")
    lo, hi = min(finite), max(finite)
    rng = (hi - lo) or 1.0
    out = Text()
    for v in vals:
        if math.isnan(v):
            out.append("x", style="bold red")
            continue
        idx = int((v - lo) / rng * (len(SPARK) - 1))
        # colour relative to this window's own range
        style = "green" if v <= lo + rng * 0.4 else (
            "yellow" if v <= lo + rng * 0.75 else "red")
        out.append(SPARK[idx], style=style)
    return out


def fmt(value: float, metric: str, digits: int = 1,
        suffix: str = "") -> Text:
    if value is None or math.isnan(value):
        return Text("-", style="dim")
    return Text(f"{value:.{digits}f}{suffix}",
                style=STYLE[rate(metric, value)])


class Dashboard:
    """Hooks the Session calls, plus the live rendering."""

    def __init__(self, session_ref_getter, refresh: float = 4.0) -> None:
        self._get = session_ref_getter    # () -> Session
        self.console = Console()
        self.live: Live | None = None
        self.logs: deque[str] = deque(maxlen=6)
        self.round_idx = 0
        self.total_rounds = 0
        self.service = "-"
        self.phase = "idle"               # settle | measure | waiting
        self.remaining = 0.0
        self.phase_total = 1.0
        self.started = time.time()
        self.blocks_done = 0

    # ---------------------------------------------------- hooks
    def log(self, msg: str) -> None:
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def on_block_start(self, round_idx: int, svc, total_rounds: int) -> None:
        self.round_idx = round_idx
        self.total_rounds = total_rounds
        self.service = svc.name
        self.phase = "waiting"

    def on_settle(self, seconds: float) -> None:
        self.phase = "settle"
        self.phase_total = max(seconds, 0.1)

    def on_block_end(self, round_idx: int, svc, results) -> None:
        self.blocks_done += 1
        if results:
            best = max(results, key=lambda r: r.metrics.score)
            self.log(f"block done - {svc.name} - best: "
                     f"{best.game}/{best.region} score={best.metrics.score}")

    def tick(self, remaining: float, measuring: bool) -> None:
        self.remaining = remaining
        if measuring:
            if self.phase != "measure":
                self.phase = "measure"
                sess = self._get()
                self.phase_total = max(sess.cfg.block_seconds, 0.1)
        self._render()

    def prompt(self, text: str) -> None:
        """Pause the live view, get the user's confirmation, resume."""
        if self.live:
            self.live.stop()
        self.console.rule("[bold yellow]ACTION NEEDED")
        self.console.print(Panel(
            Align.center(Text(text, style="bold white")),
            border_style="yellow",
            title="Switch service",
            subtitle="then press Enter",
        ))
        try:
            input()
        except EOFError:
            time.sleep(2)
        if self.live:
            self.live.start()

    # ---------------------------------------------------- render
    def _header(self) -> Panel:
        elapsed = time.time() - self.started
        phase_label = {"settle": "settling (data discarded)",
                       "measure": "measuring",
                       "waiting": "waiting for service switch",
                       "idle": "ready"}[self.phase]
        colour = {"settle": "yellow", "measure": "green",
                  "waiting": "magenta", "idle": "dim"}[self.phase]

        done = max(0.0, min(1.0, 1 - self.remaining / self.phase_total))
        bar = ProgressBar(total=1.0, completed=done, complete_style=colour)

        # Single-line header so the table still fits on a 24-line terminal.
        line = Table.grid(padding=(0, 2), expand=True)
        for _ in range(6):
            line.add_column(justify="left")
        line.add_row(
            Text.assemble(("service ", "dim"),
                          (self.service, "bold cyan")),
            Text.assemble(("round ", "dim"),
                          (f"{self.round_idx}/{self.total_rounds}", "bold")),
            Text(phase_label, style=colour),
            Text.assemble(("left ", "dim"),
                          (f"{self.remaining:.0f}s", "bold")),
            Text.assemble(("blocks ", "dim"), (str(self.blocks_done), "bold")),
            Text.assemble(("elapsed ", "dim"),
                          (time.strftime("%H:%M:%S", time.gmtime(elapsed)),
                           "bold")),
        )
        return Panel(Group(line, bar), title="pingmon - live monitor",
                     border_style=colour, padding=(0, 1))

    def _table(self) -> Panel:
        sess = self._get()
        tbl = Table(expand=True, header_style="bold white on grey23",
                    row_styles=["", "on grey11"])
        tbl.add_column("Game", style="bold", no_wrap=True)
        tbl.add_column("Region", no_wrap=True)
        tbl.add_column("p50", justify="right", no_wrap=True)
        tbl.add_column("p95", justify="right", no_wrap=True)
        tbl.add_column("jit", justify="right", no_wrap=True)
        tbl.add_column("spike", justify="right", no_wrap=True)
        tbl.add_column("loss", justify="right", no_wrap=True)
        tbl.add_column("brst", justify="right", no_wrap=True)
        tbl.add_column("score", justify="right", no_wrap=True)
        tbl.add_column("last 28 probes", no_wrap=True)

        rows = []
        for t in sess.targets:
            m = sess.store.live_metrics(t)
            rows.append((t, m))
        rows.sort(key=lambda r: (r[0].game,
                                 r[1].p50 if not math.isnan(r[1].p50)
                                 else 9e9))

        for t, m in rows:
            score_style = ("bold green" if m.score >= 75 else
                           "bold yellow" if m.score >= 50 else "bold red")
            tbl.add_row(
                t.game,
                t.region[:22],
                fmt(m.p50, "p50"),
                fmt(m.p95, "p50"),
                fmt(m.jitter, "jitter"),
                fmt(m.spike, "spike"),
                fmt(m.loss_pct, "loss", 2, "%"),
                Text(str(m.burst_max),
                     style=STYLE[rate("burst", float(m.burst_max))]),
                Text(f"{m.score:.0f}", style=score_style),
                sparkline(sess.store.live_series(t, 28)),
            )
        return Panel(tbl, title="targets (rolling window of 120 probes)",
                     border_style="blue")

    def _footer(self) -> Panel:
        lines = list(self.logs)[-3:] or ["-"]
        body = Group(*[Text(l, style="dim", overflow="ellipsis",
                            no_wrap=True) for l in lines])
        legend = Text(
            "green = good | yellow = acceptable | red = bad | "
            "x = lost packet | brst = longest consecutive-loss run",
            style="dim italic", overflow="ellipsis", no_wrap=True)
        return Panel(Group(body, legend), border_style="grey35",
                     title="events", padding=(0, 1))

    def _layout(self) -> Layout:
        lay = Layout()
        lay.split_column(
            Layout(self._header(), size=4),
            # The table always gets the biggest share - data beats chrome.
            Layout(self._table(), ratio=1, minimum_size=6),
            Layout(self._footer(), size=6),
        )
        return lay

    def _render(self) -> None:
        if self.live:
            self.live.update(self._layout())

    # ---------------------------------------------------- context
    def __enter__(self) -> "Dashboard":
        self.live = Live(self._layout(), console=self.console,
                         refresh_per_second=4, screen=False)
        self.live.start()
        return self

    def __exit__(self, *exc) -> None:
        if self.live:
            self.live.stop()
            self.live = None
