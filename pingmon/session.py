"""
session.py - the interleaved A/B scheduler and data collection

The key design choice: the test is INTERLEAVED, not sequential.

Instead of "10 minutes of service A, then 10 minutes of B" - which in
practice measures network drift rather than the service - short blocks
rotate past each other:

    round 1:  baseline -> ExitLag -> NoPing
    round 2:  NoPing -> baseline -> ExitLag      (order randomised)
    ...

Each service is then compared against the baseline FROM THE SAME ROUND,
so drift between hours has no effect on the verdict.
"""

from __future__ import annotations

import csv
import random
import subprocess
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from .probe import IcmpProber, Sample, TargetProbeThread
from .stats import Metrics, compute
from .targets import Target


@dataclass
class Service:
    name: str
    mode: str = "manual"          # manual | command | none
    instruction: str = ""
    up: str = ""
    down: str = ""
    is_baseline: bool = False

    def activate(self, log) -> None:
        if self.mode == "command" and self.up:
            log(f"running: {self.up}")
            subprocess.run(self.up, shell=True, check=False)

    def deactivate(self, log) -> None:
        if self.mode == "command" and self.down:
            log(f"running: {self.down}")
            subprocess.run(self.down, shell=True, check=False)


@dataclass
class BlockResult:
    round_idx: int
    service: str
    target_key: str
    game: str
    region: str
    genre: str
    started: float
    ended: float
    metrics: Metrics


class Store:
    """Holds samples - for the live dashboard and the final analysis."""

    def __init__(self, window: int = 240) -> None:
        self.lock = threading.Lock()
        # rolling window for the live view
        self.live: dict[str, deque[Sample]] = defaultdict(
            lambda: deque(maxlen=window)
        )
        # current block buffer: target_key -> [Sample]
        self.block: dict[str, list[Sample]] = defaultdict(list)
        self.recording = False
        self.ctx_service = ""
        self.ctx_round = 0
        self.blocks: list[BlockResult] = []
        self._raw_writer: csv.writer | None = None
        self._raw_fh = None

    # -------------------------------------------------- raw csv
    def open_raw(self, path: Path) -> None:
        self._raw_fh = path.open("w", newline="", encoding="utf-8")
        self._raw_writer = csv.writer(self._raw_fh)
        self._raw_writer.writerow(
            ["ts", "round", "service", "game", "region",
             "seq", "rtt_ms", "ok", "err"]
        )

    def close_raw(self) -> None:
        if self._raw_fh:
            self._raw_fh.flush()
            self._raw_fh.close()
            self._raw_fh = None
            self._raw_writer = None

    # -------------------------------------------------- ingest
    def on_sample(self, target: Target, s: Sample) -> None:
        with self.lock:
            self.live[target.key].append(s)
            if self.recording:
                self.block[target.key].append(s)
                if self._raw_writer:
                    self._raw_writer.writerow([
                        f"{s.ts:.3f}", self.ctx_round, self.ctx_service,
                        target.game, target.region, s.seq,
                        "" if s.rtt_ms is None else f"{s.rtt_ms:.3f}",
                        int(s.ok), s.err,
                    ])

    # -------------------------------------------------- block lifecycle
    def start_block(self, service: str, round_idx: int) -> None:
        with self.lock:
            self.block.clear()
            self.ctx_service = service
            self.ctx_round = round_idx
            self.recording = True

    def finish_block(self, targets: list[Target], service: str,
                     round_idx: int, t0: float) -> list[BlockResult]:
        with self.lock:
            self.recording = False
            snapshot = {k: list(v) for k, v in self.block.items()}
            self.block.clear()
            if self._raw_fh:
                self._raw_fh.flush()

        out: list[BlockResult] = []
        by_key = {t.key: t for t in targets}
        for key, samples in snapshot.items():
            t = by_key.get(key)
            if t is None or not samples:
                continue
            rtts = [s.rtt_ms for s in samples if s.ok and s.rtt_ms is not None]
            flags = [s.ok for s in samples]
            m = compute(rtts, flags, t.genre)
            out.append(BlockResult(
                round_idx, service, key, t.game, t.region, t.genre,
                t0, time.time(), m,
            ))
        with self.lock:
            self.blocks.extend(out)
        return out

    # -------------------------------------------------- live view
    def live_metrics(self, target: Target, last_n: int = 120) -> Metrics:
        with self.lock:
            samples = list(self.live[target.key])[-last_n:]
        rtts = [s.rtt_ms for s in samples if s.ok and s.rtt_ms is not None]
        flags = [s.ok for s in samples]
        return compute(rtts, flags, target.genre)

    def live_series(self, target: Target, last_n: int = 60) -> list[float]:
        with self.lock:
            samples = list(self.live[target.key])[-last_n:]
        return [s.rtt_ms if (s.ok and s.rtt_ms is not None) else float("nan")
                for s in samples]


# ---------------------------------------------------------------- session


@dataclass
class SessionConfig:
    block_seconds: int = 60
    settle_seconds: int = 8
    rounds: int = 6
    rate_hz: float = 5.0
    timeout: float = 2.0
    randomize_order: bool = True
    outdir: Path = field(default_factory=lambda: Path("./pingmon-runs"))


