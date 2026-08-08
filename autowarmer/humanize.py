"""Human-behavior engine — the core of autowarmer.

Everything here is pure (no device, no I/O) so it is fully unit-testable and
deterministic given a seeded random.Random. The design goal, straight from the
research: detection scores the *statistical shape* of behavior, not fixed
volume. So we never emit uniform quotas — every action is an independent
coin-flip, every duration is drawn from a right-skewed distribution, every
delay carries multiplicative jitter, and sessions live inside a circadian
rhythm with a shifting sleep window.
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def coin(rng, p: float) -> bool:
    """Independent per-item probability — the anti-quota primitive.

    A human likes a *variable fraction* of what they scroll and skips freely.
    Making the rate emergent from independent coin-flips reproduces human
    variance and non-round daily totals, which a fixed quota never does.
    """
    return rng.random() < max(0.0, min(1.0, p))


def skewed_watch_time(rng, cap: float = 45.0) -> float:
    """Seconds spent on one video, drawn right-skewed (log-normal).

    Median ~4s (many fast 2-6s skips), a long thin tail toward full watches,
    plus an occasional rewatch. TikTok counts engagement at ~3s, so a slice of
    watches deliberately clear that bar. Never a fixed or flat-uniform value.
    """
    secs = math.exp(rng.normalvariate(1.4, 0.7))          # lognormal, median e^1.4≈4.05
    if rng.random() < 0.08:                                 # occasional rewatch / lingering
        secs *= 1.0 + rng.random()
    return round(min(cap, max(1.2, secs)), 2)


def jittered_delay(rng, base: float, floor: float = 0.15) -> float:
    """A human pause around `base` seconds with multiplicative log-normal jitter.

    Multiplicative (not additive ±) so the variance scales with the interval and
    never collapses to metronomic precision — inter-action variance stays well
    above the ~100ms that clockwork automation betrays.
    """
    return round(max(floor, base * math.exp(rng.normalvariate(0.0, 0.35))), 3)


def tap_point(rng, box: Tuple[float, float, float, float]) -> Tuple[int, int]:
    """A gaussian-scattered tap inside an element box (x, y, w, h).

    Hitting the exact same pixel thousands of times is a documented tell, so we
    scatter around the centre and clamp to a safe inset.
    """
    x, y, w, h = box
    cx, cy = x + w / 2.0, y + h / 2.0
    px = rng.normalvariate(cx, w * 0.18)
    py = rng.normalvariate(cy, h * 0.18)
    px = min(x + w * 0.92, max(x + w * 0.08, px))
    py = min(y + h * 0.92, max(y + h * 0.08, py))
    return int(px), int(py)


def swipe_vector(rng, screen: Tuple[int, int]) -> Tuple[int, int, int, int]:
    """A human 'next video' up-swipe: varied start, distance, and slight x drift."""
    w, h = screen
    x0 = int(rng.normalvariate(w * 0.5, w * 0.06))
    y0 = int(rng.normalvariate(h * 0.72, h * 0.04))
    dist = rng.uniform(h * 0.35, h * 0.55)
    x1 = int(x0 + rng.normalvariate(0, w * 0.03))
    y1 = int(y0 - dist)
    return x0, y0, x1, max(int(h * 0.05), y1)


# --------------------------------------------------------------------------
# Session planning
# --------------------------------------------------------------------------

@dataclass
class Action:
    kind: str                     # open|verify|search|watch|skip|like|save|share|comment|follow|scroll|idle|close
    pre_delay: float = 0.0        # human pause before this action (seconds)
    arg: Optional[object] = None  # e.g. keyword for search, seconds for watch
    note: str = ""

    def __str__(self) -> str:
        a = "" if self.arg is None else f" {self.arg!r}"
        return f"+{self.pre_delay:>5.1f}s  {self.kind:<8}{a}  {self.note}"


@dataclass
class SessionPlan:
    account: str
    platform: str
    mode: str                     # "feed" or "search"
    keyword: Optional[str]
    actions: List[Action] = field(default_factory=list)

    @property
    def est_seconds(self) -> float:
        total = 0.0
        for a in self.actions:
            total += a.pre_delay
            if a.kind in ("watch", "idle", "skip"):
                total += float(a.arg)
        return round(total, 1)

    def pretty(self) -> str:
        head = (f"Session · {self.account} ({self.platform}) · mode={self.mode}"
                + (f" · kw={self.keyword!r}" if self.keyword else "")
                + f" · ~{self.est_seconds/60:.1f} min · {len(self.actions)} actions")
        return head + "\n" + "\n".join("  " + str(a) for a in self.actions)


def plan_session(rng, phase, expected_username: str, keywords: Sequence[str]) -> SessionPlan:
    """Compose one warm-up session from an incubation Phase + human primitives.

    `phase` is any object exposing: videos_min, videos_max, like_rate,
    follow_rate, save_rate, search_mode, max_minutes, inter_action_base,
    platform. (See incubation.Phase.)
    """
    use_search = bool(phase.search_mode and keywords)
    keyword = rng.choice(list(keywords)) if use_search else None
    app = phase.platform

    actions: List[Action] = [
        Action("open", jittered_delay(rng, 1.5), app, "launch app"),
        # We verify it's on the right account before doing anything.
        Action("verify", jittered_delay(rng, 2.0), expected_username, "confirm live account"),
    ]
    if use_search:
        actions.append(Action("search", jittered_delay(rng, 2.5), keyword, "search niche, open results"))

    n = rng.randint(phase.videos_min, phase.videos_max)
    budget = phase.max_minutes * 60.0
    spent = 0.0
    skip_rate = getattr(phase, "skip_rate", 0.30)
    for _ in range(n):
        # a real person swipes past a big fraction of videos almost instantly,
        # without watching or engaging. A skip barely pauses (often <1s total),
        # and the flick comes right after — no full inter-action delay.
        if coin(rng, skip_rate):
            pre = jittered_delay(rng, 0.15, floor=0.05)
            glance = round(rng.uniform(0.15, 1.2), 2)
            spent += pre + glance
            if spent > budget:
                break
            actions.append(Action("skip", pre, glance, "not interested — swipe past"))
            actions.append(Action("scroll", jittered_delay(rng, 0.15, floor=0.05),
                                   None, "fast skip"))
            continue
        pre = jittered_delay(rng, phase.inter_action_base)
        watch = skewed_watch_time(rng)
        spent += pre + watch
        if spent > budget:
            break
        actions.append(Action("watch", pre, watch))
        # occasionally pause to "read the caption" before engaging
        if coin(rng, 0.12):
            actions.append(Action("idle", jittered_delay(rng, 1.0),
                                   round(rng.uniform(0.8, 3.0), 2), "read caption"))
        if coin(rng, phase.like_rate):
            actions.append(Action("like", jittered_delay(rng, 0.6), None))
        if coin(rng, phase.save_rate):
            actions.append(Action("save", jittered_delay(rng, 0.6), None))
        # share / comment are rarer, deeper-engagement signals. comment carries
        # no text here — the engine supplies a curated string (or skips it).
        if coin(rng, getattr(phase, "share_rate", 0.0)):
            actions.append(Action("share", jittered_delay(rng, 0.8), None,
                                   "open share sheet, dismiss"))
        if coin(rng, getattr(phase, "comment_rate", 0.0)):
            actions.append(Action("comment", jittered_delay(rng, 1.3), None,
                                   "leave a short niche comment"))
        if coin(rng, phase.follow_rate):
            actions.append(Action("follow", jittered_delay(rng, 0.9), None,
                                   "open profile, follow, back"))
        actions.append(Action("scroll", jittered_delay(rng, 0.5), None, "next video"))

    # End-of-session human touch. Real users rarely just close the app from the
    # feed — they check stories/DMs first.
    #   Instagram (~55%): view stories (coin-flip through), then open the DM
    #     inbox, scroll, and tap into an incoming conversation to read it.
    #   TikTok (~50%): open the Inbox, scroll activity, and tap into a message.
    if phase.platform == "instagram" and coin(rng, 0.55):
        actions.append(Action("stories", jittered_delay(rng, 1.5), None,
                              "stories, then open + read a DM"))
    elif phase.platform == "tiktok" and coin(rng, 0.5):
        actions.append(Action("inbox", jittered_delay(rng, 1.5), None,
                              "open inbox, scroll, read an incoming message"))

    actions.append(Action("close", jittered_delay(rng, 1.0), None, "leave app"))
    return SessionPlan(expected_username, app, "search" if use_search else "feed", keyword, actions)


# --------------------------------------------------------------------------
# Circadian scheduling — sessions across waking hours with a shifting sleep gap
# --------------------------------------------------------------------------

def daily_schedule(rng, n_sessions: int, sleep_start_h: float = 23.0,
                   sleep_hours: float = 8.0) -> List[float]:
    """Return `n_sessions` fractional-hour times [0,24) inside waking hours.

    The sleep window itself is jittered by the caller's per-day rng so the gap
    shifts day to day; sessions are spread (not clustered) with human spacing.
    Continuous 24/7 activity is the single most-cited detection signal, so the
    contiguous sleep gap is non-negotiable.
    """
    start = (sleep_start_h + rng.uniform(-1.0, 1.0)) % 24
    length = sleep_hours + rng.uniform(-0.75, 0.75)

    def awake(h: float) -> bool:
        end = (start + length) % 24
        if start < end:
            return not (start <= h < end)
        return end <= h < start

    times: List[float] = []
    guard = 0
    while len(times) < n_sessions and guard < 500:
        guard += 1
        h = rng.uniform(0, 24)
        if awake(h) and all(abs(h - t) > 1.5 for t in times):
            times.append(round(h, 2))
    return sorted(times)
