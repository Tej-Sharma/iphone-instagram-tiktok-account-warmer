"""AutoWarmer command line.

  python3 -m autowarmer                 open the dashboard (what most people use)
  python3 -m autowarmer doctor          check this Mac and the phones
  python3 -m autowarmer status          each account's day, phase and rates
  python3 -m autowarmer warm <handle>   dry run; add --live to drive the phone
  python3 -m autowarmer warm-all        every connected phone, one after another
  python3 -m autowarmer onboard <udid>  install the runner on a phone

The dashboard runs these same verbs as child processes, so anything you see in
the UI can be reproduced in a terminal.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import APP_NAME, __version__
from . import setup_flow as S

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
DEFAULT_CFG = ROOT / "config.json"


def _raw(path: str) -> dict:
    cfg = S.load_config(Path(path))
    if cfg is None:
        print(f"no configuration yet — run `python3 -m autowarmer` and the "
              f"dashboard will walk you through setup.\n(looked in {path})")
        sys.exit(2)
    return cfg


def _config_for(path: str, udid: str):
    from ._core import Config
    for cfg in Config.load_all(path):
        if cfg.udid == udid:
            return cfg
    print(f"no phone {udid} in the configuration")
    sys.exit(2)


def cmd_serve(args):
    from .app import serve
    serve(config_path=args.config, port=args.port, open_browser=not args.no_open)


def cmd_doctor(args):
    # Useful BEFORE setup too — "which tools am I missing" is the first
    # question anyone has, and it needs no configuration to answer.
    raw = S.load_config(Path(args.config)) or {}
    print(f"{APP_NAME} {__version__}\n")
    for item in S.detect_all():
        mark = "ok  " if item["found"] else "MISSING"
        print(f"{item['label']:<16} {mark:<8}{item['path']}")
        if not item["found"]:
            for line in item["hint"].splitlines():
                print(f"                 {line}")
    if not raw:
        print("\nno setup yet — run `python3 -m autowarmer` to open the dashboard.")
        return
    problems = S.validate_config(raw)
    print(f"\nconfiguration    {'ok' if not problems else 'INCOMPLETE'}")
    for p in problems:
        print(f"  · {p}")
    devices = S.list_devices(raw.get("ios_binary", "ios"))
    here = {d["udid"] for d in devices}
    print(f"\nphones plugged in {len(devices)}")
    for dev in raw.get("devices", []):
        live = next((d for d in devices if d["udid"] == dev["udid"]), None)
        state = (f"connected · {live.get('name') or '?'} · iOS {live.get('ios') or '?'}"
                 if live else "not connected")
        print(f"  {dev.get('name', dev['udid'][-8:]):<12} {state}")
        for a in dev.get("accounts", []):
            print(f"      {a['platform']:<10} @{a['username']}")
    if not here:
        print("\nplug a phone in over USB, unlock it, and tap Trust This Computer.")


def cmd_status(args):
    from ._core import Config, Engine
    raw = _raw(args.config)
    names = {d["udid"]: d.get("name", "") for d in raw.get("devices", [])}
    for cfg in Config.load_all(args.config):
        for r in Engine(cfg, STATE).status():
            print(f"{names.get(cfg.udid, '')[:10]:<11}{r['platform']:<10} "
                  f"@{r['username']:<18} day {r['day']:<4}{r['phase']:<14}"
                  f"like {r['like_rate']:.0%}  follow {r['follow_rate']:.0%}  "
                  f"{r['sessions_per_day']}×/day")
            print(f"           {r['note']}")


def cmd_warm(args):
    from ._core import Engine
    raw = _raw(args.config)
    try:
        udid = S.resolve_device(raw, args.handle, args.platform)
    except LookupError as e:
        print(f"{e}")
        sys.exit(2)
    cfg = _config_for(args.config, udid)
    eng = Engine(cfg, STATE)
    if not args.live:
        res = eng.run_account(args.handle, live=False, platform=args.platform)
        print("DRY RUN — nothing was driven. Here's the session it would run:\n")
        print(res["plan"].pretty())
        print("\nAdd --live to actually drive the phone.")
        return
    print(f"LIVE — driving {udid[-8:]} for @{args.handle}. "
          "Keep the phone unlocked and plugged in.")
    if args.no_engage:
        print("Practice mode: it will browse, but hold every like, follow and save.")
    res = eng.run_account(args.handle, live=True, engage_live=not args.no_engage,
                          platform=args.platform, trace_level=args.trace)
    print(f"\noutcome   {res['outcome']}  {res.get('detail', '')}".rstrip())
    print(f"counts    {res.get('counts')}")
    print(f"run       {res.get('run_dir')}")
    sys.exit(0 if res.get("outcome") == "complete" else 1)


def cmd_warm_all(args):
    from ._core import fleet
    _raw(args.config)
    print("Warming every connected phone, one after another.\n")
    results = fleet.warm_all(args.config, STATE, live=True,
                             engage_live=not args.no_engage)
    print()
    bad = 0
    for r in results:
        acct = r.get("account", "—")
        outcome = r.get("outcome", "?")
        bad += outcome not in ("complete", "skipped", "locked")
        print(f"  {r['udid'][-8:]}  {acct:<20} {outcome:<12} "
              f"{r.get('detail') or ''}".rstrip())
    sys.exit(1 if bad else 0)


def cmd_install(args):
    from .install_tools import install
    res = install(args.tool, ROOT)
    if not res.get("ok"):
        print(f"\n{res.get('error', 'could not install that')}")
        sys.exit(1)


def cmd_onboard(args):
    from .runner_build import install_runner
    raw = _raw(args.config)
    devices = S.list_devices(raw.get("ios_binary", "ios"))
    if args.udid not in {d["udid"] for d in devices}:
        print(f"phone {args.udid} isn't plugged in (or hasn't been trusted yet).")
        sys.exit(2)
    report = install_runner(raw, args.udid, ROOT)
    if not report.get("ok"):
        print(f"\nnot ready: {report.get('error', 'unknown problem')}")
        if report.get("detail"):
            print(report["detail"])
        sys.exit(1)
    print("\nThis phone is ready to warm.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="autowarmer", description=f"{APP_NAME} — "
                                "warm up your own accounts on your own iPhone")
    p.add_argument("--config", default=str(DEFAULT_CFG))
    p.add_argument("--version", action="version",
                   version=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sv = sub.add_parser("serve", help="open the dashboard (default)")
    sv.add_argument("--port", type=int, default=8790)
    sv.add_argument("--no-open", action="store_true")
    sv.set_defaults(fn=cmd_serve)

    sub.add_parser("doctor", help="check this Mac and the phones").set_defaults(fn=cmd_doctor)
    sub.add_parser("status", help="each account's day, phase and rates").set_defaults(fn=cmd_status)

    wm = sub.add_parser("warm", help="warm one account")
    wm.add_argument("handle")
    wm.add_argument("--live", action="store_true", help="actually drive the phone")
    wm.add_argument("--no-engage", action="store_true",
                    help="practice mode: browse but hold likes, follows and saves")
    wm.add_argument("--platform", choices=list(S.PLATFORMS))
    wm.add_argument("--trace", choices=["full", "errors", "off"], default=None)
    wm.set_defaults(fn=cmd_warm)

    wa = sub.add_parser("warm-all", help="every connected phone, in turn")
    wa.add_argument("--no-engage", action="store_true")
    wa.set_defaults(fn=cmd_warm_all)

    it = sub.add_parser("install", help="fetch a helper tool for you")
    it.add_argument("tool", choices=["ios", "pymobiledevice3", "ocr", "all"])
    it.set_defaults(fn=cmd_install)

    ob = sub.add_parser("onboard", help="install the runner on a phone")
    ob.add_argument("udid")
    ob.set_defaults(fn=cmd_onboard)

    VERBS = {"serve", "doctor", "status", "warm", "warm-all", "onboard", "install"}
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if not VERBS & set(raw_argv):        # bare `python3 -m autowarmer` → dashboard
        raw_argv.append("serve")
    args = p.parse_args(raw_argv)
    args.fn(args)


if __name__ == "__main__":
    main()
