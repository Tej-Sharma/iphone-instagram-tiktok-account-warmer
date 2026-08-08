"""Build and install the on-device runner — the one hard setup step.

Driving an iPhone needs a small Apple-signed test app (stock Appium
WebDriverAgent) installed on that phone. It must be signed with the phone
owner's OWN Apple developer identity, which is why setup asks for an App Store
Connect API key: it lets us build and register this specific phone without
anyone logging in to a browser.

Everything here comes from the user's config — the Xcode location, the key, the
team, the bundle id. Nothing is hardcoded to any one machine.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

WDA_REPO = "https://github.com/appium/WebDriverAgent"


def wda_source(cfg: dict, root: Path) -> Optional[Path]:
    """Where the WebDriverAgent checkout lives, if it's there."""
    cand = cfg.get("wda_source") or (root / "vendor" / "WebDriverAgent")
    p = Path(os.path.expanduser(str(cand)))
    return p if (p / "WebDriverAgent.xcodeproj").is_dir() else None


def fetch_source(cfg: dict, root: Path, log=print) -> Optional[Path]:
    """Clone WebDriverAgent if it isn't present. It's stock Appium source —
    we build it ourselves so the runner is signed by the user, not by us."""
    have = wda_source(cfg, root)
    if have:
        return have
    dest = Path(os.path.expanduser(str(cfg.get("wda_source")
                                       or root / "vendor" / "WebDriverAgent")))
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"fetching WebDriverAgent source → {dest}")
    r = subprocess.run(["git", "clone", "--depth", "1", WDA_REPO, str(dest)],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        log(f"  clone failed: {(r.stderr or '').strip()[-300:]}")
        log(f"  clone it yourself with:  git clone {WDA_REPO} {dest}")
        return None
    # a fresh clone is missing this bundle dir and the build fails without it
    (dest / "Resources" / "WebDriverAgent.bundle").mkdir(parents=True, exist_ok=True)
    return wda_source(cfg, root)


def install_runner(cfg: dict, udid: str, root: Path, log=print) -> dict:
    """Build the runner for THIS phone and install it. Returns a report dict.

    The build registers the phone's UDID with the user's developer account
    automatically (`-allowProvisioningDeviceRegistration`), which is the step
    that otherwise sends people into the Apple developer portal by hand.
    """
    report: dict = {"udid": udid, "ok": False}
    sign = cfg.get("signing") or {}
    missing = [k for k in ("p8_path", "key_id", "issuer_id", "team_id")
               if not (sign.get(k) or "").strip()]
    if missing:
        report["error"] = ("Apple signing details are incomplete: "
                           + ", ".join(missing))
        return report

    src = fetch_source(cfg, root, log)
    if src is None:
        report["error"] = "WebDriverAgent source is not available"
        return report

    dev_dir = cfg.get("developer_dir") or "/Applications/Xcode.app/Contents/Developer"
    device = next((d for d in cfg.get("devices", []) if d.get("udid") == udid), {})
    bundle = (device.get("wda_bundle_id")
              or cfg.get("wda_bundle_id", "")).replace(".xctrunner", "")
    if not bundle:
        report["error"] = "no runner bundle id configured for this phone"
        return report

    env = dict(os.environ)
    env["DEVELOPER_DIR"] = os.path.expanduser(dev_dir)
    build_dir = src / "build"
    log(f"building the runner for {udid[-8:]} (a few minutes the first time)…")
    build = subprocess.run(
        ["xcodebuild", "build-for-testing",
         "-project", str(src / "WebDriverAgent.xcodeproj"),
         "-scheme", "WebDriverAgentRunner",
         "-destination", f"platform=iOS,id={udid}",
         "-derivedDataPath", str(build_dir),
         "-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration",
         "-authenticationKeyPath", os.path.expanduser(sign["p8_path"]),
         "-authenticationKeyID", sign["key_id"],
         "-authenticationKeyIssuerID", sign["issuer_id"],
         f"DEVELOPMENT_TEAM={sign['team_id']}",
         f"PRODUCT_BUNDLE_IDENTIFIER={bundle}", "CODE_SIGN_STYLE=Automatic"],
        capture_output=True, text=True, env=env, timeout=1800)
    report["build_ok"] = "BUILD SUCCEEDED" in build.stdout
    if not report["build_ok"]:
        tail = (build.stdout or "")[-600:] + (build.stderr or "")[-400:]
        report["error"] = _explain_build_failure(tail)
        report["detail"] = tail
        log("build failed — " + report["error"])
        return report
    log("build succeeded")

    app = build_dir / "Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    log("installing it on the phone…")
    inst = subprocess.run([os.path.expanduser(cfg["ios_binary"]), "install",
                           f"--path={app}", f"--udid={udid}"],
                          capture_output=True, text=True, timeout=600)
    blob = (inst.stdout or "") + (inst.stderr or "")
    report["install_ok"] = "installation successful" in blob.lower()
    if not report["install_ok"]:
        report["error"] = "install failed — is the phone unlocked and trusted?"
        report["detail"] = blob[-400:]
        log(report["error"])
        return report

    report["ok"] = True
    log("runner installed. The first run asks for the phone's passcode ON THE "
        "DEVICE once, to allow automation.")
    return report


def _explain_build_failure(tail: str) -> str:
    """Turn xcodebuild's wall of text into the one thing to fix."""
    low = tail.lower()
    if "developer mode" in low or "device is locked" in low:
        return ("the phone needs Developer Mode ON "
                "(Settings → Privacy & Security → Developer Mode) and to be unlocked")
    if "no profiles for" in low or "provisioning" in low:
        return ("Apple rejected the signing request — check the Team ID matches "
                "the API key's account, and that the key has Admin or App Manager access")
    if "authenticationkeypath" in low or "invalid_client" in low or "401" in low:
        return "the App Store Connect API key was rejected — check the Key ID and Issuer ID"
    if "xcode-select" in low or "command line tools" in low:
        return ("Xcode is not selected — run:  "
                "sudo xcode-select -s /Applications/Xcode.app")
    return "see the detail below for what xcodebuild reported"
