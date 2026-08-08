"""Run recorder — structured, reviewable traces of a live warm session.

The point: make every live run leave behind an explicit, machine- AND human-
readable record so a failure can be *diagnosed from the artifacts alone* — what
screen were we on, what did the UI/OCR say, what action ran, did it succeed, and
if not, the exact error. That closes the loop: run -> read the trace -> fix ->
repeat, until the whole warm-up drives clean.

Per run we write a directory under state/runs/<platform>__<account>__<ts>/:
  events.jsonl   one JSON object per step (seq, action, status, how, error, ...)
  NN_<action>.png   a screenshot of the screen for that step
  NN_<action>.xml   (on failure) the WDA accessibility tree, when cheap to grab
  summary.json      counts + timing + outcome
  report.md         a human table + failure details (Read this to iterate)

Capture policy is set by `level`:
  "full"   screenshot every step (dev / calibration — see everything)
  "errors" screenshot + OCR + a11y only when a step fails (lean, prod-ish)
  "off"    events.jsonl only, no images

Tracing adds a screenshot per step in "full" mode (~cheap via WDA), so the fast
warm loop should run "errors" in production and "full" while we're still fixing.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:24] or "step"


def _argrepr(arg) -> Optional[str]:
    if arg is None:
        return None
    s = repr(arg)
    return s[:80]


@dataclass
class _StepHandle:
    """Handed to the `with` body so it can annotate the step as it runs."""
    how: Optional[str] = None          # e.g. "double-tap" / "tree" / "fallback"
    note: str = ""
    status: Optional[str] = None       # override to "skip" / "ok"; None = auto
    extra: dict = field(default_factory=dict)

    def ok(self, how: str = None):
        if how is not None:
            self.how = how

    def skip(self, note: str = ""):
        self.status = "skip"
        if note:
            self.note = note


class RunRecorder:
    def __init__(self, runs_root: Path, account: str, platform: str,
                 level: str = "full", perceptor=None):
        self.level = level
        self.account = account
        self.platform = platform
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = Path(runs_root) / f"{platform}__{account.lstrip('@')}__{ts}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        # fleet-wide, always-at-the-same-path live feed: every step from every
        # account streams here as it happens, so `tail -f state/live.log` shows
        # exactly what the fleet is doing right now (per-run events.jsonl needs
        # the run dir; this is the single place to watch mid-flight).
        self.live_path = Path(runs_root).parent / "live.log"
        self.seq = 0
        self.wda = None
        self.p = perceptor
        self.counts: Counter = Counter()
        self.consecutive_fails = 0
        self._t0 = time.time()
        self._started = _now_iso()
        self._events: List[dict] = []
        self.outcome = "incomplete"
        self.log(f"run start · {account} ({platform}) · level={level}")

    def bind(self, wda) -> None:
        self.wda = wda

    # -- narration (non-step) ---------------------------------------------
    def log(self, message: str) -> None:
        self._write({"seq": None, "ts": _now_iso(), "kind": "log", "message": message})

    # -- the core: a recorded, timed, failure-capturing step --------------
    @contextmanager
    def step(self, action: str, arg=None, phase: Optional[str] = None,
             critical: bool = False, capture: Optional[bool] = None):
        self.seq += 1
        seq = self.seq
        t0 = time.time()
        rec = {"seq": seq, "ts": _now_iso(), "kind": "step", "phase": phase,
               "action": action, "arg": _argrepr(arg), "status": "ok",
               "how": None, "note": "", "error": None, "trace": None,
               "duration_ms": 0, "screenshot": None, "source": None, "ocr": None}
        h = _StepHandle()
        err = None
        try:
            yield h
        except Exception as e:                       # noqa: BLE001 — we log everything
            rec["status"] = "fail"
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["trace"] = traceback.format_exc(limit=4)
            err = e
        # let the body override status (e.g. skip) when it didn't throw
        if err is None and h.status:
            rec["status"] = h.status
        h.status = rec["status"]          # expose the FINAL status to the caller
        rec["how"] = h.how
        rec["note"] = h.note
        if h.extra:
            rec.update({k: v for k, v in h.extra.items() if k not in rec})
        rec["duration_ms"] = int((time.time() - t0) * 1000)

        self.counts[rec["status"]] += 1
        if rec["status"] == "fail":
            self.consecutive_fails += 1
        else:
            self.consecutive_fails = 0

        # capture policy
        want_shot = capture if capture is not None else (
            self.level == "full" or (self.level == "errors" and rec["status"] == "fail"))
        # only do the *rich* (OCR + a11y) capture on the FIRST failure of a streak,
        # so a wedged WDA doesn't cost 3×(screenshot+ocr+source) of hung timeouts.
        rich = rec["status"] == "fail" and self.consecutive_fails == 1
        if want_shot and self.wda is not None:
            rec["screenshot"] = self._shot(seq, action)
            if rich:
                rec["ocr"] = self._ocr(rec["screenshot"])
                rec["source"] = self._dump_source(seq, action)
        self._write(rec)
        if err is not None and critical:
            raise err

    # -- artifact capture --------------------------------------------------
    def _shot(self, seq: int, action: str) -> Optional[str]:
        name = f"{seq:02d}_{_slug(action)}.png"
        try:
            if self.wda.screenshot(str(self.dir / name)):
                return name
        except Exception:
            pass
        return None

    def _ocr(self, shot_name: Optional[str]) -> Optional[str]:
        if not shot_name or self.p is None:
            return None
        try:
            obs = self.p.ocr(str(self.dir / shot_name))
            return " | ".join(o.text for o in obs if getattr(o, "text", "").strip())[:1200]
        except Exception:
            return None

    def _dump_source(self, seq: int, action: str) -> Optional[str]:
        """Grab the a11y tree on failure. Guarded: TikTok's source can hang, so
        we rely on the WDA idle-wait settings already applied; still best-effort."""
        if self.wda is None:
            return None
        name = f"{seq:02d}_{_slug(action)}.xml"
        try:
            xml = self.wda.source()
            if xml:
                (self.dir / name).write_text(xml)
                return name
        except Exception:
            pass
        return None

    def snapshot(self, tag: str, ocr: bool = False) -> Optional[str]:
        """Explicit screen capture outside a step (e.g. landing screens)."""
        self.seq += 1
        name = self._shot(self.seq, tag)
        rec = {"seq": self.seq, "ts": _now_iso(), "kind": "snapshot",
               "action": tag, "status": "ok", "screenshot": name,
               "ocr": self._ocr(name) if ocr else None}
        self._write(rec)
        return name

    # -- finish ------------------------------------------------------------
    def finish(self, outcome: str, detail: str = "") -> Path:
        self.outcome = outcome
        dur = round(time.time() - self._t0, 1)
        c = dict(self.counts)
        # Per-account run-END line in the fleet feed, so `watch`/live.log shows
        # each account's final verdict (not just its steps). Mirrors the
        # "run start" line emitted in __init__ — one clean start/end pair per
        # account on BOTH the manual (warm-all) and scheduled paths.
        self.log(f"run end · {outcome}"
                 + (f" ({detail})" if detail else "")
                 + f" · ok={c.get('ok', 0)} fail={c.get('fail', 0)} "
                   f"skip={c.get('skip', 0)} · {dur}s")
        summary = {
            "account": self.account, "platform": self.platform,
            "started": self._started, "ended": _now_iso(),
            "duration_s": dur,
            "outcome": outcome, "detail": detail,
            "steps": self.seq, "counts": dict(self.counts),
            "fails": [e for e in self._events
                      if e.get("status") == "fail"],
        }
        (self.dir / "summary.json").write_text(json.dumps(summary, indent=2))
        (self.dir / "report.md").write_text(_render_report(summary, self._events))
        self._prune()
        return self.dir

    def _prune(self, keep: int = 10) -> None:
        """Keep only the last `keep` run dirs for THIS account — screenshots +
        a11y dumps add up fast across daily runs, so old runs are removed once
        the account has more than `keep` recorded. Dir names are timestamped, so
        lexical sort == chronological."""
        import shutil
        prefix = f"{self.platform}__{self.account.lstrip('@')}__"
        runs = sorted(d for d in self.dir.parent.glob(prefix + "*") if d.is_dir())
        for old in runs[:-keep]:
            try:
                shutil.rmtree(old)
            except Exception:
                pass

    # -- io ----------------------------------------------------------------
    def _write(self, rec: dict) -> None:
        self._events.append(rec)
        with self.events_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        self._live(rec)

    def _live(self, rec: dict) -> None:
        """Append one human-readable line to the fleet-wide live feed and flush
        immediately, so a `tail -f state/live.log` reflects each action in real
        time. Best-effort — never let logging break a run."""
        try:
            t = _dt.datetime.now().strftime("%H:%M:%S")
            who = f"{self.platform[:2]} @{self.account}"
            if rec.get("kind") == "log":
                line = f"{t}  {who:22} · {rec.get('message', '')}"
            else:
                arg = rec.get("arg") or ""
                args = f"[{arg}]" if arg else ""
                detail = rec.get("error") or rec.get("how") or rec.get("note") or ""
                line = (f"{t}  {who:22} {rec.get('action', ''):11}{args:14} "
                        f"{rec.get('status', ''):5} {detail}")
            with self.live_path.open("a") as f:
                f.write(line[:220] + "\n")
                f.flush()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Review helpers (read a past run — this is what I read to iterate)
# --------------------------------------------------------------------------

def load_run(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    events = []
    ep = run_dir / "events.jsonl"
    if ep.exists():
        for line in ep.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
    summary = {}
    sp = run_dir / "summary.json"
    if sp.exists():
        summary = json.loads(sp.read_text())
    return {"dir": str(run_dir), "events": events, "summary": summary}


def list_runs(runs_root: Path, account: Optional[str] = None, limit: int = 20) -> List[Path]:
    root = Path(runs_root)
    if not root.exists():
        return []
    dirs = [d for d in root.iterdir() if d.is_dir()]
    if account:
        a = account.lstrip("@")
        dirs = [d for d in dirs if f"__{a}__" in d.name]
    return sorted(dirs, key=lambda d: d.name, reverse=True)[:limit]


def latest_run(runs_root: Path, account: Optional[str] = None) -> Optional[Path]:
    runs = list_runs(runs_root, account, limit=1)
    return runs[0] if runs else None


def summarize_run(run_dir: Path) -> str:
    data = load_run(run_dir)
    s, evs = data["summary"], data["events"]
    lines = [f"run: {Path(run_dir).name}"]
    if s:
        c = s.get("counts", {})
        lines.append(f"outcome: {s.get('outcome')}  ({s.get('detail','')})".rstrip())
        lines.append(f"steps: {s.get('steps')}  ok={c.get('ok',0)} "
                     f"fail={c.get('fail',0)} skip={c.get('skip',0)}  "
                     f"{s.get('duration_s')}s")
    lines.append("")
    for e in evs:
        if e.get("kind") == "step":
            mark = {"ok": "✓", "fail": "✗", "skip": "·"}.get(e["status"], "?")
            row = (f"  {mark} {e['seq']:>2} {e['action']:<9} "
                   f"{(e.get('how') or ''):<12} {e['duration_ms']:>5}ms")
            if e.get("arg"):
                row += f" arg={e['arg']}"
            if e["status"] == "fail":
                row += f"  ERROR {e.get('error')}"
            if e.get("screenshot"):
                row += f"  [{e['screenshot']}]"
            lines.append(row)
        elif e.get("kind") == "log":
            lines.append(f"    · {e['message']}")
    return "\n".join(lines)


def _render_report(summary: dict, events: List[dict]) -> str:
    c = summary.get("counts", {})
    out = [f"# autowarmer run — {summary.get('account')} ({summary.get('platform')})",
           "",
           f"- **outcome:** {summary.get('outcome')} {summary.get('detail','')}".rstrip(),
           f"- **steps:** {summary.get('steps')} · ok {c.get('ok',0)} · "
           f"fail {c.get('fail',0)} · skip {c.get('skip',0)}",
           f"- **duration:** {summary.get('duration_s')}s "
           f"({summary.get('started')} → {summary.get('ended')})",
           "",
           "| # | action | status | how | ms | screenshot | error |",
           "|--:|---|---|---|--:|---|---|"]
    for e in events:
        if e.get("kind") != "step":
            continue
        out.append(f"| {e['seq']} | {e['action']} | {e['status']} | "
                   f"{e.get('how') or ''} | {e['duration_ms']} | "
                   f"{e.get('screenshot') or ''} | {(e.get('error') or '').replace('|','/')} |")
    fails = [e for e in events if e.get("status") == "fail"]
    if fails:
        out += ["", "## Failures (diagnose these)", ""]
        for e in fails:
            out.append(f"### step {e['seq']} · {e['action']}")
            out.append(f"- error: `{e.get('error')}`")
            if e.get("screenshot"):
                out.append(f"- screenshot: `{e['screenshot']}`")
            if e.get("source"):
                out.append(f"- a11y tree: `{e['source']}`")
            if e.get("ocr"):
                out.append(f"- OCR: {e['ocr'][:600]}")
            if e.get("trace"):
                out.append("```\n" + e["trace"].strip() + "\n```")
            out.append("")
    return "\n".join(out)
