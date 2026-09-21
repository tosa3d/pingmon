"""
stats.py - metrics and statistical analysis

The metrics that actually matter for gaming:
  p50            median RTT - "your normal ping"
  p95 / p99      the tail - the spikes you actually feel
  jitter (IPDV)  mean |difference between consecutive pings|
  spike          p99 - p50, how tall the jumps are
  loss           percentage of lost packets
  burst          longest run of consecutive losses -> a real freeze

Analysis note: the unit of analysis is the BLOCK, not the sample, because
samples inside one block are heavily correlated. Comparisons are paired
within each round so that network drift cancels out.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


# ---------------------------------------------------------------- helpers


def percentile(values: list[float], q: float) -> float:
    """Linearly interpolated percentile. q is 0-100."""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[int(pos)]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def ipdv_jitter(rtts: list[float]) -> float:
    """Mean absolute difference between consecutive pings (IPDV)."""
    if len(rtts) < 2:
        return float("nan")
    diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
    return sum(diffs) / len(diffs)


def rfc3550_jitter(rtts: list[float]) -> float:
    """RFC 3550 style smoothed jitter - less reactive to single spikes."""
    if len(rtts) < 2:
        return float("nan")
    j = 0.0
    for i in range(1, len(rtts)):
        d = abs(rtts[i] - rtts[i - 1])
        j += (d - j) / 16.0
    return j


def loss_runs(flags: list[bool]) -> list[int]:
    """Lengths of consecutive-loss runs. In flags, True means success."""
    runs: list[int] = []
    cur = 0
    for ok in flags:
        if ok:
            if cur:
                runs.append(cur)
            cur = 0
        else:
            cur += 1
    if cur:
        runs.append(cur)
    return runs


# ---------------------------------------------------------------- metrics


@dataclass
class Metrics:
    n: int = 0
    n_ok: int = 0
    loss_pct: float = float("nan")
    mn: float = float("nan")
    p50: float = float("nan")
    p90: float = float("nan")
    p95: float = float("nan")
    p99: float = float("nan")
    mx: float = float("nan")
    mean: float = float("nan")
    jitter: float = float("nan")
    jitter_smooth: float = float("nan")
    spike: float = float("nan")
    burst_max: int = 0
    burst_runs: list[int] = field(default_factory=list)
    score: float = float("nan")

    @property
    def valid(self) -> bool:
        return self.n_ok >= 2


def compute(rtts: list[float], flags: list[bool],
            genre: str = "fps") -> Metrics:
    m = Metrics(n=len(flags), n_ok=len(rtts))
    if flags:
        m.loss_pct = 100.0 * (len(flags) - len(rtts)) / len(flags)
    runs = loss_runs(flags)
    m.burst_runs = runs
    m.burst_max = max(runs) if runs else 0
    if not rtts:
        m.score = 0.0
        return m
    m.mn = min(rtts)
    m.mx = max(rtts)
    m.mean = sum(rtts) / len(rtts)
    m.p50 = percentile(rtts, 50)
    m.p90 = percentile(rtts, 90)
    m.p95 = percentile(rtts, 95)
    m.p99 = percentile(rtts, 99)
    m.jitter = ipdv_jitter(rtts)
    m.jitter_smooth = rfc3550_jitter(rtts)
    m.spike = m.p99 - m.p50
    m.score = game_score(m, genre)
    return m


# ---------------------------------------------------------------- score

#: Weighting by genre. In competitive FPS, jitter and burst loss matter
#: more than average ping; in MOBA/MMO, where lag compensation is
#: stronger, it is the other way round.
GENRE_WEIGHTS = {
    "fps":  {"lat": 0.80, "jit": 1.40, "spk": 1.20, "loss": 1.15, "burst": 1.40},
    "moba": {"lat": 1.30, "jit": 0.85, "spk": 1.00, "loss": 1.00, "burst": 0.90},
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def game_score(m: Metrics, genre: str = "fps") -> float:
    """
    A 0-100 score for "how good is this route for gaming".
    It exists to rank services relative to each other, not as an
    absolute standard number.
    """
    w = GENRE_WEIGHTS.get(genre, GENRE_WEIGHTS["fps"])
    if math.isnan(m.p50):
        return 0.0

    lat_pen = _clamp((m.p50 - 20.0) / 2.0, 0, 45) * w["lat"]
    jit = 0.0 if math.isnan(m.jitter) else m.jitter
    jit_pen = _clamp(jit * 2.2, 0, 30) * w["jit"]
    spk = 0.0 if math.isnan(m.spike) else m.spike
    spk_pen = _clamp((spk - 10.0) / 3.0, 0, 20) * w["spk"]
    loss = 0.0 if math.isnan(m.loss_pct) else m.loss_pct
    loss_pen = _clamp(loss * 12.0, 0, 35) * w["loss"]
    burst_pen = _clamp((m.burst_max - 1) * 4.0, 0, 15) * w["burst"]

    return round(_clamp(100.0 - (lat_pen + jit_pen + spk_pen
                                 + loss_pen + burst_pen), 0, 100), 1)


def rate(metric: str, value: float) -> str:
    """Qualitative label: good | ok | bad | ?"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "?"
    table = {
        "p50":    (50, 90),
        "jitter": (5, 15),
        "spike":  (20, 50),
        "loss":   (0.1, 1.0),
        "burst":  (1, 3),
    }
    if metric not in table:
        return "?"
    good, ok = table[metric]
    if value <= good:
        return "good"
    if value <= ok:
        return "ok"
    return "bad"


# ---------------------------------------------------------------- inference


@dataclass
class Comparison:
    service: str
    baseline: str
    metric: str
    delta: float              # negative = better (for ping/jitter/loss)
    ci_low: float
    ci_high: float
    n_pairs: int
    significant: bool

    @property
    def verdict(self) -> str:
        if not self.significant:
            return "no significant difference"
        return "better" if self.delta < 0 else "worse"


def bootstrap_paired_diff(
    pairs: list[tuple[float, float]],
    n_boot: int = 4000,
    alpha: float = 0.05,
    seed: int = 1234,
) -> tuple[float, float, float]:
    """
    Bootstrap over paired differences.
    pairs: list of (service value, baseline value), one pair per round.
    Returns (mean difference, CI low, CI high).
    """
    diffs = [a - b for a, b in pairs
             if not (math.isnan(a) or math.isnan(b))]
    if len(diffs) < 2:
        return (float("nan"), float("nan"), float("nan"))

    point = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    n = len(diffs)
    means: list[float] = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += diffs[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = percentile(means, 100 * alpha / 2)
    hi = percentile(means, 100 * (1 - alpha / 2))
    return point, lo, hi


def compare_service(
    service: str,
    baseline: str,
    metric: str,
    per_round: dict[str, dict[int, float]],
) -> Comparison | None:
    """
    per_round: {service_name: {round_index: metric_value}}
    Only rounds where both services have data are used.
    """
    a = per_round.get(service) or {}
    b = per_round.get(baseline) or {}
    common = sorted(set(a) & set(b))
    pairs = [(a[r], b[r]) for r in common]
    if len(pairs) < 2:
        return None
    point, lo, hi = bootstrap_paired_diff(pairs)
    if math.isnan(point):
        return None
    significant = not (lo <= 0.0 <= hi)
    return Comparison(service, baseline, metric, point, lo, hi,
                      len(pairs), significant)
