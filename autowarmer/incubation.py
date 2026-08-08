"""Incubation state machine — the day-by-day warm-up ramp.

Encodes the MASTER SOP: a cold account must *consume before it engages*, ramp
gradually, and stay feed-mode before it ever search-shapes. Each account has a
persisted state (created date, completed sessions); its current Phase is derived
from how many days it has been warming. Rates are per-video coin-flip
probabilities, not quotas — see humanize.coin / plan_session.

The numbers are the research consensus (community/vendor lore, directionally
reliable, not official). They are deliberately conservative and live in one
place so they are easy to tune per platform.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Phase:
    platform: str
    day: int                    # 1-based warm-up day this phase covers
    name: str
    videos_min: int
    videos_max: int
    like_rate: float            # P(like) per video
    follow_rate: float          # P(follow) per video
    save_rate: float            # P(save/favorite) per video
    search_mode: bool           # feed-mode until the account has aged
    max_minutes: float          # session length ceiling
    sessions_per_day: int
    inter_action_base: float = 1.4
    skip_rate: float = 0.30     # P(quick-skip) per video — glance & swipe past, no engage
    share_rate: float = 0.0     # P(share) per video — rarer, deeper signal
    comment_rate: float = 0.0   # P(comment) per video — highest-risk, gated on a curated pool
    posting_allowed: bool = False
    note: str = ""


# --- Ramp tables -----------------------------------------------------------
# Each entry is (up_to_day, Phase-kwargs). The phase whose up_to_day first
# covers the account's current day wins. Beyond the last entry the account is
# "matured" and holds a steady maintenance phase.

_IG_RAMP = [
    (3,  dict(name="seed/consume",   videos_min=10, videos_max=16, like_rate=0.06,
              follow_rate=0.05, save_rate=0.01, search_mode=False, max_minutes=18,
              sessions_per_day=2, note="days1-3: consume, tiny follows, ~no likes, no post")),
    (7,  dict(name="light-likes",    videos_min=12, videos_max=20, like_rate=0.12,
              follow_rate=0.05, save_rate=0.02, search_mode=False, max_minutes=20,
              sessions_per_day=2, note="days4-7: light likes, first Reel ~day7")),
    (14, dict(name="scale",          videos_min=16, videos_max=26, like_rate=0.18,
              follow_rate=0.06, save_rate=0.03, share_rate=0.02, comment_rate=0.015,
              search_mode=True, max_minutes=24,
              sessions_per_day=3, posting_allowed=True, note="days8-14: scale, search-shape, bio link ~day8-10")),
    (28, dict(name="normalize",      videos_min=18, videos_max=30, like_rate=0.22,
              follow_rate=0.06, save_rate=0.04, share_rate=0.03, comment_rate=0.02,
              search_mode=True, max_minutes=26,
              sessions_per_day=3, posting_allowed=True, note="days15-28: normal creator cadence")),
]
_IG_MATURE = dict(name="maintain", videos_min=15, videos_max=28, like_rate=0.20,
                  follow_rate=0.05, save_rate=0.04, share_rate=0.03, comment_rate=0.02,
                  search_mode=True, max_minutes=24,
                  sessions_per_day=3, posting_allowed=True, note="aged: steady maintenance")

_TT_RAMP = [
    (2,  dict(name="consume-only",   videos_min=14, videos_max=24, like_rate=0.0,
              follow_rate=0.05, save_rate=0.0, search_mode=False, max_minutes=22,
              sessions_per_day=2, inter_action_base=1.2,
              note="days1-2: consume, 0 likes day1, no upload 24h, follow a few")),
    (4,  dict(name="light-engage",   videos_min=18, videos_max=28, like_rate=0.06,
              follow_rate=0.04, save_rate=0.02, search_mode=False, max_minutes=24,
              sessions_per_day=2, inter_action_base=1.2,
              note="days3-4: like ~1 in 15-20, few follows, no churn")),
    (10, dict(name="first-posts",    videos_min=20, videos_max=32, like_rate=0.10,
              follow_rate=0.04, save_rate=0.03, share_rate=0.015, comment_rate=0.01,
              search_mode=True, max_minutes=26,
              sessions_per_day=3, inter_action_base=1.1, posting_allowed=True,
              note="days5-10: first post, 1-2/day >=30min apart, watch niche")),
    (14, dict(name="scale",          videos_min=22, videos_max=34, like_rate=0.14,
              follow_rate=0.04, save_rate=0.03, share_rate=0.025, comment_rate=0.015,
              search_mode=True, max_minutes=28,
              sessions_per_day=3, inter_action_base=1.1, posting_allowed=True,
              note="days8-14: scale, hold link to ~day10-12")),
]
_TT_MATURE = dict(name="maintain", videos_min=20, videos_max=32, like_rate=0.13,
                  follow_rate=0.04, save_rate=0.03, share_rate=0.025, comment_rate=0.015,
                  search_mode=True, max_minutes=26,
                  sessions_per_day=3, inter_action_base=1.1, posting_allowed=True,
                  note="aged: steady maintenance")

_RAMPS = {"instagram": (_IG_RAMP, _IG_MATURE), "tiktok": (_TT_RAMP, _TT_MATURE)}


def phase_for_day(platform: str, day: int) -> Phase:
    platform = platform.lower()
    if platform not in _RAMPS:
        raise ValueError(f"unknown platform: {platform!r} (want instagram|tiktok)")
    ramp, mature = _RAMPS[platform]
    day = max(1, day)
    for up_to, kw in ramp:
        if day <= up_to:
            return Phase(platform=platform, day=day, **kw)
    return Phase(platform=platform, day=day, **mature)


# --- Per-account persisted state ------------------------------------------

@dataclass
class AccountState:
    username: str
    platform: str
    created_at: str                       # ISO date the account started warming
    completed_sessions: int = 0
    last_session_at: Optional[str] = None  # ISO datetime

    def day(self, today: Optional[_dt.date] = None) -> int:
        today = today or _dt.date.today()
        start = _dt.date.fromisoformat(self.created_at)
        return (today - start).days + 1      # day 1 == creation day

    def phase(self, today: Optional[_dt.date] = None) -> Phase:
        return phase_for_day(self.platform, self.day(today))


class Store:
    """Tiny JSON-file-per-account state store."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, platform: str, username: str) -> Path:
        safe = username.replace("/", "_").lstrip("@")
        return self.root / f"{platform}__{safe}.json"

    def load(self, platform: str, username: str) -> Optional[AccountState]:
        p = self._path(platform, username)
        if not p.exists():
            return None
        return AccountState(**json.loads(p.read_text()))

    def load_or_init(self, platform: str, username: str,
                     created_at: Optional[str] = None) -> AccountState:
        st = self.load(platform, username)
        if st is None:
            st = AccountState(username=username, platform=platform,
                              created_at=created_at or _dt.date.today().isoformat())
            self.save(st)
        return st

    def save(self, st: AccountState) -> None:
        self._path(st.platform, st.username).write_text(json.dumps(asdict(st), indent=2))

    def all(self) -> List[AccountState]:
        out = []
        for f in sorted(self.root.glob("*.json")):
            try:
                out.append(AccountState(**json.loads(f.read_text())))
            except Exception:
                pass
        return out
