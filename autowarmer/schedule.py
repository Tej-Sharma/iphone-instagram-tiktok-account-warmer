"""3x/day circadian scheduler — warms all connected phones at random times.

Times are deterministic per calendar day (seeded by the date) so a restart
recomputes the same schedule and simply skips slots already passed — no
double-runs, no backfilled burst. Waking-hours only, with a contiguous sleep gap
(humanize.daily_schedule). Run as a user LaunchAgent (KeepAlive); the root
tunneld LaunchDaemon handles tunnels so no password is ever needed at run time.
"""
from __future__ import annotations

import datetime as _dt
import random
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import humanize as H
from . import fleet


def in_window(h: float, win: Tuple[float, float]) -> bool:
    """Is fractional hour `h` inside [start, end)? Handles a window that wraps
    past midnight (e.g. 22 → 2)."""
    s, e = win
    return (s <= h < e) if s < e else (h >= s or h < e)


def day_times(day_ordinal: int, n: Optional[int] = None, sleep_start: float = 23.0,
              sleep_hours: float = 8.0,
              avoid: Optional[List[Tuple[float, float]]] = None,
              windows: Optional[List[Tuple[float, float]]] = None) -> List[float]:
    """Fractional-hour session times for a given day, stable per date.

    Two modes:

    * `windows` given (e.g. [(15,17), (21,24)]): pick exactly ONE random time
      inside EACH window — the explicit "afternoon + night" schedule. Times still
      vary day to day (seeded by the date) but stay inside the chosen bands, and
      still respect `avoid`.
    * `windows` None: the legacy scatter — when `n` is None the COUNT itself is
      randomized (usually 2, some days 3) and times are scattered across waking
      hours with a real sleep gap.

    Seeded by the date, so a scheduler restart recomputes the identical plan and
    simply skips slots already passed — no double-runs, no backfilled burst.

    `avoid` keeps warm sessions OUT of the hours the backend posts in, so a
    warm-up run and a post never contend for the same phone (the lane lock is the
    hard enforcement; this is the cheap avoidance half).
    """
    rng = random.Random(day_ordinal * 7919 + (n or 0))
    if windows:
        times: List[float] = []
        for (s, e) in windows:
            hi = min(e, 23.98)                       # never emit >=24:00 (would never fire)
            if hi <= s:
                continue
            for _ in range(50):                      # one random time in [s, hi)
                cand = rng.uniform(s, hi)
                if avoid and any(in_window(cand, w) for w in avoid):
                    continue
                times.append(cand)
                break
        return sorted(times)
    if n is None:
        n = rng.choice([2, 2, 2, 3])                 # ~2/day, occasionally 3
    times = H.daily_schedule(rng, n, sleep_start, sleep_hours)
    if not avoid:
        return times
    ok = [t for t in times if not any(in_window(t, w) for w in avoid)]
    for _ in range(300):                             # bounded re-draw for the rest
        if len(ok) >= n:
            break
        cand = H.daily_schedule(rng, 1, sleep_start, sleep_hours)[0]
        if any(in_window(cand, w) for w in avoid):
            continue
        if all(abs(cand - t) > 1.5 for t in ok):     # keep human spacing
            ok.append(cand)
    return sorted(ok)


def warm_windows(cfg: dict) -> List[Tuple[float, float]]:
    """Explicit warm-up bands from config: {"warm_windows": [[15,17],[21,24]]}.
    Empty when unset (falls back to the scattered circadian schedule)."""
    raw = (cfg or {}).get("warm_windows")
    if not raw:
        return []
    pairs = raw if isinstance(raw[0], (list, tuple)) else [raw]
    return [(float(a) % 24, float(b) if float(b) == 24 else float(b) % 24)
            for a, b in pairs]


def posting_windows(cfg: dict) -> List[Tuple[float, float]]:
    """Posting hours from config: {"posting_hours": [9, 22]} or a list of
    [start, end] pairs. Empty when unset (no avoidance)."""
    raw = (cfg or {}).get("posting_hours")
    if not raw:
        return []
    pairs = raw if isinstance(raw[0], (list, tuple)) else [raw]
    return [(float(a) % 24, float(b) % 24) for a, b in pairs]


def describe(n: Optional[int] = None,
             avoid: Optional[List[Tuple[float, float]]] = None,
             windows: Optional[List[Tuple[float, float]]] = None) -> List[str]:
    """Today's planned run times as HH:MM strings (for `schedule show`)."""
    times = day_times(_dt.date.today().toordinal(), n, avoid=avoid, windows=windows)
    return [f"{int(t):02d}:{int((t % 1) * 60):02d}" for t in times]


def run_forever(config_path: str, state_root: Path, n_per_day: Optional[int] = None,
                poll_s: int = 300) -> None:
    import json
    try:
        _cfg = json.loads(Path(config_path).read_text())
        avoid = posting_windows(_cfg)
        windows = warm_windows(_cfg)
    except Exception:
        avoid, windows = [], []
    cur_date = None
    times: List[float] = []
    ran: set = set()
    while True:
        now = _dt.datetime.now()
        if now.date() != cur_date:                      # new day (or first start)
            cur_date = now.date()
            times = day_times(cur_date.toordinal(), n_per_day, avoid=avoid,
                              windows=windows)
            cur_h = now.hour + now.minute / 60.0
            ran = {i for i, t in enumerate(times) if t <= cur_h}  # skip already-passed (no backfill)
        cur_h = now.hour + now.minute / 60.0
        for i, t in enumerate(times):
            if t <= cur_h and i not in ran:
                ran.add(i)
                try:
                    fleet.warm_all(config_path, state_root)
                except Exception:                        # a bad session never kills the scheduler
                    pass
        time.sleep(poll_s)


def launchagent_plist(config_path: str) -> str:
    """A user LaunchAgent that keeps the scheduler running (survives logout/reboot)."""
    py = "/usr/bin/python3"
    proj = str(Path(__file__).resolve().parents[1])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.autowarmer.scheduler</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string><string>-m</string><string>autowarmer</string><string>schedule</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>{proj}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/autowarmer-scheduler.log</string>
  <key>StandardErrorPath</key><string>/tmp/autowarmer-scheduler.err</string>
</dict>
</plist>
"""
