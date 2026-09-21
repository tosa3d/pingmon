"""
cli.py - entry point

  pingmon init         write an example config file
  pingmon validate     check which targets actually respond
  pingmon sdr-refresh  fetch the live Valve SDR relay list for CS2
  pingmon monitor      live monitoring only, no A/B test
  pingmon run          the full interleaved A/B test
  pingmon discover     capture a game's real server IP (run mid-match)
  pingmon report DIR   rebuild the HTML report from CSV
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .probe import probe_tcp
from .report import blocks_from_session, load_blocks, write_report
from .session import Service, Session, SessionConfig
from .targets import (Target, build_default_targets, fetch_sdr_pops,
                      targets_from_config)

DEFAULT_CONFIG = Path("pingmon.yaml")


# ---------------------------------------------------------------- config


def load_config(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML is not installed:  pip install pyyaml rich")
    if not path.exists():
        sys.exit(f"Config not found: {path}\nRun this first:  pingmon init")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def services_from_config(cfg: dict) -> list[Service]:
    out: list[Service] = []
    for s in cfg.get("services") or []:
        out.append(Service(
            name=s["name"],
            mode=s.get("mode", "manual"),
            instruction=s.get("instruction", ""),
            up=s.get("up", ""),
            down=s.get("down", ""),
            is_baseline=bool(s.get("baseline", False)),
        ))
    if not out:
        sys.exit("No services defined in the config.")
    if not any(s.is_baseline for s in out):
        out[0].is_baseline = True
    return out


def session_config(cfg: dict, args) -> SessionConfig:
    r = cfg.get("run") or {}
    return SessionConfig(
        block_seconds=int(getattr(args, "block", None) or
                          r.get("block_seconds", 60)),
        settle_seconds=int(r.get("settle_seconds", 8)),
        rounds=int(getattr(args, "rounds", None) or r.get("rounds", 6)),
        rate_hz=float(r.get("rate_hz", 5.0)),
        timeout=float(r.get("timeout", 2.0)),
        randomize_order=bool(r.get("randomize_order", True)),
        outdir=Path(r.get("outdir", "./pingmon-runs")),
    )


# ---------------------------------------------------------------- commands


def cmd_init(args) -> None:
    src = Path(__file__).parent.parent / "config.example.yaml"
    dst = Path(args.path or DEFAULT_CONFIG)
    if dst.exists() and not args.force:
        sys.exit(f"{dst} already exists. Use --force to overwrite.")
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Created: {dst}\nOpen it, add your services, then run:"
          f"\n  pingmon validate\n  pingmon run")


def cmd_validate(args) -> None:
    cfg = load_config(Path(args.config))
    targets = targets_from_config(cfg) or build_default_targets()
    print(f"{'GAME':<9} {'REGION':<26} {'HOST':<38} {'IP':<16} {'RTT':>9}")
    print("-" * 101)
    ok = bad = 0
    for t in targets:
        ip = t.resolve()
        if not ip:
            print(f"{t.game:<9} {t.region[:25]:<26} {t.host[:37]:<38} "
                  f"{'-':<16} {'DNS FAIL':>9}")
            bad += 1
            continue
        best = None
        for _ in range(3):
            s = probe_tcp(ip, t.port, 2.0)
            if s.ok and s.rtt_ms is not None:
                best = s.rtt_ms if best is None else min(best, s.rtt_ms)
            time.sleep(0.12)
        if best is None:
            print(f"{t.game:<9} {t.region[:25]:<26} {t.host[:37]:<38} "
                  f"{ip:<16} {'NO REPLY':>9}")
            bad += 1
        else:
            print(f"{t.game:<9} {t.region[:25]:<26} {t.host[:37]:<38} "
                  f"{ip:<16} {best:>7.1f}ms")
            ok += 1
    print("-" * 101)
    print(f"healthy: {ok}   broken: {bad}")
    if bad:
        print("Remove the broken targets from your config, "
              "or replace them with a manual IP.")


def cmd_sdr_refresh(args) -> None:
    print("Fetching the Steam Datagram Relay config from Valve...")
    try:
        pops = fetch_sdr_pops()
    except Exception as e:                     # noqa: BLE001
        sys.exit(f"Failed: {e}\n"
                 "If your network blocks it, put a relay IP in the config "
                 "by hand.")
    print(f"Found {len(pops)} PoPs.\n")
    print("Paste this under the cs2 game in your config:\n")
    print("    targets:")
    for code, ips in sorted(pops.items()):
        print(f"      - {{ region: \"{code}\", ip: \"{ips[0]}\", "
              f"port: 27015, note: \"Valve SDR\" }}")


def cmd_discover(args) -> None:
    from .discover import collect, rank
    print(f"Get into a live match. Sampling for {args.seconds} seconds...")

    def tick(i, n, found):
        print(f"  [{i}/{n}] unique destinations: {found}", end="\r",
              flush=True)

    eps = rank(collect(args.seconds, 2.0, args.game, tick))
    print(" " * 60, end="\r")
    if not eps:
        print("Nothing found. Make sure you were mid-match and the game "
              "is running.")
        if args.game:
            print("If the process name differs, drop --game to see "
                  "everything.")
        return
    print(f"\n{'PROTO':<6} {'IP':<17} {'PORT':>6} {'SEEN':>5}  PROCESS")
    print("-" * 66)
    for e in eps[:20]:
        print(f"{e.proto:<6} {e.ip:<17} {e.port:>6} {e.count:>5}  {e.process}")
    print("-" * 66)
    top = eps[0]
    print("\nMost likely game server - put this in your config:\n")
    print(f"      - {{ region: \"discovered\", ip: \"{top.ip}\", "
          f"port: {top.port}, note: \"captured mid-match\" }}")


def _build(cfg: dict) -> list[Target]:
    targets = targets_from_config(cfg)
    if not targets:
        print("[!] Config has no targets - using the built-in defaults.")
        targets = build_default_targets()
    return targets


def cmd_monitor(args) -> None:
    """Watch only: no blocks, no services, just live metrics."""
    from .dashboard import Dashboard
    cfg = load_config(Path(args.config))
    targets = _build(cfg)
    sess = Session(targets, [Service("live", mode="none")],
                   session_config(cfg, args))
    sess.prepare()
    if not sess.targets:
        sys.exit("No healthy targets.")
    sess.start_probes()
    dash = Dashboard(lambda: sess)
    dash.service = "live monitor"
    dash.phase = "measure"
    dash.phase_total = 1.0
    dash.log("monitor mode - press Ctrl+C to stop")
    try:
        with dash:
            while True:
                dash.remaining = 0.0
                dash.tick(0.0, True)
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        sess.stop_probes()
    print("\nDone.")


def cmd_run(args) -> None:
    cfg = load_config(Path(args.config))
    targets = _build(cfg)
    services = services_from_config(cfg)
    scfg = session_config(cfg, args)
    baseline = next(s.name for s in services if s.is_baseline)

    total = scfg.rounds * len(services) * (scfg.block_seconds
                                           + scfg.settle_seconds)
    print(f"\npingmon {__version__}")
    print(f"  targets  : {len(targets)}")
    print(f"  services : {', '.join(s.name for s in services)}")
    print(f"  baseline : {baseline}")
    print(f"  structure: {scfg.rounds} rounds x {len(services)} blocks "
          f"x {scfg.block_seconds}s")
    print(f"  duration : about {total // 60} minutes "
          f"(excluding manual pauses)\n")

    if args.dashboard:
        from .dashboard import Dashboard
        sess_box: dict = {}
        dash = Dashboard(lambda: sess_box["s"])
        sess = Session(targets, services, scfg, hooks=dash)
        sess_box["s"] = sess
        try:
            with dash:
                blocks = sess.run()
        except KeyboardInterrupt:
            sess.abort()
            blocks = sess.store.blocks
            print("\nStopped - building a report from what we have.")
    else:
        sess = Session(targets, services, scfg)
        try:
            blocks = sess.run()
        except KeyboardInterrupt:
            sess.abort()
            blocks = sess.store.blocks

    if not blocks:
        print("No data was collected.")
        return
    rows = blocks_from_session(blocks)
    out = write_report(rows, baseline, sess.run_dir / "report.html")
    print(f"\nOutput in: {sess.run_dir}")
    print(f"  samples.csv  raw data, every probe")
    print(f"  blocks.csv   per-block summary")
    print(f"  report.html  the final report  ->  {out}")


def cmd_report(args) -> None:
    d = Path(args.dir)
    blocks_csv = d / "blocks.csv" if d.is_dir() else d
    if not blocks_csv.exists():
        sys.exit(f"Not found: {blocks_csv}")
    rows = load_blocks(blocks_csv)
    if not rows:
        sys.exit("The file is empty.")
    baseline = args.baseline or rows[0]["service"]
    out = write_report(rows, baseline, blocks_csv.parent / "report.html")
    print(f"Created: {out}")


# ---------------------------------------------------------------- parser


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="pingmon",
        description="Test and monitor ping-reduction services, "
                    "game by game")
    p.add_argument("--version", action="version",
                   version=f"pingmon {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="write an example config")
    sp.add_argument("--path", default=str(DEFAULT_CONFIG))
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("validate", help="check target health")
    sp.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("sdr-refresh", help="fetch Valve PoPs for CS2")
    sp.set_defaults(func=cmd_sdr_refresh)

    sp = sub.add_parser("discover", help="capture a game's real server IP")
    sp.add_argument("--game", choices=["r6", "cs2", "apex", "warzone",
                                       "fc26"], default=None)
    sp.add_argument("--seconds", type=int, default=20)
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("monitor", help="live monitoring, no test")
    sp.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("run", help="run the interleaved A/B test")
    sp.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    sp.add_argument("--rounds", type=int, default=None)
    sp.add_argument("--block", type=int, default=None)
    sp.add_argument("--no-dashboard", dest="dashboard",
                    action="store_false", default=True)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("report", help="rebuild the report from CSV")
    sp.add_argument("dir")
    sp.add_argument("--baseline", default=None)
    sp.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
