"""
report.py - self-contained HTML report with statistical analysis

The unit of analysis is the ROUTE: each (game, region) pair. For every
route, each service is paired against the baseline FROM THE SAME ROUND,
and the difference is reported with a bootstrap 95% confidence interval.
If that interval spans zero, the verdict is "no significant difference" -
which is a real and useful result, not a failure.

Charts are inline SVG: no external dependencies, validated for both light
and dark mode, and always beside their matching table (the table doubles
as the table-view relief for the low-contrast light-mode hues).
"""

from __future__ import annotations

import csv
import html
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path

from .stats import compare_service, percentile

# ---------------------------------------------------------------- palette
# Categorical slots from the reference palette, validated:
#   light adjacent - CVD dE 9.1 / normal dE 19.6
#   dark  adjacent - CVD dE 8.4 / normal dE 19.3
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#008300", "#9085e9", "#e66767"]

METRIC_LABEL = {
    "p50": "median ping (ms)",
    "jitter": "jitter (ms)",
    "spike": "spike, p99 minus p50 (ms)",
    "loss_pct": "packet loss (%)",
    "score": "game score",
}
LOWER_IS_BETTER = {"p50": True, "jitter": True, "spike": True,
                   "loss_pct": True, "score": False}


# ---------------------------------------------------------------- loading


