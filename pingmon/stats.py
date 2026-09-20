"""
stats.py — محاسبه‌ی متریک‌ها و تحلیل آماری

متریک‌هایی که واقعاً برای گیمینگ اهمیت دارند:
  p50            میانه‌ی RTT — «پینگ معمولت»
  p95 / p99      دُم توزیع — اسپایک‌هایی که حس می‌کنی
  jitter (IPDV)  میانگین |اختلاف دو پینگ پشت‌سرهم|
  spike          p99 − p50، ارتفاع پرش‌ها
  loss           درصد بسته‌ی گم‌شده
  burst          طولانی‌ترین رشته‌ی گم‌شدن پشت‌سرهم ← فریز واقعی

تحلیل: واحد تحلیل «بلوک» است نه «نمونه»، چون نمونه‌های داخل یک بلوک
به‌شدت همبسته‌اند. مقایسه هم جفت‌شده در هر راند انجام می‌شود تا drift
شبکه خنثی شود.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


# ---------------------------------------------------------------- helpers


def percentile(values: list[float], q: float) -> float:
    """صدک با درون‌یابی خطی. q بین ۰ و ۱۰۰."""
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
    """میانگین قدرمطلق اختلاف پینگ‌های متوالی (IPDV)."""
    if len(rtts) < 2:
        return float("nan")
    diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
    return sum(diffs) / len(diffs)


def rfc3550_jitter(rtts: list[float]) -> float:
    """جیتر هموارشده به سبک RFC 3550 — به اسپایک‌های تکی کمتر واکنش می‌دهد."""
    if len(rtts) < 2:
        return float("nan")
    j = 0.0
    for i in range(1, len(rtts)):
        d = abs(rtts[i] - rtts[i - 1])
        j += (d - j) / 16.0
    return j


def loss_runs(flags: list[bool]) -> list[int]:
    """طول رشته‌های گم‌شدن پشت‌سرهم. flags: True یعنی موفق."""
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

#: وزن‌دهی بر اساس ژانر. در FPS رقابتی، jitter و burst-loss از میانگین
#: پینگ مهم‌ترند؛ در MOBA/MMO که lag compensation قوی‌تر است، برعکس.
GENRE_WEIGHTS = {
    "fps":  {"lat": 0.80, "jit": 1.40, "spk": 1.20, "loss": 1.15, "burst": 1.40},
    "moba": {"lat": 1.30, "jit": 0.85, "spk": 1.00, "loss": 1.00, "burst": 0.90},
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def game_score(m: Metrics, genre: str = "fps") -> float:
    """
    امتیاز ۰ تا ۱۰۰ برای «چقدر این مسیر برای بازی خوب است».
    صرفاً برای رتبه‌بندی نسبی سرویس‌هاست، نه یک عدد مطلق استاندارد.
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
    """برچسب کیفی: good | ok | bad | ?"""
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
    delta: float              # منفی = بهتر (برای پینگ/جیتر/لاس)
    ci_low: float
    ci_high: float
    n_pairs: int
    significant: bool

    @property
    def verdict(self) -> str:
        if not self.significant:
            return "بدون تفاوت معنی‌دار"
        return "بهتر" if self.delta < 0 else "بدتر"


def bootstrap_paired_diff(
    pairs: list[tuple[float, float]],
    n_boot: int = 4000,
    alpha: float = 0.05,
    seed: int = 1234,
) -> tuple[float, float, float]:
    """
    بوت‌استرپ روی اختلاف‌های جفت‌شده.
    pairs: لیست (مقدار سرویس، مقدار خط‌پایه) — هر جفت از یک راند.
    خروجی: (میانگین اختلاف، کران پایین، کران بالای بازه اطمینان)
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
    فقط راندهایی که هر دو سرویس در آن‌ها داده دارند استفاده می‌شوند.
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
