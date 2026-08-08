"""Device layer — talk to the iPhone.

Two tiers:
  * READ-ONLY (safe, collides with nothing): list devices, read info, grab a
    screenshot via go-ios's screenshot service. Used by `doctor`.
  * DRIVING (WebDriverAgent over port 8100): tap/swipe/source. Only invoked in
    live mode. Launching WDA competes with any other tool driving the same phone on
    port 8100, so only one driver may run at a time — see README.

We drive the phone with `ios` (go-ios), which setup installs, and its
installed, signed WDA runner, so there is nothing new to sign or install.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


@dataclass
class Config:
    udid: str
    wda_bundle_id: str            # our runner's .xctrunner id
    ios_binary: str               # go-ios (used only for `forward` on 8100)
    iproxy_binary: str
    pymobiledevice3_binary: str = "pymobiledevice3"
    wda_host_port: int = 8100
    trace_level: str = "full"     # full | errors | off — per-step trace verbosity
    screen_points: Optional[List[int]] = None   # [w,h] logical points; avoids window_size() (wedges on TikTok)
    tunneld_port: int = 49151     # pymobiledevice3 tunneld REST port (root daemon => no per-run sudo)
    vision_recovery: bool = False # opt-in: use a vision advisor on unknown screens
    claude_binary: str = "claude"
    ai_comments: bool = False     # opt-in: AI-generate short comments from screen content
    ai_text_model: str = "openai/gpt-4o-mini"
    ai_vision_model: str = "openai/gpt-4o-mini"
    # Sound applied when a job carries no soundName. Either one string for all
    # platforms, or {"instagram": …, "tiktok": …} — the catalogs differ, so a
    # per-platform default is usually what you want (e.g. "volt slope" exists
    # on TikTok but not on IG, where it would just skip music).
    default_sound: object = ""
    accounts: List[dict] = field(default_factory=list)

    @classmethod
    def _from_merged(cls, m: dict) -> "Config":
        return cls(udid=m["udid"], wda_bundle_id=m["wda_bundle_id"],
                   ios_binary=m["ios_binary"], iproxy_binary=m["iproxy_binary"],
                   pymobiledevice3_binary=m.get("pymobiledevice3_binary", "pymobiledevice3"),
                   wda_host_port=int(m.get("wda_host_port", 8100)),
                   trace_level=m.get("trace_level", "full"),
                   screen_points=m.get("screen_points"),
                   tunneld_port=int(m.get("tunneld_port", 49151)),
                   vision_recovery=bool(m.get("vision_recovery", False)),
                   claude_binary=m.get("claude_binary", "claude"),
                   ai_comments=bool(m.get("ai_comments", False)),
                   ai_text_model=m.get("ai_text_model", "openai/gpt-4o-mini"),
                   ai_vision_model=m.get("ai_vision_model", "openai/gpt-4o-mini"),
                   default_sound=m.get("default_sound", ""),
                   accounts=m.get("accounts", []))

    @classmethod
    def load(cls, path: str) -> "Config":
        """Single-device config (first device, or legacy top-level)."""
        return cls.load_all(path)[0]

    @classmethod
    def load_all(cls, path: str) -> "List[Config]":
        """One Config per device. Supports a `devices: [{udid, wda_bundle_id,
        screen_points, accounts}, ...]` array (each inheriting shared top-level
        fields), or the legacy single-device top-level format."""
        d = json.loads(Path(path).read_text())
        if "devices" not in d:
            return [cls._from_merged(d)]
        shared = {k: v for k, v in d.items() if k != "devices"}
        out = []
        for dev in d["devices"]:
            m = dict(shared)
            m.pop("accounts", None)          # device supplies its own accounts
            m.update(dev)                    # device fields override shared
            m.setdefault("wda_bundle_id", d.get("wda_bundle_id"))
            m.setdefault("screen_points", d.get("screen_points"))
            out.append(cls._from_merged(m))
        return out


class GoIos:
    """Thin wrapper over the bundled go-ios binary (read-only surface)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _run(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run([self.cfg.ios_binary, *args],
                              capture_output=True, text=True, timeout=timeout)

    def list_devices(self) -> List[str]:
        out = self._run("list").stdout.strip()
        try:
            return json.loads(out).get("deviceList", [])
        except Exception:
            return []

    def info(self) -> dict:
        out = self._run("info", "--udid", self.cfg.udid).stdout.strip()
        try:
            return json.loads(out)
        except Exception:
            return {}

    def screenshot(self, dest: str) -> bool:
        """Capture the screen without touching WDA (separate service; safe)."""
        r = self._run("screenshot", "--udid", self.cfg.udid, "--output", dest, timeout=40)
        return Path(dest).exists() and Path(dest).stat().st_size > 0


_SCREEN_CACHE_DIR = Path(__file__).resolve().parent.parent / "state" / "screen"


class WDA:
    """WebDriverAgent WebDriver client — DRIVING tier (live mode only).

    Assumes something has already launched the runner and forwarded device
    port 8100 to `host_port` (see Driver.ensure_lane). All taps go through here.
    """

    def __init__(self, host_port: int, screen_hint: Optional[Sequence[int]] = None,
                 cache_path: Optional[Path] = None):
        self.base = f"http://127.0.0.1:{host_port}"
        self.session_id: Optional[str] = None
        self._screen: Optional[Tuple[int, int]] = (
            (int(screen_hint[0]), int(screen_hint[1])) if screen_hint else None)
        self._screen_cache = cache_path

    def _req(self, method: str, path: str, body: Optional[dict] = None, timeout: int = 20):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # WDA puts the ACTUAL reason in the response body; the bare status
            # line ("HTTP Error 400:") is undebuggable. Keep the exception type
            # and .code intact so callers that branch on status still work.
            try:
                detail = (json.loads(e.read().decode()).get("value") or {})
                msg = detail.get("message") or ""
            except Exception:                                     # noqa: BLE001
                msg = ""
            if msg:
                e.msg = f"{e.msg} {msg.splitlines()[0][:300]}"
            raise

    def status(self) -> dict:
        return self._req("GET", "/status")

    def reachable(self) -> bool:
        try:
            return bool(self.status().get("value"))
        except Exception:
            return False

    def alive(self, timeout: int = 4) -> bool:
        """Fast, BOUNDED liveness probe — distinct from reachable() (20s).

        A wedged WDA on TikTok dies in one of two ways: it closes the socket
        (empty body → JSONDecodeError, instant) or holds it open silently
        (socket.timeout, blocks the full request timeout). Recovery code must
        tell 'WDA is dead' from 'this screen is a UI trap' in a few seconds —
        otherwise a single reorient round taps a dead socket for ~90s (each
        tap/swipe blocks ~20s) and leaves the app foregrounded, which is
        exactly the 'stuck on the comment/search screen' the user sees.
        """
        try:
            r = self._req("GET", "/status", timeout=timeout)
            return bool(r.get("value"))
        except Exception:
            return False

    def new_session(self, bundle_id: Optional[str] = None) -> str:
        # The idle/quiescence disable is baked into our patched runner's
        # FBConfiguration defaults (0), not passed here — W3C drops unprefixed
        # vendor capabilities, and passing them added instability.
        always = {}
        if bundle_id:
            always["bundleId"] = bundle_id
        cap = {"capabilities": {"alwaysMatch": always}}
        r = self._req("POST", "/session", cap)
        self.session_id = r.get("sessionId") or r.get("value", {}).get("sessionId")
        return self.session_id

    def configure(self) -> None:
        """Apply the settings that make WDA usable on constantly-animating apps.

        TikTok never goes 'idle' (its feed animates forever), so WDA's default
        wait-for-idle makes EVERY request — source, taps, even window/size —
        block until a timeout. Zeroing the idle/quiescence waits and bounding the
        snapshot depth is what lets TikTok be driven at all. Harmless on IG.
        Must be called after new_session (settings are per-session).
        """
        try:
            self._req("POST", f"/session/{self.session_id}/appium/settings",
                      {"settings": {"waitForIdleTimeout": 0,
                                    "animationCoolOffTimeout": 0,
                                    "shouldWaitForQuiescence": False,
                                    "snapshotMaxDepth": 32,
                                    "customSnapshotTimeout": 15}}, timeout=20)
        except Exception:
            pass          # best-effort; driving still works, just slower on TikTok

    def alert_text(self) -> Optional[str]:
        """Text of a native iOS alert if one is up, else None. Native alerts
        (permission prompts: local network, notifications, Photos) are NOT
        in-app modals — OCR/tap dismissal can't safely handle them, and one
        left on screen wedges every subsequent action."""
        try:
            r = self._req("GET", f"/session/{self.session_id}/alert/text", timeout=8)
            v = r.get("value")
            return v if isinstance(v, str) else None
        except Exception:
            return None                  # 404/NoAlertOpenError ⇒ no alert

    def alert_dismiss(self) -> bool:
        """DENY a native alert (taps its cancel/'Don't Allow' button).

        Deny is always the right default for us: the SOP wants Contacts denied,
        location off, and no new device permissions granted mid-session — and a
        prompt we didn't ask for should never be accepted by an automation.
        Photos-add for our own importer is the one exception, and that is
        accepted explicitly by the staging path, not here.
        """
        try:
            self._req("POST", f"/session/{self.session_id}/alert/dismiss", {}, timeout=8)
            return True
        except Exception:
            return False

    def window_size(self) -> Tuple[int, int]:
        """Logical screen size — BOUNDED, cached, and never fatal.

        The screen is a static property of the phone, so this must never cost a
        live snapshot on the hot path — and never fail a run. Order: config hint
        / already-learned → on-disk cache → one generous live query. On this
        fleet the cache is seeded, so driving issues ZERO window/size calls.
        """
        if self._screen:
            return self._screen
        cached = self._read_screen_cache()
        if cached:
            self._screen = cached
            return cached
        # Nothing known yet: ask ONCE, generously. Never with a short timeout —
        # WDA serves requests serially and a client-side timeout does NOT cancel
        # the server's work, so an abandoned 6s snapshot keeps running for ~15s
        # and every tap issued behind it queues and times out too. That cascade
        # is what made taps 'hang' on Robert's phone (2026-08-04); short retries
        # here made it worse, not better.
        v = self._req("GET", f"/session/{self.session_id}/window/size",
                      timeout=60)["value"]
        wh = (int(v["width"]), int(v["height"]))
        self._screen = wh
        self._write_screen_cache(wh)
        return wh

    def _read_screen_cache(self) -> Optional[Tuple[int, int]]:
        try:
            d = json.loads(self._screen_cache.read_text())
            return int(d["w"]), int(d["h"])
        except Exception:
            return None

    def _write_screen_cache(self, wh: Tuple[int, int]) -> None:
        try:
            self._screen_cache.parent.mkdir(parents=True, exist_ok=True)
            self._screen_cache.write_text(json.dumps({"w": wh[0], "h": wh[1]}))
        except Exception:
            pass

    def wait_ready(self, attempts: int = 12, each: int = 3) -> bool:
        """Poll a bounded snapshot (window/size) until the foreground app is
        snapshot-ready. During a cold load / transition (TikTok), snapshots wedge
        for many seconds, which makes coordinate taps hang; once the app settles
        the same call returns instantly. Returns True once ready."""
        for _ in range(attempts):
            try:
                self._req("GET", f"/session/{self.session_id}/window/size", timeout=each)
                return True
            except Exception:
                time.sleep(1.5)
        return False

    def tap(self, x: int, y: int) -> None:
        # Snapshot-free absolute injection (our runner patch) — works even while
        # a never-idling app (TikTok) would wedge the standard W3C coordinate path.
        self._mw("/wda/mw/tap", {"x": int(x), "y": int(y)})

    def double_tap(self, x: int, y: int, gap_ms: int = 90) -> None:
        """Double-tap (native like on TikTok / Reels) — one tight injected event
        (count=2) so the inter-tap gap registers as a like, snapshot-free."""
        self._mw("/wda/mw/tap", {"x": int(x), "y": int(y), "count": 2})

    def swipe(self, x0: int, y0: int, x1: int, y1: int, ms: int = 320) -> None:
        self._mw("/wda/mw/swipe", {"x1": int(x0), "y1": int(y0),
                                   "x2": int(x1), "y2": int(y1), "durationMs": int(ms)})

    def _mw(self, path: str, body: dict, timeout: int = 10, retries: int = 2) -> None:
        # Absolute-coordinate injection via our runner's snapshot-free endpoint.
        # Bounded + retried: a wedged inject fails fast, and a settle between
        # tries is usually enough (a cold-loading app briefly stalls even the
        # snapshot-free path). Between tries we let a transient wedge clear
        # rather than killing the run on one unlucky tap.
        last = None
        for _ in range(retries + 1):
            try:
                self._req("POST", path, body, timeout=timeout)
                return
            except Exception as e:
                last = e
                time.sleep(1.0)
        raise last

    def send_keys(self, text: str, human: bool = True) -> None:
        """Type into the currently-focused field (search box / comment composer).

        Human-paced by default: one character at a time with a jittered
        inter-keystroke gap (~55-190ms) plus occasional longer "thinking"
        pauses, and a slightly longer beat after spaces. Blasting the whole
        string in one shot is an obvious automation tell (no human types 20
        chars in one frame), so warm-up typing is deliberately slowed. Pass
        human=False for an instant fill when speed matters and realism doesn't."""
        if not human:
            self._req("POST", f"/session/{self.session_id}/wda/keys",
                      {"value": list(text)})
            return
        import random
        for ch in text:
            self._req("POST", f"/session/{self.session_id}/wda/keys", {"value": [ch]})
            time.sleep(random.uniform(0.055, 0.19))
            if ch == " ":
                time.sleep(random.uniform(0.06, 0.22))       # small beat between words
            if random.random() < 0.06:
                time.sleep(random.uniform(0.35, 0.95))       # occasional pause (thinking)

    def _perform(self, actions: list, timeout: int = 8, retries: int = 1) -> None:
        # Coordinate actions resolve via an app-frame snapshot, which is fast on a
        # settled screen but can wedge for many seconds while a never-idling app
        # (TikTok) is still loading/transitioning. Bound each attempt and retry
        # after a short settle rather than blocking ~20s. One retry only, so a
        # navigation double-tap (harmless) is the worst case — never rapid spam.
        last = None
        for attempt in range(retries + 1):
            try:
                self._req("POST", f"/session/{self.session_id}/actions",
                          {"actions": actions}, timeout=timeout)
                return
            except Exception as e:                       # socket timeout / transient
                last = e
                time.sleep(1.5)                          # let the app settle, then retry
        raise last

    def source(self) -> str:
        """Accessibility tree (XML) — element-based navigation before OCR."""
        return self._req("GET", f"/session/{self.session_id}/source").get("value", "")

    def screenshot(self, dest: str) -> bool:
        """Capture the screen through WDA itself (base64 PNG) — no go-ios /
        tunnel needed, so it works regardless of which tool owns the tunnel."""
        import base64
        b64 = self._req("GET", "/screenshot").get("value", "")
        if not b64:
            return False
        Path(dest).write_bytes(base64.b64decode(b64))
        return True

    def active_app(self) -> Optional[str]:
        """Bundle id of the FOREGROUND app, or None if it can't be read.

        The cheapest possible "are we still where we think we are" check, and
        the one that stops blind coordinate taps from landing on the
        springboard: if a launch silently failed, a tab-bar tap becomes a tap
        on the dock, which opens Phone/Messages instead (observed 2026-08-01).
        """
        for path in (f"/session/{self.session_id}/wda/activeAppInfo",
                     "/wda/activeAppInfo"):
            try:
                v = self._req("GET", path, timeout=10).get("value") or {}
                bid = v.get("bundleId")
                if bid:
                    return bid
            except Exception:
                continue
        return None

    def launch_app(self, bundle_id: str, attempts: int = 3) -> None:
        """Launch an app, tolerating the terminate→launch race.

        A cold open terminates first; if the launch lands while the app is still
        tearing down, WDA answers 400 and the whole run dies at step one (this
        killed verify on Robert's phone, 2026-08-04 — the same launch succeeds
        on its own). Retry, then accept success if the app is foreground anyway.
        """
        last: Optional[Exception] = None
        for i in range(attempts):
            try:
                self._req("POST", f"/session/{self.session_id}/wda/apps/launch",
                          {"bundleId": bundle_id}, timeout=45)
                return
            except Exception as e:                                # noqa: BLE001
                last = e
                if self.active_app() == bundle_id:
                    return                    # it came up regardless — good enough
                time.sleep(1.5 * (i + 1))
        raise last if last else RuntimeError("launch failed")

    def terminate_app(self, bundle_id: str) -> None:
        self._req("POST", f"/session/{self.session_id}/wda/apps/terminate",
                  {"bundleId": bundle_id})


class LaneBusy(RuntimeError):
    """Another driver already owns this phone's lane. Driving anyway would
    interleave taps into somebody else's session — refuse instead."""


class LaneLock:
    """Whole-machine mutex for one phone's WDA lane (port 8100 is per-Mac, so
    two sessions driving one phone WILL interleave taps).

    A lock file names the owning pid; a lock whose pid is gone is stale and
    gets reclaimed. This is the operational "one driver at a time" rule made
    structural — it is what stops a calibration run from stomping a warm-up
    run that another session is driving.
    """

    def __init__(self, udid: str, root: Optional[Path] = None, owner: str = "autowarmer"):
        root = Path(root or Path(__file__).resolve().parents[1] / "state")
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"lane-{udid[-8:]}.lock"
        self.owner, self.udid = owner, udid
        self.held = False

    def holder(self) -> Optional[dict]:
        """The live holder's record, or None (also clears a stale file)."""
        try:
            rec = json.loads(self.path.read_text())
        except Exception:
            return None
        pid = int(rec.get("pid", 0))
        if pid and pid != os.getpid():
            try:
                os.kill(pid, 0)                      # signal 0 = liveness probe
            except OSError:
                self.path.unlink(missing_ok=True)    # stale — owner died
                return None
        elif pid == os.getpid():
            return rec
        return rec

    def acquire(self) -> None:
        h = self.holder()
        if h and int(h.get("pid", 0)) != os.getpid():
            raise LaneBusy(
                f"phone {self.udid[-8:]} is being driven by {h.get('owner')} "
                f"(pid {h.get('pid')}, since {h.get('started')}). Stop that run "
                f"first — two drivers on one phone interleave taps.")
        self.path.write_text(json.dumps(
            {"pid": os.getpid(), "owner": self.owner, "udid": self.udid,
             "started": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        self.held = True

    def release(self) -> None:
        if self.held:
            try:
                if json.loads(self.path.read_text()).get("pid") == os.getpid():
                    self.path.unlink(missing_ok=True)
            except Exception:
                pass
            self.held = False


class Driver:
    """Brings a device lane up (runner + port forward) for live driving.

    NOT used by doctor/plan. Kept minimal and honest: it reuses go-ios to run
    the installed WDA runner and iproxy to forward 8100. Collision-aware.
    """

    def __init__(self, cfg: Config, exclusive: bool = True, owner: str = "autowarmer"):
        self.cfg = cfg
        self._procs: List[subprocess.Popen] = []
        self.wda = WDA(cfg.wda_host_port, screen_hint=cfg.screen_points,
                       cache_path=_SCREEN_CACHE_DIR / f"{cfg.udid}.json")
        self.exclusive = exclusive
        self.lock = LaneLock(cfg.udid, owner=owner)

    def _kernel_rsd(self) -> Optional[Tuple[str, int]]:
        """RSD (address, port) for this device from a running go-ios KERNEL
        tunnel, or None. This is the iOS-26 no-password lane: a root go-ios
        kernel tunnel (run ONCE as a LaunchDaemon) exposes a *routable* RSD per
        device; pymobiledevice3 `--rsd HOST PORT` then launches WDA with NO
        per-run prompt. (pymobiledevice3's own tunneld can't tunnel iOS 26 on
        py3.9 — QUIC was removed; go-ios does TCP natively. go-ios *userspace*
        tunnels aren't routable — 'no route to host' — so it must be kernel.)"""
        try:
            out = subprocess.run([self.cfg.ios_binary, "tunnel", "ls"],
                                 capture_output=True, text=True, timeout=10).stdout
            for t in json.loads(out):
                if t.get("udid") == self.cfg.udid and not t.get("userspaceTun", False):
                    addr, port = t.get("address"), t.get("rsdPort")
                    if addr and port:
                        return str(addr), int(port)
        except Exception:
            pass
        return None

    def ensure_lane(self, wait_s: int = 120) -> bool:
        """Bring up the WDA lane on iOS 17+/26.

        go-ios `runwda` cannot keep the XCUITest test resident on iOS 26.
        pymobiledevice3's `developer dvt xcuitest` DOES. On iOS 17+ that needs a
        RemoteXPC tunnel, whose creation needs ROOT (hence the per-run password
        prompt). To avoid the prompt at scale we run a root go-ios KERNEL tunnel
        ONCE (LaunchDaemon); every device then has a routable RSD and we launch
        via `--rsd HOST PORT` with no prompt. If no kernel tunnel is up we fall
        back to the direct `--udid` path (prompts for root).
        go-ios `forward` maps device:8100 -> host once WDA binds.
        """
        if self.exclusive:
            # Somebody else's lane already answering on 8100 (another session,
            # means this phone is being driven RIGHT NOW. Reusing it
            # would interleave our taps into their run — refuse loudly instead.
            if self.wda.reachable() and self.lock.holder() is None:
                raise LaneBusy(
                    f"WDA is already serving on port {self.cfg.wda_host_port} and "
                    "autowarmer didn't start it — another session is driving "
                    "this phone. Stop that run first (one driver per phone).")
            self.lock.acquire()                  # raises LaneBusy if held
        if self.wda.reachable():
            return True
        env = dict(os.environ)
        env["PATH"] = os.path.expanduser("~/Library/Python/3.9/bin") + ":" + env.get("PATH", "")
        # 1) launch + hold the runner via pymobiledevice3.
        rsd = self._kernel_rsd()
        if rsd:                              # kernel tunnel up -> no root prompt
            xcuitest = [self.cfg.pymobiledevice3_binary, "developer", "dvt", "xcuitest",
                        self.cfg.wda_bundle_id, "--rsd", rsd[0], str(rsd[1])]
        else:                                # direct -> may prompt for root
            xcuitest = [self.cfg.pymobiledevice3_binary, "developer", "dvt", "xcuitest",
                        self.cfg.wda_bundle_id, "--udid", self.cfg.udid]
        self._procs.append(subprocess.Popen(
            xcuitest, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env))
        # 2) forward device:8100 -> host once the server is coming up.
        self._procs.append(subprocess.Popen(
            [self.cfg.ios_binary, "forward", str(self.cfg.wda_host_port), "8100",
             "--udid=" + self.cfg.udid],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for _ in range(wait_s):
            if self.wda.reachable():
                return True
            time.sleep(1)
        self.lock.release()                      # never hold a lock for a dead lane
        return False

    def force_teardown(self) -> bool:
        """Kill THIS device's lane helpers (for a clean retry after a wedge).

        Scoped on purpose: a blanket `pkill -f "dvt xcuitest"` reaps every
        phone's runner on the machine — including a lane another phone (or
        another session) is mid-run on. We match only helpers carrying this
        device's udid, or its kernel-tunnel RSD address (the `--rsd` launch
        form doesn't carry the udid on its command line).

        Refuses entirely when another process owns this phone's lane.
        Returns True if it was safe to proceed.
        """
        h = self.lock.holder()
        if h and int(h.get("pid", 0)) != os.getpid():
            return False                      # someone else's lane — hands off
        pats = [f"forward {self.cfg.wda_host_port} .*{self.cfg.udid}",
                f"xcuitest.*{self.cfg.udid}"]
        rsd = self._kernel_rsd()
        if rsd:
            pats.append(f"xcuitest.*{rsd[0]} {rsd[1]}")
        for pat in pats:
            subprocess.run(["pkill", "-f", pat], capture_output=True)
        self.close()
        time.sleep(2)
        return True

    def close(self) -> None:
        for p in self._procs:
            try:
                p.terminate()
            except Exception:
                pass
        self._procs.clear()
        self.lock.release()