def load_blocks(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            for k in ("loss_pct", "min", "p50", "p90", "p95", "p99",
                      "max", "mean", "jitter", "jitter_smooth",
                      "spike", "score"):
                try:
                    r[k] = float(r[k])
                except (TypeError, ValueError):
                    r[k] = float("nan")
            for k in ("round", "n", "n_ok", "burst_max"):
                try:
                    r[k] = int(r[k])
                except (TypeError, ValueError):
                    r[k] = 0
            rows.append(r)
    return rows


def blocks_from_session(blocks) -> list[dict]:
    out = []
    for b in blocks:
        m = b.metrics
        out.append({
            "round": b.round_idx, "service": b.service, "game": b.game,
            "region": b.region, "genre": b.genre, "n": m.n, "n_ok": m.n_ok,
            "loss_pct": m.loss_pct, "min": m.mn, "p50": m.p50, "p90": m.p90,
            "p95": m.p95, "p99": m.p99, "max": m.mx, "mean": m.mean,
            "jitter": m.jitter, "jitter_smooth": m.jitter_smooth,
            "spike": m.spike, "burst_max": m.burst_max, "score": m.score,
        })
    return out


# ---------------------------------------------------------------- shaping


def routes(rows: list[dict]) -> list[tuple[str, str]]:
    return sorted({(r["game"], r["region"]) for r in rows})


def services_of(rows: list[dict]) -> list[str]:
    seen: list[str] = []
    for r in rows:
        if r["service"] not in seen:
            seen.append(r["service"])
    return seen


def per_round(rows: list[dict], game: str, region: str,
              metric: str) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for r in rows:
        if r["game"] == game and r["region"] == region:
            v = r[metric]
            if not (isinstance(v, float) and math.isnan(v)):
                out[r["service"]][r["round"]] = v
    return dict(out)


def summarise(rows: list[dict], game: str, region: str,
              service: str) -> dict:
    sel = [r for r in rows if r["game"] == game and r["region"] == region
           and r["service"] == service]
    if not sel:
        return {}

    def med(key: str) -> float:
        vals = [r[key] for r in sel
                if not (isinstance(r[key], float) and math.isnan(r[key]))]
        return statistics.median(vals) if vals else float("nan")

    return {
        "blocks": len(sel),
        "p50": med("p50"), "p95": med("p95"), "p99": med("p99"),
        "jitter": med("jitter"), "spike": med("spike"),
        "loss_pct": med("loss_pct"), "score": med("score"),
        "burst_max": max((r["burst_max"] for r in sel), default=0),
        "samples": sum(r["n"] for r in sel),
    }


# ---------------------------------------------------------------- svg bits


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return [0.0, 1.0]
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if raw <= step:
            break
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.5:
        ticks.append(round(v, 10))
        v += step
    # The axis must always cover the largest value, or a bar overshoots
    # the top of the plot and lands on the legend.
    while ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def grouped_bars(title: str, groups: list[str], series: list[str],
                 values: dict[tuple[str, str], float],
                 unit: str = "", lower_better: bool = True,
                 height: int = 260) -> str:
    """
    Grouped bar chart. x = region, colour = service.
    Every bar is directly labelled (the relief rule for the low-contrast
    light-mode hues).
    """
    # pad_t must leave room for the value label above the tallest bar.
    pad_l, pad_r, pad_t, pad_b = 58, 18, 30, 52
    # Fixed 700 width like the other charts: a narrow viewBox gets scaled
    # up to the container width and the bars look absurdly fat.
    width = max(700, 120 * max(len(groups), 1))
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    finite = [v for v in values.values() if math.isfinite(v)]
    if not finite:
        return f'<div class="chart-empty">No data for "{_esc(title)}"</div>'
    vmax = max(finite)
    ticks = _nice_ticks(0, vmax * 1.08, 4)
    top = ticks[-1] or 1.0

    def y(v: float) -> float:
        return pad_t + plot_h - (v / top) * plot_h

    gw = plot_w / max(len(groups), 1)
    bw = min(34.0, (gw - 16) / max(len(series), 1))

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_esc(title)}">'
    ]
    for t in ticks:
        yy = y(t)
        parts.append(
            f'<line class="grid" x1="{pad_l}" x2="{width - pad_r}" '
            f'y1="{yy:.1f}" y2="{yy:.1f}"/>'
            f'<text class="tick" x="{pad_l - 8}" y="{yy + 4:.1f}" '
            f'text-anchor="end">{t:g}</text>'
        )
    parts.append(
        f'<line class="axis" x1="{pad_l}" x2="{width - pad_r}" '
        f'y1="{pad_t + plot_h}" y2="{pad_t + plot_h}"/>'
    )

    for gi, g in enumerate(groups):
        cx = pad_l + gw * gi + gw / 2
        total = bw * len(series) + 2 * (len(series) - 1)
        x0 = cx - total / 2
        for si, s in enumerate(series):
            v = values.get((g, s), float("nan"))
            x = x0 + si * (bw + 2)          # 2px gap between adjacent bars
            if not math.isfinite(v):
                continue
            h = max(1.0, (v / top) * plot_h)
            yy = pad_t + plot_h - h
            tip = f"{s} - {g}: {v:.2f}{unit}"
            label = f"{v:.1f}" if v < 20 else f"{v:.0f}"
            parts.append(
                f'<g class="mark" tabindex="0" data-tip="{_esc(tip)}">'
                f'<rect x="{x:.1f}" y="{yy:.1f}" width="{bw:.1f}" '
                f'height="{h:.1f}" rx="4" fill="var(--s{si % 8 + 1})"/>'
                f'<text class="vlab" x="{x + bw / 2:.1f}" '
                f'y="{yy - 6:.1f}" text-anchor="middle">{label}</text>'
                f'</g>'
            )
        parts.append(
            f'<text class="glab" x="{cx:.1f}" y="{pad_t + plot_h + 18}" '
            f'text-anchor="middle">{_esc(g[:16])}</text>'
        )

    parts.append("</svg>")
    legend = "".join(
        f'<span class="lg"><i style="background:var(--s{i % 8 + 1})"></i>'
        f'{_esc(s)}</span>' for i, s in enumerate(series)
    )
    arrow = "lower is better" if lower_better else "higher is better"
    return (f'<figure class="fig"><figcaption>{_esc(title)} '
            f'<span class="hint">{arrow}</span></figcaption>'
            f'<div class="legend">{legend}</div>'
            f'{"".join(parts)}</figure>')


