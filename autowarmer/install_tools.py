"""Install the helper tools for the user, instead of telling them to.

Setup used to print instructions and hope. Two of the four things AutoWarmer
needs can simply be fetched, so they are: go-ios (a release binary from
GitHub) and pymobiledevice3 (a pip package). Both land somewhere the app owns
— no sudo, no PATH edits, nothing touched outside the user's own account.

Xcode is the exception and always will be: it is a 10 GB App Store install
that only the person at the keyboard can accept the licence for.

Everything here streams progress with `log` because it runs as a job whose
output the dashboard shows live.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

GO_IOS_LATEST = "https://api.github.com/repos/danielpaulus/go-ios/releases/latest"
GO_IOS_FALLBACK = ("https://github.com/danielpaulus/go-ios/releases/latest/"
                   "download/go-ios-mac.zip")


def _run(argv, log, timeout=900) -> subprocess.CompletedProcess:
    log("  $ " + " ".join(str(a) for a in argv))
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    for line in (p.stdout or "").splitlines()[-12:]:
        log("    " + line)
    if p.returncode != 0:
        for line in (p.stderr or "").splitlines()[-12:]:
            log("    " + line)
    return p


# ------------------------------------------------------------------ go-ios

def _mac_asset_url(log: Callable[[str], None]) -> str:
    """The macOS build of the newest go-ios release."""
    try:
        req = urllib.request.Request(
            GO_IOS_LATEST, headers={"Accept": "application/vnd.github+json",
                                    "User-Agent": "AutoWarmer"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.loads(r.read())
        for a in rel.get("assets", []):
            name = a.get("name", "").lower()
            if "mac" in name or "darwin" in name:
                log(f"  found {a['name']} from release {rel.get('tag_name')}")
                return a["browser_download_url"]
        log("  release had no macOS build listed — using the standard URL")
    except Exception as e:                                    # noqa: BLE001
        log(f"  could not read the release list ({e}) — using the standard URL")
    return GO_IOS_FALLBACK


def install_go_ios(dest_dir: Path, log: Callable[[str], None] = print) -> dict:
    """Download go-ios and put the `ios` binary in `dest_dir`."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "ios"
    url = _mac_asset_url(log)
    log(f"  downloading {url}")
    try:
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td) / "go-ios.zip"
            req = urllib.request.Request(url, headers={"User-Agent": "AutoWarmer"})
            with urllib.request.urlopen(req, timeout=300) as r, \
                    open(zip_path, "wb") as f:
                shutil.copyfileobj(r, f)
            size = zip_path.stat().st_size
            log(f"  downloaded {size/1024/1024:.1f} MB, unpacking")
            with zipfile.ZipFile(zip_path) as z:
                member = next((m for m in z.namelist()
                               if Path(m).name == "ios" and not m.endswith("/")), None)
                if member is None:
                    return {"ok": False, "path": "",
                            "error": "that download did not contain an `ios` binary"}
                z.extract(member, td)
                shutil.copyfile(Path(td) / member, dest)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "path": "",
                "error": f"download failed: {e}. You can install it by hand from "
                         "github.com/danielpaulus/go-ios/releases"}
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    # macOS quarantines anything downloaded; without this the first run is
    # blocked by Gatekeeper with a dialog nobody expects mid-setup.
    subprocess.run(["xattr", "-d", "com.apple.quarantine", str(dest)],
                   capture_output=True)
    check = subprocess.run([str(dest), "version"], capture_output=True,
                           text=True, timeout=60)
    if check.returncode != 0:
        return {"ok": False, "path": str(dest),
                "error": "installed, but it would not run: "
                         + (check.stderr or "").strip()[:200]}
    log(f"  installed go-ios {(check.stdout or '').strip()} → {dest}")
    return {"ok": True, "path": str(dest), "error": ""}


# --------------------------------------------------------- pymobiledevice3

def install_pymobiledevice3(log: Callable[[str], None] = print) -> dict:
    """pip-install pymobiledevice3 into the user's own site-packages.

    Deliberately NOT `--upgrade`: a working install is left exactly as it is.
    This tool talks to a moving target (Apple changes the device protocols every
    release), so silently bumping a version that currently drives someone's
    phones is a good way to break a setup that was fine a minute ago.
    """
    from .setup_flow import find_tool
    have = find_tool("pymobiledevice3")
    if have["found"]:
        log(f"  already installed → {have['path']} (left untouched)")
        return {"ok": True, "path": have["path"], "error": ""}
    log("  installing pymobiledevice3 (this can take a minute)")
    p = _run([sys.executable, "-m", "pip", "install", "--user",
              "pymobiledevice3"], log)
    if p.returncode != 0:
        return {"ok": False, "path": "",
                "error": "pip could not install pymobiledevice3 — see the log above"}
    from .setup_flow import find_tool
    found = find_tool("pymobiledevice3")
    if found["found"]:
        log(f"  installed → {found['path']}")
        return {"ok": True, "path": found["path"], "error": ""}
    # installed as a module but its script dir isn't a place we look
    return {"ok": False, "path": "",
            "error": "installed, but the pymobiledevice3 command could not be "
                     "found afterwards — add your Python user bin directory to PATH"}


# ------------------------------------------------------------------- extras

def build_ocr(root: Path, log: Callable[[str], None] = print) -> dict:
    """Compile the Apple Vision text-reader AutoWarmer uses to confirm which
    account is on screen. Doing it during setup means a missing Swift compiler
    surfaces here, and not in the middle of the first warm-up."""
    src = Path(root) / "tools" / "vision_ocr.swift"
    out = Path(root) / "bin" / "vision_ocr"
    if not src.is_file():
        return {"ok": False, "path": "", "error": f"missing {src}"}
    out.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("swiftc") is None:
        return {"ok": False, "path": "",
                "error": "swiftc not found — install Xcode, then run: "
                         "sudo xcode-select -s /Applications/Xcode.app"}
    p = _run(["swiftc", "-O", str(src), "-o", str(out)], log, timeout=600)
    if p.returncode != 0 or not out.is_file():
        return {"ok": False, "path": "", "error": "the text reader failed to build"}
    log(f"  built the screen text reader → {out}")
    return {"ok": True, "path": str(out), "error": ""}


TOOLS = {
    "ios": ("go-ios", lambda root, log: install_go_ios(Path(root) / "bin", log)),
    "pymobiledevice3": ("pymobiledevice3", lambda root, log: install_pymobiledevice3(log)),
    "ocr": ("screen text reader", lambda root, log: build_ocr(root, log)),
}


def install(tool: str, root: Path, log: Callable[[str], None] = print) -> dict:
    """Install one tool by name, or everything installable with 'all'."""
    if tool == "all":
        results = {}
        for name in TOOLS:
            log(f"— {TOOLS[name][0]}")
            results[name] = install(name, root, log)
        ok = all(r["ok"] for r in results.values())
        return {"ok": ok, "results": results,
                "error": "" if ok else "some tools could not be installed"}
    entry = TOOLS.get(tool)
    if entry is None:
        return {"ok": False, "path": "",
                "error": f"{tool} has to be installed by hand"}
    label, fn = entry
    log(f"installing {label}…")
    res = fn(root, log)
    log(("done — " + res["path"]) if res["ok"] else ("failed — " + res["error"]))
    return res
