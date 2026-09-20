"""
cli.py — نقطه ورود

  pingmon init         ساخت فایل کانفیگ نمونه
  pingmon validate     بررسی اینکه کدام مقصدها واقعاً پاسخ می‌دهند
  pingmon sdr-refresh  گرفتن لیست زنده‌ی PoPهای Valve برای CS2
  pingmon monitor      فقط مانیتورینگ زنده، بدون تست A/B
  pingmon run          اجرای کامل تست A/B متناوب
  pingmon discover     شکار IP واقعی سرور بازی (وسط مچ اجرا کن)
  pingmon report DIR   ساخت دوباره‌ی گزارش از روی CSV
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
        sys.exit("PyYAML نصب نیست:  pip install pyyaml rich")
    if not path.exists():
        sys.exit(f"کانفیگ پیدا نشد: {path}\nاول این را بزن:  pingmon init")
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
        sys.exit("هیچ سرویسی در کانفیگ تعریف نشده.")
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
        sys.exit(f"{dst} از قبل هست. برای بازنویسی --force بزن.")
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"ساخته شد: {dst}\nبازش کن، سرویس‌هایت را بنویس، بعد:"
          f"\n  pingmon validate\n  pingmon run")


def cmd_validate(args) -> None:
    cfg = load_config(Path(args.config))
    targets = targets_from_config(cfg) or build_default_targets()
    print(f"{'GAME':<9} {'REGION':<26} {'HOST':<38} {'IP':<16} {'RTT':>8}")
    print("-" * 100)
    ok = bad = 0
    for t in targets:
        ip = t.resolve()
        if not ip:
            print(f"{t.game:<9} {t.region[:25]:<26} {t.host[:37]:<38} "
                  f"{'—':<16} {'DNS FAIL':>8}")
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
                  f"{ip:<16} {'NO REPLY':>8}")
            bad += 1
        else:
            print(f"{t.game:<9} {t.region[:25]:<26} {t.host[:37]:<38} "
                  f"{ip:<16} {best:>7.1f}ms")
            ok += 1
    print("-" * 100)
    print(f"سالم: {ok}   خراب: {bad}")
    if bad:
        print("مقصدهای خراب را از کانفیگ بردار یا با IP دستی جایگزین کن.")


def cmd_sdr_refresh(args) -> None:
    print("در حال گرفتن کانفیگ Steam Datagram Relay از والو…")
    try:
        pops = fetch_sdr_pops()
    except Exception as e:                     # noqa: BLE001
        sys.exit(f"ناموفق: {e}\n"
                 "اگر شبکه اجازه نمی‌دهد، IP رله را دستی در کانفیگ بگذار.")
    print(f"{len(pops)} PoP پیدا شد.\n")
    print("این بخش را در کانفیگ زیر بازی cs2 بگذار:\n")
    print("    targets:")
    for code, ips in sorted(pops.items()):
        print(f"      - {{ region: \"{code}\", ip: \"{ips[0]}\", "
              f"port: 27015, note: \"Valve SDR\" }}")


def cmd_discover(args) -> None:
    from .discover import collect, rank
    print(f"وسط مچ باش. {args.seconds} ثانیه نمونه‌برداری می‌کنم…")

    def tick(i, n, found):
        print(f"  [{i}/{n}] مقصد یکتا: {found}", end="\r", flush=True)

    eps = rank(collect(args.seconds, 2.0, args.game, tick))
    print(" " * 60, end="\r")
    if not eps:
        print("چیزی پیدا نشد. مطمئن شو وسط مچ بودی و بازی اجراست.")
        if args.game:
            print("اگر نام پروسه فرق دارد، --game را بردار تا همه دیده شوند.")
        return
    print(f"\n{'PROTO':<6} {'IP':<17} {'PORT':>6} {'SEEN':>5}  PROCESS")
    print("-" * 66)
    for e in eps[:20]:
        print(f"{e.proto:<6} {e.ip:<17} {e.port:>6} {e.count:>5}  {e.process}")
    print("-" * 66)
    top = eps[0]
    print("\nمحتمل‌ترین سرور بازی — این را در کانفیگ بگذار:\n")
    print(f"      - {{ region: \"discovered\", ip: \"{top.ip}\", "
          f"port: {top.port}, note: \"کشف‌شده وسط مچ\" }}")


def _build(cfg: dict) -> list[Target]:
    targets = targets_from_config(cfg)
    if not targets:
        print("[!] کانفیگ مقصدی ندارد — از پیش‌فرض‌ها استفاده می‌کنم.")
        targets = build_default_targets()
    return targets


def cmd_monitor(args) -> None:
    """فقط نگاه کردن: هیچ بلوک یا سرویسی، فقط متریک‌های زنده."""
    from .dashboard import Dashboard
    cfg = load_config(Path(args.config))
    targets = _build(cfg)
    sess = Session(targets, [Service("live", mode="none")],
                   session_config(cfg, args))
    sess.prepare()
    if not sess.targets:
        sys.exit("هیچ مقصد سالمی نیست.")
    sess.start_probes()
    dash = Dashboard(lambda: sess)
    dash.service = "مانیتور زنده"
    dash.phase = "measure"
    dash.phase_total = 1.0
    dash.log("حالت مانیتور — Ctrl+C برای خروج")
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
    print("\nتمام.")


def cmd_run(args) -> None:
    cfg = load_config(Path(args.config))
    targets = _build(cfg)
    services = services_from_config(cfg)
    scfg = session_config(cfg, args)
    baseline = next(s.name for s in services if s.is_baseline)

    total = scfg.rounds * len(services) * (scfg.block_seconds
                                           + scfg.settle_seconds)
    print(f"\npingmon {__version__}")
    print(f"  مقصدها : {len(targets)}")
    print(f"  سرویس‌ها: {', '.join(s.name for s in services)}")
    print(f"  خط‌پایه : {baseline}")
    print(f"  ساختار : {scfg.rounds} راند × {len(services)} بلوک "
          f"× {scfg.block_seconds}s")
    print(f"  زمان   : حدود {total // 60} دقیقه (بدون احتساب مکث‌های دستی)\n")

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
            print("\nمتوقف شد — تا همین‌جا گزارش می‌سازم.")
    else:
        sess = Session(targets, services, scfg)
        try:
            blocks = sess.run()
        except KeyboardInterrupt:
            sess.abort()
            blocks = sess.store.blocks

    if not blocks:
        print("هیچ داده‌ای جمع نشد.")
        return
    rows = blocks_from_session(blocks)
    out = write_report(rows, baseline, sess.run_dir / "report.html")
    print(f"\nخروجی‌ها در: {sess.run_dir}")
    print(f"  samples.csv  داده‌ی خام هر پروب")
    print(f"  blocks.csv   خلاصه‌ی هر بلوک")
    print(f"  report.html  گزارش نهایی  ←  {out}")


def cmd_report(args) -> None:
    d = Path(args.dir)
    blocks_csv = d / "blocks.csv" if d.is_dir() else d
    if not blocks_csv.exists():
        sys.exit(f"پیدا نشد: {blocks_csv}")
    rows = load_blocks(blocks_csv)
    if not rows:
        sys.exit("فایل خالی است.")
    baseline = args.baseline or rows[0]["service"]
    out = write_report(rows, baseline, blocks_csv.parent / "report.html")
    print(f"ساخته شد: {out}")


# ---------------------------------------------------------------- parser


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="pingmon",
        description="تست و مانیتورینگ سرویس‌های کاهش پینگ، بازی‌به‌بازی")
    p.add_argument("--version", action="version",
                   version=f"pingmon {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="ساخت کانفیگ نمونه")
    sp.add_argument("--path", default=str(DEFAULT_CONFIG))
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("validate", help="بررسی سلامت مقصدها")
    sp.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("sdr-refresh", help="گرفتن PoPهای Valve برای CS2")
    sp.set_defaults(func=cmd_sdr_refresh)

    sp = sub.add_parser("discover", help="شکار IP سرور بازی وسط مچ")
    sp.add_argument("--game", choices=["r6", "cs2", "apex", "warzone",
                                       "fc26"], default=None)
    sp.add_argument("--seconds", type=int, default=20)
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("monitor", help="مانیتورینگ زنده بدون تست")
    sp.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("run", help="اجرای تست A/B متناوب")
    sp.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    sp.add_argument("--rounds", type=int, default=None)
    sp.add_argument("--block", type=int, default=None)
    sp.add_argument("--no-dashboard", dest="dashboard",
                    action="store_false", default=True)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("report", help="ساخت دوباره‌ی گزارش از CSV")
    sp.add_argument("dir")
    sp.add_argument("--baseline", default=None)
    sp.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