def delta_chart(title: str, entries: list[dict], unit: str = " ms",
                height_per_row: int = 34) -> str:
    """
    Effect chart: each service's difference from the baseline, with a 95%
    confidence interval. Status colour always travels with a text label,
    never on its own.
    """
    if not entries:
        return '<div class="chart-empty">Not enough data to compare</div>'

    pad_l, pad_r, pad_t, pad_b = 150, 150, 20, 34
    width = 760
    plot_w = width - pad_l - pad_r
    height = pad_t + pad_b + height_per_row * len(entries)
    plot_h = height - pad_t - pad_b

    bounds = []
    for e in entries:
        for k in ("lo", "hi", "delta"):
            if math.isfinite(e[k]):
                bounds.append(e[k])
    if not bounds:
        return '<div class="chart-empty">Not enough data to compare</div>'
    span = max(abs(min(bounds)), abs(max(bounds))) * 1.25 or 1.0
    ticks = _nice_ticks(-span, span, 4)
    lo_t, hi_t = ticks[0], ticks[-1]

    def x(v: float) -> float:
        return pad_l + (v - lo_t) / (hi_t - lo_t) * plot_w

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" '
             f'role="img" aria-label="{_esc(title)}">']
    for t in ticks:
        xx = x(t)
        parts.append(
            f'<line class="grid" x1="{xx:.1f}" x2="{xx:.1f}" '
            f'y1="{pad_t}" y2="{pad_t + plot_h}"/>'
            f'<text class="tick" x="{xx:.1f}" y="{height - 12}" '
            f'text-anchor="middle">{t:g}</text>'
        )
    x0 = x(0)
    parts.append(f'<line class="zero" x1="{x0:.1f}" x2="{x0:.1f}" '
                 f'y1="{pad_t}" y2="{pad_t + plot_h}"/>')

    for i, e in enumerate(entries):
        cy = pad_t + height_per_row * i + height_per_row / 2
        if e["sig"]:
            colour = "var(--good)" if e["delta"] < 0 else "var(--critical)"
            mark, word = ("▼", "better") if e["delta"] < 0 else ("▲", "worse")
        else:
            colour = "var(--muted)"
            mark, word = "●", "no change"
        tip = (f'{e["service"]}: {e["delta"]:+.2f}{unit} - {word} '
               f'(95% CI {e["lo"]:+.2f} to {e["hi"]:+.2f})')
        parts.append(
            f'<g class="mark" tabindex="0" data-tip="{_esc(tip)}">'
            f'<text class="rlab" x="{pad_l - 12}" y="{cy + 4:.1f}" '
            f'text-anchor="end">{_esc(e["service"][:20])}</text>'
        )
        if math.isfinite(e["lo"]) and math.isfinite(e["hi"]):
            parts.append(
                f'<line x1="{x(e["lo"]):.1f}" x2="{x(e["hi"]):.1f}" '
                f'y1="{cy:.1f}" y2="{cy:.1f}" stroke="{colour}" '
                f'stroke-width="2" stroke-linecap="round" opacity="0.55"/>'
            )
        parts.append(
            f'<circle cx="{x(e["delta"]):.1f}" cy="{cy:.1f}" r="5.5" '
            f'fill="{colour}" stroke="var(--surface)" stroke-width="2"/>'
            f'<text class="dlab" x="{width - pad_r + 10}" y="{cy + 4:.1f}" '
            f'fill="{colour}">{mark} {e["delta"]:+.1f} {word}</text>'
            f'</g>'
        )
    parts.append("</svg>")
    return (f'<figure class="fig"><figcaption>{_esc(title)} '
            f'<span class="hint">left of zero = better than baseline, '
            f'bar = 95% confidence interval</span></figcaption>'
            f'{"".join(parts)}</figure>')


