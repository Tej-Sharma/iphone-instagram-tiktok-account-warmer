"""The AutoWarmer dashboard — a local page and the small API behind it.

Binds 127.0.0.1 only. There is no login because there is nothing to log in to:
the server is a process on the user's own Mac, reachable only from that Mac,
holding data that is already on its disk. Binding wider would turn a private
tool into an open remote control of someone's phones, so it doesn't.

Every phone-driving action is handed to `jobs.Runner`, which runs it as a child
process — the UI stays responsive, and a wedged run can always be stopped.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import APP_NAME, __version__
from . import setup_flow as S
from .jobs import Busy, Runner

ROOT = Path(__file__).resolve().parents[1]
UI = Path(__file__).resolve().parent / "ui.html"
STATE = ROOT / "state"

_probe_cache: dict = {"at": 0.0, "rows": None, "key": None}
PROBE_TTL = 20.0            # `ios apps` is slow; don't run it on every poll


def device_rows(cfg: dict, force: bool = False) -> list:
    """Per-phone live state: connected, runner installed, iOS version."""
    key = json.dumps([d.get("udid") for d in cfg.get("devices", [])])
    if (not force and _probe_cache["rows"] is not None
            and _probe_cache["key"] == key
            and time.time() - _probe_cache["at"] < PROBE_TTL):
        return _probe_cache["rows"]

    live = {d["udid"]: d for d in S.list_devices(cfg.get("ios_binary", "ios"))}
    rows = []
    for dev in cfg.get("devices", []):
        udid = dev.get("udid", "")
        here = live.get(udid)
        row = {"udid": udid, "name": dev.get("name") or udid[-8:],
               "connected": here is not None,
               "device_name": (here or {}).get("name", ""),
               "ios": (here or {}).get("ios", ""),
               "model": (here or {}).get("model", ""),
               "accounts": dev.get("accounts", []),
               "runner_installed": None}
        if here is not None:
            row["runner_installed"] = _runner_installed(cfg, dev)
        rows.append(row)
    _probe_cache.update(at=time.time(), rows=rows, key=key)
    return rows


def _runner_installed(cfg: dict, dev: dict) -> Optional[bool]:
    """Is our runner on the phone? None when the question couldn't be asked."""
    import subprocess
    bundle = (dev.get("wda_bundle_id") or cfg.get("wda_bundle_id") or "")
    if not bundle:
        return None
    try:
        out = subprocess.run([S._expand(cfg["ios_binary"]), "apps",
                              "--udid", dev["udid"]],
                             capture_output=True, text=True, timeout=45).stdout
    except Exception:
        return None
    return bundle.replace(".xctrunner", "") in out


def account_rows(config_path: Path) -> list:
    """Where every account sits on its ramp — day, phase, today's rates."""
    try:
        from ._core import Config, Engine
    except Exception:
        return []
    rows = []
    raw = S.load_config(config_path) or {}
    names = {d.get("udid"): d.get("name", "") for d in raw.get("devices", [])}
    try:
        configs = Config.load_all(str(config_path))
    except Exception:
        return []
    for cfg in configs:
        try:
            for r in Engine(cfg, STATE).status():
                rows.append({**r, "udid": cfg.udid,
                             "device": names.get(cfg.udid, cfg.udid[-8:])})
        except Exception as e:                          # noqa: BLE001
            rows.append({"udid": cfg.udid, "device": names.get(cfg.udid, ""),
                         "error": f"{type(e).__name__}: {e}"})
    return rows


def run_history(limit: int = 25) -> list:
    """Recent warm runs, newest first, from the recorded run directories."""
    runs = STATE / "runs"
    if not runs.is_dir():
        return []
    items = []
    for d in runs.iterdir():
        try:
            s = json.loads((d / "summary.json").read_text())
        except Exception:
            continue
        if not s.get("account"):
            continue
        items.append({"run": d.name, "when": s.get("started", ""),
                      "account": s.get("account"), "platform": s.get("platform"),
                      "outcome": s.get("outcome", "?"), "detail": s.get("detail", ""),
                      "duration_s": s.get("duration_s"), "counts": s.get("counts", {})})
    items.sort(key=lambda x: x["when"], reverse=True)
    return items[:limit]


