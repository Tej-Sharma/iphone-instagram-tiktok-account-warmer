"""Run warm-ups as child processes, stream their output to the dashboard.

Two decisions worth stating, because both are load-bearing:

**Child process, not a thread.** A warm run drives a phone over a WebDriver
session that can wedge; in-process that takes the dashboard down with it, and
"stop" becomes impossible. A child process can always be killed, and its death
never touches the UI.

**One run at a time.** The phones share one driving lane per Mac, so a second
concurrent run would interleave taps on the same device. Starting a run while
one is active is refused, not queued — the caller sees why.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

MAX_LINES = 4000          # ring buffer per job; a long warm-all stays bounded


class Busy(RuntimeError):
    """Another run is already driving the phones."""


class Job:
    def __init__(self, jid: str, kind: str, label: str, argv: List[str]):
        self.id = jid
        self.kind = kind                  # warm | warm-all | onboard | doctor
        self.label = label                # human summary, e.g. "@handle"
        self.argv = argv
        self.status = "running"           # running | done | failed | stopped
        self.started = time.time()
        self.ended: Optional[float] = None
        self.returncode: Optional[int] = None
        self.lines: deque = deque(maxlen=MAX_LINES)
        self.dropped = 0                  # lines aged out of the ring buffer
        self.proc: Optional[subprocess.Popen] = None

    def snapshot(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "status": self.status, "started": self.started,
                "ended": self.ended, "returncode": self.returncode,
                "elapsed": round((self.ended or time.time()) - self.started, 1),
                "line_count": self.dropped + len(self.lines)}


class Runner:
    """Owns the single active job and the recent history."""

    def __init__(self, root: Path, python: Optional[str] = None,
                 history: int = 20):
        self.root = Path(root)
        self.python = python or sys.executable
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._active: Optional[str] = None
        self._lock = threading.Lock()
        self._history = history
        self._seq = 0

    # ---- lifecycle
    def active(self) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(self._active) if self._active else None

    def start(self, kind: str, label: str, args: List[str]) -> Job:
        with self._lock:
            cur = self._jobs.get(self._active) if self._active else None
            if cur is not None and cur.status == "running":
                raise Busy(f"{cur.kind} {cur.label} is still running — "
                           "stop it first, or wait for it to finish")
            self._seq += 1
            jid = f"j{self._seq}-{int(time.time())}"
            argv = [self.python, "-m", "autowarmer", *args]
            job = Job(jid, kind, label, argv)
            self._jobs[jid] = job
            self._order.append(jid)
            self._active = jid
            self._prune()

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        # the repo layout needs the parent dir importable for `autowarmer`;
        # in a packaged build this is simply the install root
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        try:
            job.proc = subprocess.Popen(
                job.argv, cwd=str(self.root), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True)     # own group, so stop kills children
        except OSError as e:
            job.status, job.ended = "failed", time.time()
            job.lines.append(f"could not start: {e}")
            return job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def _pump(self, job: Job) -> None:
        assert job.proc is not None and job.proc.stdout is not None
        for line in job.proc.stdout:
            if len(job.lines) == job.lines.maxlen:
                job.dropped += 1
            job.lines.append(line.rstrip("\n"))
        job.returncode = job.proc.wait()
        job.ended = time.time()
        if job.status != "stopped":
            job.status = "done" if job.returncode == 0 else "failed"

    def stop(self, jid: Optional[str] = None) -> bool:
        """Terminate a run. The phone is left as-is — the next run's setup
        tears the lane down and cold-relaunches the app anyway."""
        with self._lock:
            job = self._jobs.get(jid or self._active or "")
        if job is None or job.proc is None or job.status != "running":
            return False
        job.status = "stopped"
        try:
            os.killpg(os.getpgid(job.proc.pid), signal.SIGTERM)
        except OSError:
            try:
                job.proc.terminate()
            except OSError:
                return False
        return True

    # ---- reads
    def get(self, jid: str) -> Optional[Job]:
        return self._jobs.get(jid)

    def tail(self, jid: str, since: int = 0) -> dict:
        job = self._jobs.get(jid)
        if job is None:
            return {"error": "no such job"}
        lines = list(job.lines)
        start = max(0, since - job.dropped)
        return {**job.snapshot(), "from": job.dropped + start,
                "lines": lines[start:]}

    def recent(self) -> List[dict]:
        return [self._jobs[j].snapshot() for j in reversed(self._order)
                if j in self._jobs]

    def _prune(self) -> None:
        while len(self._order) > self._history:
            old = self._order.pop(0)
            job = self._jobs.get(old)
            if job is not None and job.status == "running":
                self._order.insert(0, old)      # never drop a live job
                break
            self._jobs.pop(old, None)