def round_lines(title: str, series: dict[str, dict[int, float]],
                unit: str = " ms", height: int = 240) -> str:
    """Metric trend across rounds - shows what the interleaving saw."""
    if not series:
        return ""
    pad_l, pad_r, pad_t, pad_b = 58, 90, 16, 40
    width, plot_h = 760, height - pad_t - pad_b
    plot_w = width - pad_l - pad_r

    rounds = sorted({r for d in series.values() for r in d})
    vals = [v for d in series.values() for v in d.values()
            if math.isfinite(v)]
    if not rounds or not vals:
        return ""
    ticks = _nice_ticks(min(vals) * 0.92, max(vals) * 1.08, 4)
    lo_t, hi_t = ticks[0], ticks[-1]

    def x(r: int) -> float:
        if len(rounds) == 1:
            return pad_l + plot_w / 2
        return pad_l + (rounds.index(r) / (len(rounds) - 1)) * plot_w

    def y(v: float) -> float:
        return pad_t + plot_h - (v - lo_t) / (hi_t - lo_t) * plot_h

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" '
             f'role="img" aria-label="{_esc(title)}">']
    for t in ticks:
        yy = y(t)
        parts.append(
            f'<line class="grid" x1="{pad_l}" x2="{width - pad_r}" '
            f'y1="{yy:.1f}" y2="{yy:.1f}"/>'
            f'<text class="tick" x="{pad_l - 8}" y="{yy + 4:.1f}" '
            f'text-anchor="end">{t:g}</text>')
    for r in rounds:
        parts.append(f'<text class="glab" x="{x(r):.1f}" '
                     f'y="{pad_t + plot_h + 18}" text-anchor="middle">'
                     f'R{r}</text>')

    for si, (name, d) in enumerate(series.items()):
        pts = [(x(r), y(d[r])) for r in rounds
               if r in d and math.isfinite(d[r])]
        if not pts:
            continue
        col = f"var(--s{si % 8 + 1})"
        path = " ".join(("M" if i == 0 else "L") + f"{px:.1f},{py:.1f}"
                        for i, (px, py) in enumerate(pts))
        parts.append(f'<path d="{path}" fill="none" stroke="{col}" '
                     f'stroke-width="2" stroke-linejoin="round"/>')
        for (px, py), r in zip(pts, [r for r in rounds if r in d]):
            parts.append(
                f'<g class="mark" tabindex="0" '
                f'data-tip="{_esc(f"{name} - round {r}: {d[r]:.2f}{unit}")}">'
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{col}" '
                f'stroke="var(--surface)" stroke-width="2"/></g>')
        lx, ly = pts[-1]
        parts.append(f'<text class="slab" x="{lx + 8:.1f}" y="{ly + 4:.1f}" '
                     f'fill="{col}">{_esc(name[:12])}</text>')
    parts.append("</svg>")
    legend = "".join(
        f'<span class="lg"><i style="background:var(--s{i % 8 + 1})"></i>'
        f'{_esc(s)}</span>' for i, s in enumerate(series)
    )
    return (f'<figure class="fig"><figcaption>{_esc(title)}</figcaption>'
            f'<div class="legend">{legend}</div>{"".join(parts)}</figure>')


# ---------------------------------------------------------------- css/js