class Handler(BaseHTTPRequestHandler):
    config_path: Path = ROOT / "config.json"
    runner: Runner = Runner(ROOT)

    # ---- plumbing
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                       # the page navigated away mid-poll

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _q(self) -> dict:
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    # ---- reads
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            cfg = S.load_config(self.config_path)
            configured = S.is_configured(cfg)
            active = self.runner.active()
            payload = {
                "app": APP_NAME, "version": __version__,
                "configured": configured,
                "config": cfg or {},
                "config_path": str(self.config_path),
                "problems": S.validate_config(cfg) if cfg else [],
                "active": active.snapshot() if active else None,
                "jobs": self.runner.recent(),
            }
            if configured:
                payload["devices"] = device_rows(cfg,
                                                 force=self._q().get("probe") == "1")
                payload["accounts"] = account_rows(self.config_path)
                payload["runs"] = run_history()
            else:
                payload["detected"] = S.detect_all()
            return self._json(200, payload)
        if path == "/api/detect":
            return self._json(200, {"detected": S.detect_all()})
        if path == "/api/devices":
            q = self._q()
            cfg = S.load_config(self.config_path) or {}
            binary = q.get("ios") or cfg.get("ios_binary", "ios")
            return self._json(200, {"devices": S.list_devices(binary)})
        if path == "/api/job":
            q = self._q()
            return self._json(200, self.runner.tail(q.get("id", ""),
                                                    int(q.get("since", 0))))
        return self._send(404, b"not found", "text/plain")

    # ---- writes
    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
        except Exception:
            return self._json(400, {"error": "malformed request"})

        if path == "/api/check-path":
            return self._json(200, S.check_path(body.get("name", ""),
                                                body.get("path", "")))
        if path == "/api/config":
            cfg = S.build_config(body.get("binaries", {}), body.get("signing", {}),
                                 body.get("devices", []),
                                 existing=S.load_config(self.config_path))
            problems = S.validate_config(cfg)
            if problems and not body.get("force"):
                return self._json(400, {"problems": problems})
            S.save_config(self.config_path, cfg)
            _probe_cache.update(at=0.0, rows=None, key=None)
            # tell the user where their Apple key ended up — it moved
            note = S.store_p8(cfg["signing"]["p8_path"])["note"]
            return self._json(200, {"saved": True, "config": cfg,
                                    "problems": problems, "key_note": note})
        if path == "/api/run":
            kind = body.get("kind", "")
            try:
                job = self._start(kind, body)
            except Busy as e:
                return self._json(409, {"error": str(e)})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, job.snapshot())
        if path == "/api/stop":
            return self._json(200, {"stopped": self.runner.stop(body.get("id"))})
        return self._send(404, b"not found", "text/plain")

    def _start(self, kind: str, body: dict):
        cfgp = ["--config", str(self.config_path)]
        if kind in ("warm", "practice"):
            handle = (body.get("handle") or "").strip().lstrip("@")
            if not handle:
                raise ValueError("which account?")
            args = [*cfgp, "warm", handle, "--live"]
            if body.get("platform"):
                args += ["--platform", body["platform"]]
            if kind == "practice":
                args.append("--no-engage")
            label = f"@{handle}" + (" (practice)" if kind == "practice" else "")
            return self.runner.start(kind, label, args)
        if kind == "plan":
            handle = (body.get("handle") or "").strip().lstrip("@")
            if not handle:
                raise ValueError("which account?")
            return self.runner.start(kind, f"@{handle}", [*cfgp, "warm", handle])
        if kind == "warm-all":
            args = [*cfgp, "warm-all"]
            if body.get("practice"):
                args.append("--no-engage")
            return self.runner.start(kind, "every phone", args)
        if kind == "onboard":
            udid = body.get("udid", "")
            if not udid:
                raise ValueError("which phone?")
            return self.runner.start(kind, udid[-8:], [*cfgp, "onboard", udid])
        if kind == "install":
            tool = body.get("tool", "")
            if tool not in ("ios", "pymobiledevice3", "ocr", "all"):
                raise ValueError(f"{tool or 'that'} has to be installed by hand")
            return self.runner.start(kind, tool, [*cfgp, "install", tool])
        if kind == "doctor":
            return self.runner.start(kind, "this Mac", [*cfgp, "doctor"])
        raise ValueError(f"unknown action {kind!r}")

    def log_message(self, *a):          # the terminal belongs to the user
        pass


def serve(config_path: Optional[str] = None, port: int = 8790,
          open_browser: bool = True) -> None:
    if config_path:
        Handler.config_path = Path(config_path)
    Handler.runner = Runner(ROOT)
    for attempt in range(20):           # a stale tab may still hold the port
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port + attempt), Handler)
            break
        except OSError:
            continue
    else:
        print(f"could not find a free port near {port}")
        return
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"{APP_NAME} {__version__} — {url}")
    print("This runs entirely on your Mac. Press Ctrl-C to stop.")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        job = Handler.runner.active()
        if job is not None:
            Handler.runner.stop(job.id)
