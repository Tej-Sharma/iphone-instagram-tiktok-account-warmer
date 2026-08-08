"""Fleet orchestration — warm every connected + ready phone, onboard new ones.

Devices are driven SEQUENTIALLY (one WDA lane / port 8100 at a time), which
avoids port and resource contention; the root `tunneld` daemon serves tunnels for
all of them so no per-run password prompt. Config supports a `devices: [...]`
array (see Config.load_all); the legacy single-device config is one device.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from . import humanize as H
from .device import Config, Driver, GoIos
from .engine import Engine

ROOT = Path(__file__).resolve().parents[1]


def connected_udids(cfg: Config) -> set:
    return set(GoIos(cfg).list_devices())


def warm_all(config_path: str, state_root: Path, live: bool = True,
             engage_live: bool = True, only_udid: Optional[str] = None,
             rng=None) -> List[dict]:
    """Warm every configured device that is currently connected, sequentially.
    Returns a per-(device,account) result list. Skips devices not plugged in."""
    import random
    rng = rng or random.Random()
    devices = Config.load_all(config_path)
    if not devices:
        return []
    # Tier gate: the free tier drives the first max_phones config devices; the
    # rest are reported locked (visible, never silent) instead of driven.
    # Every configured phone is driveable.
    try:
        from . import entitlements as E
    except ImportError:
        E = None
    raw = json.loads(Path(config_path).read_text())
    ent = E.resolve(raw, state_root) if E else {"max_phones": None}
    locked = []
    if E is not None:
        devices, locked = E.allowed_devices(ent, devices)
    connected = connected_udids((devices or locked)[0])
    results: List[dict] = []
    for cfg in locked:
        if only_udid and cfg.udid != only_udid:
            continue
        results.append({"udid": cfg.udid, "outcome": "locked",
                        "detail": f"free tier covers {ent['max_phones']} phones — "
                                  f"contact {E.CONTACT} to unlock this one"})
    for cfg in devices:
        if only_udid and cfg.udid != only_udid:
            continue
        if cfg.udid not in connected:
            results.append({"udid": cfg.udid, "outcome": "skipped", "detail": "not connected"})
            continue
        eng = Engine(cfg, state_root)
        for acct in cfg.accounts:
            try:
                res = eng.run_account(acct["username"], live=live, engage_live=engage_live,
                                      platform=acct["platform"])
                results.append({"udid": cfg.udid, "account": acct["username"],
                                "outcome": res.get("outcome"), "detail": res.get("detail"),
                                "run_dir": res.get("run_dir")})
            except Exception as e:                                   # noqa: BLE001
                results.append({"udid": cfg.udid, "account": acct["username"],
                                "outcome": "error", "detail": f"{type(e).__name__}: {e}"})
            time.sleep(H.jittered_delay(rng, 25.0))                  # human gap between accounts
        # ensure THIS device's lane is fully down before the next device —
        # SCOPED to this udid (Driver.force_teardown matches only helpers
        # carrying this device's udid / kernel-tunnel RSD). A blanket
        # `pkill -f "dvt xcuitest"` here reaped EVERY phone's runner, including a
        # concurrent posting agent mid-post on another phone — which turns a live
        # post into a draft. force_teardown also refuses a lane another process
        # owns, so warm-all and the posting agent coexist safely.
        try:
            Driver(cfg).force_teardown()
        except Exception:
            pass
        time.sleep(2)
    if E is not None:
        E.push_status(raw, state_root)  # web console mirror — best-effort
    return results


def status(config_path: str) -> List[dict]:
    """Per-device readiness: configured, connected, iOS version, runner installed."""
    devices = Config.load_all(config_path)
    if not devices:
        return []
    ios = GoIos(devices[0])
    connected = set(ios.list_devices())
    rows = []
    for cfg in devices:
        row = {"udid": cfg.udid, "connected": cfg.udid in connected,
               "accounts": [a["username"] for a in cfg.accounts]}
        if row["connected"]:
            info = GoIos(cfg).info()
            row["ios"] = info.get("ProductVersion", "?")
            row["name"] = info.get("DeviceName", "?")
            row["runner_installed"] = _runner_installed(cfg)
        rows.append(row)
    return rows


def _runner_installed(cfg: Config) -> bool:
    """Is our WDA runner installed on the device? (go-ios apps list)."""
    try:
        out = subprocess.run([cfg.ios_binary, "apps", "--udid", cfg.udid],
                             capture_output=True, text=True, timeout=30).stdout
        return cfg.wda_bundle_id.replace(".xctrunner", "") in out or cfg.wda_bundle_id in out
    except Exception:
        return False


def onboard(config_path: str, udid: str) -> dict:
    """One-time prep so a phone is queue-ready: verify it's paired, build+install
    the WDA runner for THIS udid (registers the UDID in provisioning via the ASC
    key), then verify the lane comes up. Reports remaining MANUAL steps (Trust /
    Developer Mode) it can't automate."""
    d = json.loads(Path(config_path).read_text())
    cfgs = Config.load_all(config_path)
    cfg = next((c for c in cfgs if c.udid == udid), cfgs[0])
    report = {"udid": udid}

    if udid not in connected_udids(cfg):
        report["connected"] = False
        report["next"] = "plug the phone in and tap 'Trust This Computer' on the device"
        return report
    report["connected"] = True

    sign = d.get("signing", {})
    if not sign.get("p8_path"):
        report["next"] = ("add a `signing` block to config (p8_path, key_id, issuer_id, "
                          "team_id) so the runner can be built for new devices")
        return report

    proj = str(ROOT / "vendor" / "WebDriverAgent")
    bundle_base = cfg.wda_bundle_id.replace(".xctrunner", "")
    env = dict(os.environ)
    env["DEVELOPER_DIR"] = "/Applications/Xcode.app/Contents/Developer"
    build = subprocess.run(
        ["xcodebuild", "build-for-testing",
         "-project", f"{proj}/WebDriverAgent.xcodeproj", "-scheme", "WebDriverAgentRunner",
         "-destination", f"platform=iOS,id={udid}", "-derivedDataPath", f"{proj}/build",
         "-allowProvisioningUpdates",
         # new phones aren't in the team profile yet — let xcodebuild register
         # the UDID with App Store Connect automatically (needs the ASC key)
         "-allowProvisioningDeviceRegistration",
         "-authenticationKeyPath", os.path.expanduser(sign["p8_path"]),
         "-authenticationKeyID", sign.get("key_id", ""),
         "-authenticationKeyIssuerID", sign.get("issuer_id", ""),
         f"DEVELOPMENT_TEAM={sign.get('team_id', '')}",
         f"PRODUCT_BUNDLE_IDENTIFIER={bundle_base}", "CODE_SIGN_STYLE=Automatic"],
        capture_output=True, text=True, env=env, timeout=900)
    report["build_ok"] = "BUILD SUCCEEDED" in build.stdout
    if not report["build_ok"]:
        report["next"] = "build failed — device may need Developer Mode ON (Settings > Privacy & Security)"
        report["build_tail"] = build.stdout[-400:]
        return report

    app = f"{proj}/build/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    inst = subprocess.run([cfg.ios_binary, "install", f"--path={app}", f"--udid={udid}"],
                          capture_output=True, text=True, timeout=240)
    report["install_ok"] = "installation successful" in (inst.stdout + inst.stderr)

    drv = Driver(cfg)
    try:
        report["lane_ok"] = drv.ensure_lane(wait_s=90)
    finally:
        drv.close()
    report["ready"] = bool(report.get("install_ok") and report.get("lane_ok"))
    if not report["ready"]:
        report["next"] = ("if lane failed: enable Developer Mode on the device and re-run onboard; "
                          "the first XCUITest run may need a one-time on-device confirm")
    return report