CSS = """
:root{
  color-scheme:light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --border:rgba(11,11,11,.10);
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
  --s5:#e87ba4; --s6:#008300; --s7:#4a3aa7; --s8:#e34948;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835;
    --border:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
    --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --border:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
  --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto;padding:32px 16px 72px}
h1{font-size:1.7rem;margin:0 0 4px}
h2{font-size:1.2rem;margin:40px 0 4px;padding-top:20px;
  border-top:1px solid var(--border)}
h3{font-size:1rem;margin:24px 0 8px;color:var(--ink2)}
p{color:var(--ink2);margin:6px 0 14px}
.sub{color:var(--muted);font-size:.86rem}
.cards{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin:20px 0}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:14px 16px}
.card .k{font-size:.76rem;color:var(--muted);letter-spacing:.02em}
.card .v{font-size:1.5rem;font-weight:650;margin-top:2px}
.panel{background:var(--surface);border:1px solid var(--border);
  border-radius:14px;padding:18px;margin:16px 0}
table{width:100%;border-collapse:collapse;font-size:.88rem;
  font-variant-numeric:tabular-nums;margin:10px 0}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid var(--border)}
td:not(.name){text-align:right}
th{color:var(--muted);font-weight:600;font-size:.78rem;
  text-transform:uppercase;letter-spacing:.04em}
th:not(:first-child){text-align:right}
tbody tr:hover{background:color-mix(in oklab,var(--ink) 4%,transparent)}
table.txt th,table.txt td{text-align:left}
td.name{font-weight:600}
.swatch{display:inline-block;width:10px;height:10px;border-radius:3px;
  margin-right:7px;vertical-align:middle}
.good{color:var(--good);font-weight:650}
.bad{color:var(--critical);font-weight:650}
.warn{color:var(--serious);font-weight:650}
.flat{color:var(--muted)}
.fig{margin:18px 0 6px}
figcaption{font-size:.9rem;font-weight:600;margin-bottom:8px}
.hint{font-weight:400;color:var(--muted);font-size:.8rem}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:8px}
.lg{font-size:.8rem;color:var(--ink2);display:flex;align-items:center;gap:6px}
.lg i{width:10px;height:10px;border-radius:3px;display:inline-block}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--grid);stroke-width:1}
.axis{stroke:var(--axis);stroke-width:1}
.zero{stroke:var(--axis);stroke-width:1.5;stroke-dasharray:4 3}
.tick,.glab{fill:var(--muted);font-size:11px;
  font-family:system-ui,sans-serif;font-variant-numeric:tabular-nums}
.vlab{fill:var(--ink2);font-size:10px;font-family:system-ui,sans-serif;
  font-variant-numeric:tabular-nums}
.rlab{fill:var(--ink);font-size:12px;font-family:system-ui,sans-serif}
.dlab,.slab{font-size:11.5px;font-family:system-ui,sans-serif;font-weight:600}
.mark{cursor:default}
.mark:hover rect,.mark:focus rect{opacity:.82}
.mark:focus{outline:none}
.chart-empty{color:var(--muted);font-size:.86rem;padding:18px;
  border:1px dashed var(--border);border-radius:10px;text-align:center}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .1s;
  background:var(--ink);color:var(--page);padding:6px 10px;
  border-radius:7px;font-size:.78rem;z-index:99;max-width:300px;
  font-variant-numeric:tabular-nums}
.note{border-left:3px solid var(--warning);padding:10px 14px;
  background:color-mix(in oklab,var(--warning) 8%,transparent);
  border-radius:8px;margin:14px 0;font-size:.88rem;color:var(--ink2)}
.note b{color:var(--ink)}
.foot{margin-top:40px;color:var(--muted);font-size:.8rem}
"""

JS = """
(function(){
  var tip=document.getElementById('tip');
  function show(e){
    var t=e.currentTarget.getAttribute('data-tip'); if(!t) return;
    tip.textContent=t; tip.style.opacity='1';
    var r=e.currentTarget.getBoundingClientRect();
    var x=(e.clientX||r.left+r.width/2), y=(e.clientY||r.top);
    tip.style.left=Math.min(x+14,innerWidth-tip.offsetWidth-10)+'px';
    tip.style.top=Math.max(y-tip.offsetHeight-10,8)+'px';
  }
  function hide(){tip.style.opacity='0';}
  document.querySelectorAll('.mark').forEach(function(m){
    m.addEventListener('mousemove',show);
    m.addEventListener('mouseleave',hide);
    m.addEventListener('focus',show);
    m.addEventListener('blur',hide);
  });
})();
"""