class Session:
    def __init__(self, targets: list[Target], services: list[Service],
                 cfg: SessionConfig, hooks=None) -> None:
        self.targets = targets
        self.services = services
        self.cfg = cfg
        self.store = Store()
        self.hooks = hooks           # dashboard, or None
        self.threads: list[TargetProbeThread] = []
        self._abort = threading.Event()
        self.run_dir = cfg.outdir / time.strftime("run-%Y%m%d-%H%M%S")

    # -------------------------------------------------- helpers
    def log(self, msg: str) -> None:
        if self.hooks and hasattr(self.hooks, "log"):
            self.hooks.log(msg)
        else:
            print(msg, flush=True)

    def _await_manual(self, svc: Service) -> None:
        text = svc.instruction or f"Switch to '{svc.name}' now"
        if self.hooks and hasattr(self.hooks, "prompt"):
            self.hooks.prompt(text)
        else:
            print(f"\n>>> {text}\n>>> then press Enter...", flush=True)
            try:
                input()
            except EOFError:
                time.sleep(2)

    def abort(self) -> None:
        self._abort.set()

    # -------------------------------------------------- lifecycle
    def prepare(self) -> list[Target]:
        """Resolve DNS once and drop targets that do not resolve."""
        alive: list[Target] = []
        for t in self.targets:
            if t.ip or t.resolve():
                alive.append(t)
            else:
                self.log(f"[!] dropped (DNS failed): {t.key} -> {t.host}")
        self.targets = alive
        return alive

    def start_probes(self) -> None:
        icmp = IcmpProber() if any(t.method == "icmp" for t in self.targets) \
            else None
        if icmp and not IcmpProber.available():
            self.log("[!] ICMP unavailable (needs admin) - falling back to TCP")
            for t in self.targets:
                if t.method == "icmp":
                    t.method = "tcp"
            icmp = None
        for t in self.targets:
            th = TargetProbeThread(t, self.cfg.rate_hz, self.cfg.timeout,
                                   self.store.on_sample, icmp)
            th.start()
            self.threads.append(th)

    def stop_probes(self) -> None:
        for th in self.threads:
            th.stop()
        for th in self.threads:
            th.join(timeout=3)

    # -------------------------------------------------- main loop
    def run(self) -> list[BlockResult]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.store.open_raw(self.run_dir / "samples.csv")
        self.prepare()
        if not self.targets:
            self.log("No healthy targets left. Check your config.")
            return []
        self.start_probes()
        self.log(f"start: {len(self.targets)} targets x "
                 f"{len(self.services)} services x {self.cfg.rounds} rounds")

        rng = random.Random(20260920)
        try:
            for r in range(1, self.cfg.rounds + 1):
                if self._abort.is_set():
                    break
                order = list(self.services)
                if self.cfg.randomize_order:
                    rng.shuffle(order)
                for svc in order:
                    if self._abort.is_set():
                        break
                    self._run_block(r, svc)
        finally:
            self.stop_probes()
            self.store.close_raw()
            self._write_blocks_csv()
        return self.store.blocks

    def _run_block(self, round_idx: int, svc: Service) -> None:
        if self.hooks and hasattr(self.hooks, "on_block_start"):
            self.hooks.on_block_start(round_idx, svc, self.cfg.rounds)

        if svc.mode == "manual":
            self._await_manual(svc)
        else:
            svc.activate(self.log)

        # Settle phase: this data is thrown away so the transient of the
        # tunnel coming up never enters the measurement.
        if self.hooks and hasattr(self.hooks, "on_settle"):
            self.hooks.on_settle(self.cfg.settle_seconds)
        self._sleep(self.cfg.settle_seconds)

        t0 = time.time()
        self.store.start_block(svc.name, round_idx)
        self._sleep(self.cfg.block_seconds, measuring=True)
        results = self.store.finish_block(self.targets, svc.name,
                                          round_idx, t0)

        if svc.mode == "command":
            svc.deactivate(self.log)
        if self.hooks and hasattr(self.hooks, "on_block_end"):
            self.hooks.on_block_end(round_idx, svc, results)

    def _sleep(self, seconds: float, measuring: bool = False) -> None:
        end = time.perf_counter() + seconds
        while not self._abort.is_set():
            left = end - time.perf_counter()
            if left <= 0:
                return
            if self.hooks and hasattr(self.hooks, "tick"):
                self.hooks.tick(left, measuring)
            time.sleep(min(0.25, left))

    # -------------------------------------------------- output
    def _write_blocks_csv(self) -> None:
        path = self.run_dir / "blocks.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([
                "round", "service", "game", "region", "genre",
                "started", "ended", "n", "n_ok", "loss_pct",
                "min", "p50", "p90", "p95", "p99", "max", "mean",
                "jitter", "jitter_smooth", "spike", "burst_max", "score",
            ])
            for b in self.store.blocks:
                m = b.metrics
                w.writerow([
                    b.round_idx, b.service, b.game, b.region, b.genre,
                    f"{b.started:.0f}", f"{b.ended:.0f}",
                    m.n, m.n_ok, f"{m.loss_pct:.3f}",
                    f"{m.mn:.2f}", f"{m.p50:.2f}", f"{m.p90:.2f}",
                    f"{m.p95:.2f}", f"{m.p99:.2f}", f"{m.mx:.2f}",
                    f"{m.mean:.2f}", f"{m.jitter:.2f}",
                    f"{m.jitter_smooth:.2f}", f"{m.spike:.2f}",
                    m.burst_max, f"{m.score:.1f}",
                ])
