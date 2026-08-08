"""First-run setup — everything AutoWarmer needs, asked for once.

The warm engine needs four things it cannot invent: where the helper binaries
live, an Apple signing identity (to build the on-device runner), which iPhones
to drive, and which accounts live on each. Every one of those is per-user, so
none of it may be hardcoded — this module DETECTS what it can, explains how to
get what it cannot, and writes the result to config.json.

Nothing here touches the network and nothing here drives a phone; it is pure
enough to test, which matters because a wrong path here surfaces much later as
a mystifying failure mid-run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # noqa: F401  (used by detection + device listing)
from pathlib import Path
from typing import Dict, List, Optional

# Where each tool tends to live, in the order worth trying. `shutil.which` is
# consulted first (a user's own PATH beats our guesses); these cover the common
# installs where a GUI app is launched without the shell's PATH.
APP_BIN = str(Path(__file__).resolve().parents[1] / "bin")

KNOWN: Dict[str, List[str]] = {
    # our own bin/ comes first: it is where "Install for me" puts things, and
    # it must win over a stale copy elsewhere on the machine
    "ios": [
        APP_BIN + "/ios",
        "/opt/homebrew/bin/ios", "/usr/local/bin/ios",
        "~/go/bin/ios", "~/.local/bin/ios",
    ],
    "iproxy": [
        "/opt/homebrew/bin/iproxy", "/usr/local/bin/iproxy",
        "/opt/homebrew/opt/libimobiledevice/bin/iproxy",
    ],
    "pymobiledevice3": [
        "/opt/homebrew/bin/pymobiledevice3", "/usr/local/bin/pymobiledevice3",
        "~/.local/bin/pymobiledevice3",
        "~/Library/Python/3.13/bin/pymobiledevice3",
        "~/Library/Python/3.12/bin/pymobiledevice3",
        "~/Library/Python/3.11/bin/pymobiledevice3",
        "~/Library/Python/3.9/bin/pymobiledevice3",
    ],
}

HINTS: Dict[str, str] = {
    # go-ios has no Homebrew formula — it ships as a release binary. Saying
    # "brew install go-ios" would send people to a dead end.
    "ios": "go-ios — download the macOS build from\n"
           "  github.com/danielpaulus/go-ios/releases\n"
           "unzip it, and move the `ios` binary somewhere on your PATH\n"
           "(e.g. /usr/local/bin). With Go installed you can instead run:\n"
           "  go install github.com/danielpaulus/go-ios/cmd/ios@latest",
    "pymobiledevice3": "install with:  python3 -m pip install --user pymobiledevice3",
    "xcode": "Install the full Xcode from the App Store (Command Line Tools alone "
             "cannot build the on-device runner), open it once to accept the "
             "licence, then:  sudo xcode-select -s /Applications/Xcode.app",
    "git": "git — install it with:  xcode-select --install",
}

LABELS = {"ios": "go-ios", "pymobiledevice3": "pymobiledevice3",
          "xcode": "Xcode", "git": "git"}

# What AutoWarmer can fetch itself, and what only a human can do.
INSTALLABLE = {"ios", "pymobiledevice3"}

# Xcode and git are needed to BUILD the on-device runner; go-ios and
# pymobiledevice3 are needed to DRIVE the phone. (iproxy is not in this list:
# the driving lane uses `ios forward`, so nothing ever executes iproxy — asking
# users to install it was pure friction.)
WHY = {
    "xcode": "builds the small helper app that drives your phone",
    "git": "fetches the source of that helper app",
    "ios": "talks to the phone over USB",
    "pymobiledevice3": "starts the helper app on the phone",
}

XCODE_DEFAULT = "/Applications/Xcode.app/Contents/Developer"


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p or ""))


def _runnable(path: str) -> bool:
    p = Path(_expand(path))
    return p.is_file() and os.access(p, os.X_OK)


def find_tool(name: str) -> dict:
    """Locate one helper binary. Returns {name,label,found,path,source,hint}."""
    out = {"name": name, "label": LABELS.get(name, name), "found": False,
           "path": "", "source": "", "hint": HINTS.get(name, ""),
           "why": WHY.get(name, ""),
           "installable": name in INSTALLABLE}
    which = shutil.which(name)
    if which:
        return {**out, "found": True, "path": which, "source": "PATH"}
    for cand in KNOWN.get(name, []):
        if _runnable(cand):
            return {**out, "found": True, "path": _expand(cand),
                    "source": "known location"}
    return out


def find_xcode() -> dict:
    """Full Xcode (needed to build the runner) — not the Command Line Tools."""
    out = {"name": "xcode", "label": "Xcode", "found": False, "path": "",
           "source": "", "hint": HINTS["xcode"], "why": WHY["xcode"],
           "installable": False}
    try:
        sel = subprocess.run(["xcode-select", "-p"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except Exception:
        sel = ""
    for cand, src in ((sel, "xcode-select"), (XCODE_DEFAULT, "known location")):
        # CommandLineTools is a valid xcode-select target but cannot build the
        # runner — treat it as "not found" so the user is told the real fix.
        if cand and "CommandLineTools" not in cand and Path(cand).is_dir():
            return {**out, "found": True, "path": cand, "source": src}
    return out


def detect_all() -> List[dict]:
    """Everything AutoWarmer needs on the Mac, in setup order."""
    return [find_xcode(), find_tool("git"), find_tool("ios"),
            find_tool("pymobiledevice3")]


def check_path(name: str, path: str) -> dict:
    """Validate a path the user typed or picked. Xcode wants a developer dir;
    the rest want an executable file."""
    p = _expand(path)
    if name == "xcode":
        ok = bool(p) and Path(p).is_dir()
        why = "" if ok else "not a directory"
        if ok and "CommandLineTools" in p:
            ok, why = False, ("that's the Command Line Tools — the full Xcode "
                              "app is required to build the runner")
        return {"ok": ok, "path": p, "why": why}
    ok = _runnable(p)
    return {"ok": ok, "path": p,
            "why": "" if ok else "not an executable file at that path"}


# ---------------------------------------------------------------- devices

def list_devices(ios_binary: str) -> List[dict]:
    """Connected iPhones via go-ios, each with name + iOS version when readable.
    Never raises — an unplugged Mac is a normal state, not an error."""
    try:
        out = subprocess.run([_expand(ios_binary), "list"], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        udids = json.loads(out).get("deviceList", [])
    except Exception:
        return []
    devices = []
    for udid in udids:
        row = {"udid": udid, "name": "", "ios": ""}
        try:
            info = json.loads(subprocess.run(
                [_expand(ios_binary), "info", "--udid", udid],
                capture_output=True, text=True, timeout=15).stdout)
            row["name"] = info.get("DeviceName", "")
            row["ios"] = info.get("ProductVersion", "")
            row["model"] = info.get("ProductType", "")
        except Exception:
            pass
        devices.append(row)
    return devices


# ---------------------------------------------------------------- signing

def bundle_id_for(team_id: str, suffix: str = ".xctrunner") -> str:
    """The runner's bundle id under the user's OWN team. Xcode appends
    `.xctrunner` to the id it builds, which is what ends up installed."""
    tid = re.sub(r"[^a-z0-9]", "", (team_id or "team").lower()) or "team"
    return f"com.{tid}.autowarmer.wda{suffix}"


KEYS_DIR = "~/.autowarmer/keys"


def keys_dir_for(keys_dir: str = "") -> str:
    """Where the Apple key is kept. Resolved at call time so a test (or a user
    with an unusual home) can redirect it via AUTOWARMER_KEYS_DIR."""
    return keys_dir or os.environ.get("AUTOWARMER_KEYS_DIR") or KEYS_DIR


def store_p8(src: str, keys_dir: str = "") -> dict:
    """Keep our own copy of the Apple key, readable only by this user.

    Apple lets you download a .p8 exactly once, and people download it to
    Downloads and then tidy Downloads — at which point every future build
    fails with an error about signing that explains nothing. So setup copies
    it somewhere the app owns (0600 in a 0700 directory) and remembers that
    path instead. The key never leaves this Mac; it is only ever handed to
    `xcodebuild` to sign the user's own phones.
    """
    out = {"path": _expand(src), "copied": False, "note": ""}
    src_p = Path(_expand(src))
    if not src_p.is_file():
        return out                     # validation reports this properly
    dest_dir = Path(_expand(keys_dir_for(keys_dir)))
    if src_p.parent == dest_dir:
        out["note"] = "already stored privately"
        return out
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(dest_dir, 0o700)
        dest = dest_dir / src_p.name
        shutil.copyfile(src_p, dest)
        os.chmod(dest, 0o600)
    except OSError as e:               # keep the original path; don't block setup
        out["note"] = f"could not make a private copy ({e}) — using it in place"
        return out
    return {"path": str(dest), "copied": True,
            "note": f"copied to {dest_dir} (readable only by you). "
                    "You can delete the original download now."}


def validate_signing(sign: dict) -> List[str]:
    """Problems with the Apple signing block, in plain words. Empty = fine."""
    problems = []
    p8 = _expand(sign.get("p8_path", ""))
    if not p8:
        problems.append("Apple API key file (.p8) is required to build the runner")
    elif not Path(p8).is_file():
        problems.append(f"no .p8 file at {p8}")
    elif not p8.endswith(".p8"):
        problems.append("the API key should be the .p8 file Apple gave you")
    if not (sign.get("key_id") or "").strip():
        problems.append("Key ID is required (the 10-character id beside your key)")
    if not (sign.get("issuer_id") or "").strip():
        problems.append("Issuer ID is required (the UUID at the top of the Keys page)")
    if not (sign.get("team_id") or "").strip():
        problems.append("Team ID is required (Membership page of your developer account)")
    return problems


# ---------------------------------------------------------------- accounts

PLATFORMS = ("instagram", "tiktok")


def clean_account(acct: dict) -> dict:
    """Normalize one account row from the UI into what the engine reads."""
    kw = acct.get("keywords")
    if isinstance(kw, str):
        kw = [k.strip() for k in re.split(r"[,\n]", kw)]
    return {"platform": acct.get("platform", "instagram"),
            "username": (acct.get("username") or "").strip().lstrip("@"),
            "created_at": (acct.get("created_at") or "")[:10],
            "keywords": [k for k in (kw or []) if k]}


def validate_config(cfg: dict) -> List[str]:
    """Everything wrong with a proposed config, as sentences a person can act
    on. The UI blocks saving until this is empty."""
    problems: List[str] = []
    # iproxy is deliberately not checked: nothing ever executes it (the driving
    # lane uses `ios forward`), so requiring it would block setup for no reason.
    for key, label in (("ios_binary", "go-ios"),
                       ("pymobiledevice3_binary", "pymobiledevice3")):
        path = cfg.get(key, "")
        if not path:
            problems.append(f"{label} path is not set")
        elif not _runnable(path):
            problems.append(f"{label} is not executable at {path}")
    problems += validate_signing(cfg.get("signing") or {})
    devices = cfg.get("devices") or []
    if not devices:
        problems.append("add at least one iPhone")
    seen_udid, seen_acct = set(), set()
    for dev in devices:
        udid = (dev.get("udid") or "").strip()
        if not udid:
            problems.append("a phone has no UDID")
            continue
        if udid in seen_udid:
            problems.append(f"phone {udid[-8:]} is listed twice")
        seen_udid.add(udid)
        if not dev.get("accounts"):
            problems.append(f"phone {dev.get('name') or udid[-8:]} has no accounts")
        for a in dev.get("accounts", []):
            if not a.get("username"):
                problems.append(f"an account on {dev.get('name') or udid[-8:]} has no username")
                continue
            if a.get("platform") not in PLATFORMS:
                problems.append(f"@{a['username']}: platform must be instagram or tiktok")
            key = (a.get("platform"), a["username"].lower())
            if key in seen_acct:
                problems.append(f"@{a['username']} is listed on more than one phone")
            seen_acct.add(key)
            if not a.get("created_at"):
                problems.append(f"@{a['username']} needs the date the account was "
                                "created — the whole ramp is measured from it")
    return problems


# ---------------------------------------------------------------- config io

def build_config(binaries: dict, signing: dict, devices: List[dict],
                 existing: Optional[dict] = None,
                 keys_dir: str = "") -> dict:
    """Assemble config.json from the wizard's pieces, preserving anything the
    user set outside the wizard (trace level, screen sizes, ports).

    config.json records the PATH to the Apple key, never its contents — the
    key stays a separate 0600 file, so a config someone pastes into a chat
    can't leak a signing identity.
    """
    cfg = dict(existing or {})
    cfg["ios_binary"] = _expand(binaries.get("ios", ""))
    # the engine's Config still expects this key; nothing runs it (see above)
    cfg["iproxy_binary"] = _expand(binaries.get("iproxy", "")) or find_tool("iproxy")["path"]
    cfg["pymobiledevice3_binary"] = _expand(binaries.get("pymobiledevice3", ""))
    if binaries.get("xcode"):
        cfg["developer_dir"] = _expand(binaries["xcode"])
    stored = store_p8(signing.get("p8_path", ""), keys_dir)
    cfg["signing"] = {"p8_path": stored["path"],
                      "key_id": (signing.get("key_id") or "").strip(),
                      "issuer_id": (signing.get("issuer_id") or "").strip(),
                      "team_id": (signing.get("team_id") or "").strip()}
    cfg.setdefault("wda_host_port", 8100)
    cfg.setdefault("trace_level", "errors")

    prev = {d.get("udid"): d for d in (existing or {}).get("devices", [])}
    out_devices = []
    for i, dev in enumerate(devices, 1):
        udid = (dev.get("udid") or "").strip()
        old = prev.get(udid, {})
        row = {"udid": udid,
               "name": (dev.get("name") or old.get("name") or f"iphone-{i}").strip(),
               "wda_bundle_id": (dev.get("wda_bundle_id") or old.get("wda_bundle_id")
                                 or bundle_id_for(cfg["signing"]["team_id"])),
               "accounts": [clean_account(a) for a in dev.get("accounts", [])]}
        screen = dev.get("screen_points") or old.get("screen_points")
        if screen:
            row["screen_points"] = list(screen)
        out_devices.append(row)
    cfg["devices"] = out_devices
    # Single-device configs elsewhere read these off the top level; keeping the
    # first phone mirrored there means every code path finds a device.
    if out_devices:
        cfg["udid"] = out_devices[0]["udid"]
        cfg["wda_bundle_id"] = out_devices[0]["wda_bundle_id"]
        cfg["accounts"] = out_devices[0]["accounts"]
    return cfg


def resolve_device(cfg: dict, handle: str,
                   platform: Optional[str] = None) -> str:
    """Which phone is this account logged in on? Fail closed: an unknown or
    ambiguous handle raises rather than guessing, because guessing means
    driving the wrong person's phone."""
    want = (handle or "").strip().lstrip("@").lower()
    hits = []
    for dev in cfg.get("devices") or ([cfg] if cfg.get("udid") else []):
        for a in dev.get("accounts", []):
            if (a.get("username") or "").lower() != want:
                continue
            if platform and a.get("platform") != platform:
                continue
            hits.append(dev.get("udid"))
    hits = [h for h in dict.fromkeys(hits) if h]
    if not hits:
        raise LookupError(f"no configured phone has @{want}"
                          + (f" on {platform}" if platform else ""))
    if len(hits) > 1:
        raise LookupError(f"@{want} is listed on {len(hits)} phones — "
                          "remove the duplicate before running it")
    return hits[0]


def accounts_of(cfg: dict) -> List[dict]:
    """Every configured account, flattened, each carrying its phone."""
    out = []
    for dev in cfg.get("devices") or ([cfg] if cfg.get("udid") else []):
        for a in dev.get("accounts", []):
            out.append({**a, "udid": dev.get("udid"),
                        "device": dev.get("name") or (dev.get("udid") or "")[-8:]})
    return out


def load_config(path: Path) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def save_config(path: Path, cfg: dict) -> None:
    """Write config.json atomically — a half-written config on a crash would
    lock the user out of their own setup."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)


def is_configured(cfg: Optional[dict]) -> bool:
    """Enough to show the dashboard instead of the wizard."""
    return bool(cfg and cfg.get("devices")
                and any(d.get("accounts") for d in cfg["devices"]))