# ---------------------------------------------------------------- builder


def build_report(rows: list[dict], baseline: str,
                 meta: dict | None = None) -> str:
    meta = meta or {}
    svcs = services_of(rows)
    others = [s for s in svcs if s != baseline]
    all_routes = routes(rows)
    games = sorted({g for g, _ in all_routes})

    n_samples = sum(r["n"] for r in rows)
    n_blocks = len(rows)
    n_rounds = len({r["round"] for r in rows})

    body: list[str] = []
    body.append(f"""
<h1>Ping-reduction service test report</h1>
<div class="sub">Generated {time.strftime('%Y-%m-%d %H:%M')} &middot;
baseline: <b>{_esc(baseline)}</b> &middot;
method: interleaved A/B, paired within each round</div>
<div class="cards">
  <div class="card"><div class="k">services</div><div class="v">{len(svcs)}</div></div>
  <div class="card"><div class="k">rounds</div><div class="v">{n_rounds}</div></div>
  <div class="card"><div class="k">blocks</div><div class="v">{n_blocks}</div></div>
  <div class="card"><div class="k">samples</div><div class="v">{n_samples:,}</div></div>
  <div class="card"><div class="k">routes</div><div class="v">{len(all_routes)}</div></div>
</div>""")

    # ---------------------------------------------------- verdict
    verdicts: list[str] = []
    for game in games:
        game_routes = [(g, rg) for g, rg in all_routes if g == game]
        # The primary route is the region with the best baseline score -
        # that is, the one you would actually be playing on.
        best_route, best_score = None, -1.0
        for g, rg in game_routes:
            s = summarise(rows, g, rg, baseline)
            sc = s.get("score", float("nan"))
            if math.isfinite(sc) and sc > best_score:
                best_route, best_score = (g, rg), sc
        if best_route is None:
            best_route = game_routes[0]
        g, rg = best_route

        pr50 = per_round(rows, g, rg, "p50")
        prj = per_round(rows, g, rg, "jitter")
        winner, winner_txt = None, "no significant difference from baseline"
        best_gain = 0.0
        for s in others:
            c50 = compare_service(s, baseline, "p50", pr50)
            cj = compare_service(s, baseline, "jitter", prj)
            gain = 0.0
            if c50 and c50.significant and c50.delta < 0:
                gain += -c50.delta
            if cj and cj.significant and cj.delta < 0:
                gain += -cj.delta * 2.0      # jitter counts double
            if gain > best_gain:
                best_gain, winner = gain, s
        if winner:
            winner_txt = f"<span class='good'>{_esc(winner)}</span> wins"
        verdicts.append(
            f'<tr><td class="name">{_esc(g)}</td>'
            f'<td class="name">{_esc(rg)}</td>'
            f'<td class="name">{winner_txt}</td></tr>'
        )

    body.append(f"""
<h2>Verdict</h2>
<p>For each game, the primary route is the region with the best baseline
score - the one you would actually be playing on. "No significant
difference" is a real result: it means the service is not worth its price
on that route.</p>
<div class="panel"><table class="txt">
<thead><tr><th>Game</th><th>Primary route</th><th>Result</th></tr></thead>
<tbody>{''.join(verdicts)}</tbody></table></div>""")

    # ---------------------------------------------------- per game
    for game in games:
        game_routes = [rg for g, rg in all_routes if g == game]
        body.append(f"<h2>{_esc(game)}</h2>")

        # Full table - both the data and the table-view relief
        head = ("<tr><th>Route</th><th>Service</th><th>p50</th><th>p95</th>"
                "<th>jitter</th><th>spike</th><th>loss</th><th>burst</th>"
                "<th>score</th><th>blocks</th></tr>")
        trs: list[str] = []
        for rg in game_routes:
            for s in svcs:
                d = summarise(rows, game, rg, s)
                if not d:
                    continue
                sc = d["score"]
                cls = "good" if sc >= 75 else ("warn" if sc >= 50 else "bad")
                trs.append(
                    f'<tr><td class="name">{_esc(rg)}</td>'
                    f'<td class="name"><span class="swatch" style="background:'
                    f'var(--s{svcs.index(s) % 8 + 1})"></span>{_esc(s)}</td>'
                    f'<td>{d["p50"]:.1f}</td><td>{d["p95"]:.1f}</td>'
                    f'<td>{d["jitter"]:.2f}</td><td>{d["spike"]:.1f}</td>'
                    f'<td>{d["loss_pct"]:.2f}%</td><td>{d["burst_max"]}</td>'
                    f'<td class="{cls}">{sc:.0f}</td>'
                    f'<td class="sub">{d["blocks"]}</td></tr>'
                )
        body.append(f'<div class="panel"><table><thead>{head}</thead>'
                    f'<tbody>{"".join(trs)}</tbody></table></div>')

        # Bar charts: ping and jitter
        for metric, unit, lower in (("p50", " ms", True),
                                    ("jitter", " ms", True)):
            vals = {}
            for rg in game_routes:
                for s in svcs:
                    d = summarise(rows, game, rg, s)
                    if d:
                        vals[(rg, s)] = d[metric]
            body.append('<div class="panel">' + grouped_bars(
                f"{METRIC_LABEL[metric]} - {game}", game_routes, svcs,
                vals, unit, lower) + "</div>")

        # Effect chart and trend, per route
        for rg in game_routes:
            pr50 = per_round(rows, game, rg, "p50")
            prj = per_round(rows, game, rg, "jitter")
            for metric, pr, unit in (("p50", pr50, " ms"),
                                     ("jitter", prj, " ms")):
                entries = []
                for s in others:
                    c = compare_service(s, baseline, metric, pr)
                    if c is None:
                        continue
                    entries.append({"service": s, "delta": c.delta,
                                    "lo": c.ci_low, "hi": c.ci_high,
                                    "sig": c.significant,
                                    "n": c.n_pairs})
                if entries:
                    body.append('<div class="panel">' + delta_chart(
                        f"Effect on {METRIC_LABEL[metric]} - "
                        f"{game} / {rg} (vs {baseline})",
                        entries, unit) + "</div>")
            if pr50:
                body.append('<div class="panel">' + round_lines(
                    f"Median ping across rounds - {game} / {rg}",
                    pr50) + "</div>")

    # ---------------------------------------------------- caveats
    body.append("""
<h2>What these numbers cannot tell you</h2>
<div class="note">
<b>1.</b> A TCP/ICMP probe is not literally game traffic. Some servers
drop or rate-limit it, so the figure can be slightly optimistic or
pessimistic.<br>
<b>2.</b> If your ping-reduction service uses split tunnelling and only
routes the game process, this tool may never have gone through the tunnel
at all. Verify once with full-tunnel mode, or by adding the Python
executable to the service's process list.<br>
<b>3.</b> These numbers are valid for <i>your line, at these hours</i>
only. A full verdict needs one run at peak and one off-peak.<br>
<b>4.</b> Server tickrate and lag compensation are not measured - what you
feel in game is not only the network.<br>
<b>5.</b> The in-game ping counter is the ground truth. After reading this
report, play one match with the winner and one without, and compare.
</div>
<p class="foot">Built with pingmon &middot; analysis: paired bootstrap
(4000 resamples, 95% confidence interval) over block medians &middot;
colour palette validated for colour-vision deficiency in both light and
dark mode.</p>""")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pingmon report</title><style>{CSS}</style></head>
<body><div id="tip"></div><div class="wrap">{''.join(body)}</div>
<script>{JS}</script></body></html>"""


def write_report(rows: list[dict], baseline: str, out: Path,
                 meta: dict | None = None) -> Path:
    out.write_text(build_report(rows, baseline, meta), encoding="utf-8")
    return out
