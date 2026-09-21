# pingmon

Monitor and test ping-reduction services — **game by game, service by service**.

It measures what each service actually does to each game, then uses statistics
to say whether that effect is real or just network noise.

---

## Why this and not a website

Public ping-test sites measure your RTT **to their own server**, not to the
game server. Worse: most ping-reduction services use split tunnelling and only
route the game process through the tunnel — so your browser traffic never
enters it at all, and that number measures nothing.

This tool probes the **real region each game runs in**, from **your own line**,
and compares services **interleaved** so network drift cannot leak into the
result.

---

## Install

Requires Python 3.10+.

```bash
pip install -r requirements.txt      # rich + pyyaml
```

Works identically on Windows and Linux. The default probe is TCP and needs
**no admin rights**.

### Windows one-click

Double-click `1-VALIDATE.bat`. It finds Python, builds a virtualenv, installs
the dependencies, creates the config and runs a health check — writing
everything to `last-run.txt`. Then:

```
2-MONITOR.bat    live dashboard
3-RUN-TEST.bat   full A/B test + HTML report
4-SDR-CS2.bat    fetch Valve relay IPs for CS2
```

---

## Quick start

```bash
python -m pingmon init          # writes pingmon.yaml
python -m pingmon validate      # which targets actually respond?
python -m pingmon monitor       # watch only - live dashboard
python -m pingmon run           # the full A/B test
```

Before `run`, open `pingmon.yaml` and list the services you want to compare.
Exactly one must be marked `baseline: true`.

---

## Commands

| Command | What it does |
|---|---|
| `init` | Write an example config |
| `validate` | Check DNS and reachability for every target |
| `monitor` | Live dashboard, no test — for watching the current state |
| `run` | Full interleaved A/B test + HTML report |
| `discover` | Capture a game's real server IP (run mid-match) |
| `sdr-refresh` | Fetch the live Valve SDR relay list for CS2 |
| `report DIR` | Rebuild the HTML report from CSV |

---

## Method — why the result is trustworthy

### Interleaved, not sequential

Testing service A for 10 minutes and then B for 10 minutes measures **network
drift**, not the services. Instead, short blocks rotate:

```
round 1:  baseline -> ExitLag -> NoPing
round 2:  NoPing -> baseline -> ExitLag       <- order randomised
round 3:  ExitLag -> NoPing -> baseline
...
```

Each service is paired against the baseline **from the same round**, so drift
between hours cancels out.

### Settle phase

After every service switch, the first 8 seconds are discarded so the transient
of the tunnel coming up never enters the measurement.

### Statistics

The unit of analysis is the **block**, not the sample — samples inside a block
are heavily correlated. Paired differences go through a bootstrap (4000
resamples) with a 95% confidence interval. If that interval spans zero, the
verdict is **"no significant difference"**.

> That verdict is not a failure — it is a valid result. It means the service is
> not worth its price on that route.

---

## Metrics

| Metric | Meaning | Excellent | Acceptable | Bad |
|---|---|---|---|---|
| `p50` | median RTT, "your normal ping" | < 50 ms | 50–90 | > 120 |
| `jitter` | mean difference between consecutive pings (IPDV) | < 5 ms | 5–15 | > 20 |
| `spike` | `p99 − p50`, how tall the jumps are | < 20 ms | 20–50 | > 80 |
| `loss` | percentage of lost packets | < 0.1% | 0.1–1% | > 2% |
| `brst` | longest run of consecutive losses | ≤ 1 | 2–3 | ≥ 5 |

**Burst loss is the metric most tools never measure.** 1% loss scattered evenly
is barely noticeable; the same 1% arriving as runs of five is a visible freeze
every time.

### Game score

A 0–100 number combining all metrics, weighted by genre:

- `genre: fps` — jitter and burst weigh more (a steady 70 ms beats a jumpy 45)
- `genre: moba` — average ping weighs more (lag compensation is stronger)

It exists to **rank services against each other**, not as an absolute standard.

---

## Targets by game

| Game | Hosting | Status | Notes |
|---|---|---|---|
| **Rainbow Six Siege** | Microsoft Azure | Ready | R6's region names are literally Azure region names |
| **Apex Legends** | Multiplay/i3D + AWS | Ready | The game also shows a datacenter list with live pings |
| **CS2** | Steam Datagram Relay | Run `sdr-refresh` | Fetch Valve's relay IPs into your config |
| **CoD Warzone** | Undocumented | Approximate | Use `discover` mid-match for accuracy |
| **EA FC 26** | Mode-dependent | FUT only | See below |

### EA FC 26 warning

FC 26 has two completely different connection types:

| Mode | Connection | Can a ping service help? |
|---|---|---|
| Ultimate Team (Rivals, Champs, Draft), Clubs, Rush, VOLTA | Dedicated server | Yes |
| Online Seasons, Friendlies, Co-op Seasons | **Peer-to-peer** | Essentially no |

In P2P modes there is no server in the middle whose route could be optimised —
traffic goes straight to your opponent's house. If you mostly play Seasons,
testing is pointless.

---

## Mode B — the real server IP

The default (probing the region) is roughly 80% accurate and plenty for a
buy-or-not decision. If the result is ambiguous (under 10 ms apart), go get the
real IP:

```bash
# mid-match, in another terminal:
python -m pingmon discover --game warzone --seconds 30
```

It reads the OS connection table (`netstat -ano` on Windows, `ss -tunp` on
Linux), ranks the stable UDP destinations, and prints a config line ready to
paste.

Entirely passive — nothing is hooked or injected, so it is safe with anti-cheat.

---

## Output

Each run creates a folder:

```
pingmon-runs/run-20260920-143022/
├── samples.csv    every single probe — for your own analysis
├── blocks.csv     per-block summary — the report's input
└── report.html    the final report
```

The report is self-contained (no external dependencies), has a dark mode, and
its colour palette is validated for colour-vision deficiency.

---

## What these numbers cannot tell you

1. **A TCP/ICMP probe is not literally game traffic.** Some servers drop or
   rate-limit it.

2. **Split tunnelling — the big one.** If a service only routes the game
   process, this tool may never go through the tunnel and would therefore
   measure nothing. Fix: use full-tunnel mode, or add the Python executable to
   the service's process list. Verify this once — otherwise the whole run is
   worthless.

3. **Time of day matters.** The numbers are valid for your line at those hours
   only. Do one run at peak and one off-peak.

4. **Tickrate and lag compensation are not measured.** What you feel in game is
   not only the network.

5. **The in-game ping counter is the ground truth.** After the report, play one
   match with the winner and one without, and compare. If they agree, trust the
   result.

---

## Tuning run length

The default is 6 rounds × 3 services × 60 s ≈ 25 minutes, excluding manual
pauses.

- **Do not go below 4 rounds** — the confidence interval gets so wide that
  nothing comes out significant.
- For a solid result: 8–10 rounds.
- Fewer targets means cleaner data. Enable only the region you actually play on.

```bash
python -m pingmon run --rounds 10 --block 90
```

---

## Layout

```
pingmon/
├── targets.py     region catalog + live Valve SDR fetch
├── probe.py       fixed-rate TCP / ICMP / A2S probing
├── stats.py       metrics, score, paired bootstrap
├── session.py     the A/B scheduler and CSV logging
├── dashboard.py   live terminal dashboard
├── report.py      HTML report + inline SVG charts
├── discover.py    game server IP capture
└── cli.py         entry point
```
