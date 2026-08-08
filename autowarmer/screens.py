"""Fixed-UI screen map + navigator — deterministic, fast, and safe.

IG and TikTok have a *fixed* chrome: the bottom tab bar never moves, and each
screen has a stable signature. So we don't perceive our way around — we tap known
tab coordinates and cheaply CONFIRM the resulting screen. Two hard rules the last
run violated:

  1. NEVER scroll a screen that isn't a content feed. Scrolling a static profile
     is meaningless repetition and a real ban signal. Every scroll is gated on a
     confirmed feed.
  2. If an action didn't produce the expected screen, ADAPT (re-navigate) or stop
     — never blindly repeat.

Confirmation is deliberately cheap and per-platform:
  * Instagram — the accessibility tree is fast and deterministic. A content feed
    always contains `like-button`; a profile never does. One `source()` check.
  * TikTok — its tree is slow/non-semantic, so we OCR once and look for the feed
    markers ("For You"/"Following") vs profile markers ("Edit profile"). OCR runs
    only at navigation/aborts, never per scroll.
"""
from __future__ import annotations

from typing import Optional

# Bottom-nav tab centres (normalized), captured on-device 2026-07-22 (iPhone SE).
TABS = {
    "instagram": {"home": (0.168, 0.945), "reels": (0.333, 0.945),
                  "search": (0.650, 0.945), "profile": (0.833, 0.945)},
    "tiktok":    {"home": (0.100, 0.955), "friends": (0.300, 0.955),
                  "inbox": (0.700, 0.955), "profile": (0.900, 0.955)},
}
# The consumption surface we warm on (an endless *content* feed, never a profile).
FEED_TAB = {"instagram": "reels", "tiktok": "home"}


# -- cheap, deterministic screen signatures --------------------------------
def ig_is_feed(source_xml: str) -> bool:
    """A content feed (Reels/home) always carries like/comment buttons."""
    return ("like-button" in source_xml) or ("comment-button" in source_xml)


def ig_is_profile(source_xml: str) -> bool:
    return ("profile-tab" in source_xml
            and not ig_is_feed(source_xml))


def tiktok_is_feed(ocr_text: str) -> bool:
    t = ocr_text.lower()
    if "edit profile" in t or "followers" in t:      # profile markers
        return False
    return ("for you" in t) or ("following" in t and "for you" in t)


# Words that SAFELY dismiss an interstitial (close it without committing to
# anything). We tap these; we never tap the affirmative side ("Allow", "Turn on",
# "Continue", "Save", "Delete", "Log out") which could change account settings.
_DISMISS = ("not now", "not now,", "cancel", "close", "dismiss", "skip",
            "maybe later", "later", "no thanks", "no, thanks", "not right now",
            "got it")   # pure acknowledgment (info modals, e.g. TikTok's
                        # Bluetooth privacy sheet) — dismisses, commits nothing
_AVOID = ("allow", "turn on", "log out", "logout", "delete", "remove", "confirm",
          "continue", "save", "open settings", "update")
# DENY controls: they grant nothing, which is exactly our policy (SOP: Contacts
# denied, location off). Checked BEFORE _AVOID, because "Don't allow" contains
# "allow" and would otherwise be vetoed — which is what left TikTok's in-app
# "Find contacts" modal stuck on screen right after a real post (2026-08-02).
_DENY = ("don't allow", "dont allow", "don't allow,", "deny", "not allowed")
# Pure acknowledgment of an INFORMATIONAL sheet (IG's one-time "Video posts are
# now shared as reels"). Only tapped when nothing destructive is on screen —
# "OK" also confirms "Delete post?", so it is gated, not trusted.
_ACK = ("ok", "okay", "continue watching", "retry", "try again")
# A draft prompt ("Continue editing your draft?") is NOT a dismissal choice —
# it decides WHICH video gets posted. "Continue editing" resumes a stale draft
# and would publish the wrong clip, so always take the fresh-start branch.
# (_AVOID already contains "continue", which keeps the wrong one un-tappable.)
_FRESH_START = ("start new video", "start new post", "start new", "new video",
                "new post", "discard draft", "start over")
# Publish-confirmation choices that DECLINE an extra grant while still
# completing the action (IG's "original audio → Meta AI" sheet). Listed so the
# poster can find them; `_AVOID`'s "turn off" would otherwise veto this one.
_CONSERVATIVE_CONFIRM = ("turn off and share",)
_DESTRUCTIVE = ("delete", "discard", "remove", "log out", "logout", "unfollow",
                "block", "report", "clear", "reset", "turn off")


def find_dismiss(obs):
    """Return the Obs of a safe dismiss control on screen, or None.

    Exact-label matches win (a lone "Not Now" button); a substring fallback
    catches "Not now, thanks" etc. Anything whose text also contains an _AVOID
    token is skipped so we never tap a committing action by accident.
    """
    def clean(o):
        return o.text.strip().lower().strip(".")

    screen = " ".join(clean(o) for o in obs)
    # Only a real draft PROMPT ("Continue editing your draft?") triggers the
    # fresh-start branch. A bare "draft" match is wrong: IG's composer gallery
    # permanently shows a "Drafts" tab AND titles itself "New post", so the
    # dismisser tapped the un-tappable title in a loop (2026-08-05).
    if "continue editing" in screen or "your draft" in screen:
        for o in obs:
            if clean(o) in _FRESH_START:
                return o
    for o in obs:                                     # explicit DENY next
        if clean(o) in _DENY:
            return o
    for o in obs:                    # complete the action, minus the extra grant
        if clean(o) in _CONSERVATIVE_CONFIRM:
            return o
    # "OK" only when no destructive CHOICE is on offer. Scan controls, not
    # prose: a destructive word inside body copy must not poison the ack —
    # IG's one-time "shared as reels" notice says "you can turn off remixing
    # from your settings" in a paragraph, and gating on the whole screen left
    # its OK untappable (aborted a live post run, 2026-08-04). Buttons are
    # short; a paragraph mentioning "turn off"/"delete" is not a button.
    destructive_control = any(
        any(d in clean(o) for d in _DESTRUCTIVE) and len(clean(o)) <= 32
        for o in obs)
    if not destructive_control:
        for o in obs:
            if clean(o) in _ACK:
                return o
    for o in obs:                                     # exact button label
        t = clean(o)
        if t in _DISMISS and not any(a in t for a in _AVOID):
            return o
    for o in obs:                                     # substring fallback
        t = clean(o)
        if any(d in t for d in _DISMISS) and not any(a in t for a in _AVOID):
            return o
    return None


class Navigator:
    """Deterministic tab navigation with cheap screen confirmation."""

    def __init__(self, platform: str, wda, perceptor=None, advisor=None,
                 recorder=None, screen=None):
        self.platform = platform
        self.wda = wda
        self.p = perceptor
        self.advisor = advisor          # optional VisionAdvisor for unknown screens
        self.rec = recorder             # optional RunRecorder for logging recovery
        self.feed_tab = FEED_TAB[platform]
        self._wh = screen               # threaded in — avoids window_size() on TikTok

    def _size(self):
        if self._wh is None:
            self._wh = self.wda.window_size()
        return self._wh

    def tap_tab(self, name: str) -> None:
        w, h = self._size()
        nx, ny = TABS[self.platform][name]
        self.wda.tap(int(nx * w), int(ny * h))

    # -- confirmation ------------------------------------------------------
    def on_feed(self) -> bool:
        """Cheap check: are we on a scrollable content feed right now?"""
        try:
            if self.platform == "instagram":
                return ig_is_feed(self.wda.source())
            # tiktok: OCR one screenshot (only called at nav points, not per scroll)
            import tempfile
            import os
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            if not self.wda.screenshot(path) or self.p is None:
                return False
            obs = self.p.ocr(path)
            text = " ".join(getattr(o, "text", "") for o in obs)
            return tiktok_is_feed(text)
        except Exception:
            return False

    def on_content(self) -> bool:
        """On ANY scrollable video surface — the home feed OR a keyword-search
        RESULTS PLAYER. Used so warming inside search results isn't mistaken for
        having drifted off the feed.

        IG: identical to on_feed (a result opens as a post carrying the like
        rail). TikTok: the For-You feed OR a results player, the latter told
        apart from a results GRID (which shows the Top/Videos/Users/Sounds tab
        row and must NOT be scrolled) by the ABSENCE of that tab row plus the
        top search pill."""
        if self.platform == "instagram":
            return self.on_feed()
        try:
            obs = self._shot_obs()
            text = " ".join(getattr(o, "text", "").lower() for o in obs)
            if tiktok_is_feed(text):
                return True                                  # For You / Following
            if "videos" in text and "users" in text and "sounds" in text:
                return False                                 # results GRID — tap a video first
            top = " ".join(getattr(o, "text", "").lower()
                           for o in obs if o.center[1] < 0.1)
            return "search" in top                           # results PLAYER (query + Search pill)
        except Exception:
            return False

    def content_kind(self) -> str:
        """TikTok only: classify the current feed item via one OCR — 'live', 'ad',
        or 'video'. LIVE has no rail and tapping it enters a fullscreen trap; ads
        shift the rail and replace follow with a shop link. Engagement is skipped
        on both — we just watch and scroll past."""
        if self.platform != "tiktok":
            return "video"
        obs = self._shot_obs()
        text = " ".join(getattr(o, "text", "") for o in obs).lower()
        if "tap to watch live" in text or "live now" in text or "watch live" in text:
            return "live"
        toks = text.replace("\n", " ").split()
        if "sponsored" in toks or "ad" in toks or "promoted" in toks:
            return "ad"
        return "video"

    def probe(self):
        """One cheap read -> (is_feed, signature). IG only (fast a11y); TikTok
        returns (None, None) — its per-scroll checks are too costly, so callers
        fall back to periodic OCR confirmation for it."""
        if self.platform != "instagram":
            return (None, None)
        try:
            import re
            xml = self.wda.source()
            is_feed = ("like-button" in xml) or ("comment-button" in xml)
            m = re.search(r'name="(Post by [^"]+)"', xml)
            return (is_feed, m.group(1) if m else None)
        except Exception:
            return (None, None)

    def feed_signature(self) -> Optional[str]:
        """A cheap 'which post am I on' signal for stuck-detection (IG only).

        Returns the top visible post's author label if resolvable, else None.
        TikTok returns None (we don't pay for its tree per scroll)."""
        if self.platform != "instagram":
            return None
        try:
            import re
            xml = self.wda.source()
            m = re.search(r'name="(Post by [^"]+)"', xml)
            return m.group(1) if m else None
        except Exception:
            return None

    def _shot_obs(self):
        import os
        import tempfile
        if self.p is None:
            return []
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            if not self.wda.screenshot(path):
                return []
            return self.p.ocr(path)
        except Exception:
            return []

    def dismiss_popup(self) -> bool:
        """If a known interstitial is up, tap its safe dismiss control. Returns
        True iff something was dismissed."""
        import time
        if self.dismiss_native_alert():
            return True
        obs = self._shot_obs()
        tgt = find_dismiss(obs)
        if tgt is None:
            return False
        cx, cy = tgt.center
        w, h = self._size()
        self.wda.tap(int(cx * w), int(cy * h))
        if self.rec:
            self.rec.log(f"dismissed popup via {tgt.text!r}")
        time.sleep(1.2)
        return True

    def dismiss_native_alert(self) -> bool:
        """DENY any native iOS permission alert ("Allow … to find devices on
        local networks?", notifications, tracking). These are system alerts,
        not in-app modals: the OCR safe-word path can't touch them (every
        button says "Allow", which is on the never-tap list) and one sitting on
        screen wedges the whole session — which is exactly how an IG run got
        stuck on 2026-07-30. WDA's alert API taps the DENY button for us.
        """
        import time
        probe = getattr(self.wda, "alert_text", None)
        deny = getattr(self.wda, "alert_dismiss", None)
        if probe is None or deny is None:
            return False                     # client without the alert API
        text = probe()
        if not text or not deny():
            return False
        if self.rec:
            self.rec.log(f"denied native alert: {text.splitlines()[0][:80]!r}")
        time.sleep(1.0)
        return True

    def clear_blocking(self, rounds: int = 3) -> int:
        """Dismiss onboarding modals / interstitials that block a screen we're
        about to rely on (e.g. TikTok's 'Add your college' over the profile). Uses
        the OCR safe-word dismiss first, then the vision advisor for X/close/skip
        controls that carry no readable text. Bounded rounds (no spam). Returns how
        many it cleared."""
        import os
        import tempfile
        import time
        cleared = 0
        for _ in range(rounds):
            if self.dismiss_popup():                 # OCR safe-word ("Not Now" etc.)
                cleared += 1
                continue
            if self.advisor is None:
                break
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            try:
                if not self.wda.screenshot(path):
                    break
                action = self.advisor.suggest(path, self.platform)
            except Exception:
                break
            if action and action.get("type") == "tap":
                w, h = self._size()
                self.wda.tap(int(action["x"] * w), int(action["y"] * h))
                if self.rec:
                    self.rec.log(f"cleared modal via vision tap "
                                 f"{action['x']:.2f},{action['y']:.2f}")
                time.sleep(1.2)
                cleared += 1
                continue
            break                                    # advisor says nothing to dismiss
        return cleared

    def force_clear(self) -> bool:
        """Aggressively clear a modal sheet that's blocking the feed — used when
        we're STUCK even though on_feed reads true (a sheet is over the reel, and
        the reel's elements persist in the tree behind it, fooling on_feed). Tries
        the text dismisser first, then the vision advisor (which can tap an X that
        carries no readable text). Returns True if it acted."""
        if self.dismiss_popup():
            return True
        if self.advisor is not None:
            self._ask_vision()
            return True
        return False

    # -- navigation with adaptation ---------------------------------------
    def ensure_feed(self, tries: int = 2) -> bool:
        """Get onto the content feed and CONFIRM it. Never returns True unless a
        real feed is verified — callers must refuse to scroll otherwise."""
        import time
        for _ in range(tries):
            if self.on_feed():
                return True
            self.tap_tab(self.feed_tab)      # deterministic tab tap
            time.sleep(2.0)
        return self.on_feed()

    def _press_back(self) -> None:
        """Tap the top-left back arrow — escapes profiles / rewards / settings
        sub-pages that a bottom-tab tap can't back out of."""
        import time
        w, h = self._size()
        self.wda.tap(int(0.06 * w), int(0.075 * h))
        time.sleep(1.2)

    def _escape(self, attempt: int) -> None:
        """Escalating trap escape, rotating per recovery round. Traps stack
        (e.g. a Shop LIVE with a 'Similar products' sheet on top), and each
        layer needs a different move:
          0: top-left back arrow (sub-pages)
          1: swipe a bottom sheet DOWN (product/comment sheets — no OCRable
             close control; the drag-to-dismiss gesture is generic)
          2: top-right ✕ (LIVE / shop overlays put close there)
        Only ever called when NOT on a feed, so none of these can misfire on
        real content."""
        import time
        w, h = self._size()
        mode = attempt % 3
        if mode == 0:
            self._press_back()
        elif mode == 1:
            self.wda.swipe(int(0.5 * w), int(0.40 * h),
                           int(0.5 * w), int(0.92 * h), 400)
            time.sleep(1.2)
        else:
            self.wda.tap(int(0.93 * w), int(0.068 * h))
            time.sleep(1.2)

    def ensure_ready(self, tries: int = 5) -> bool:
        """The adaptation loop: get to a confirmed feed, dealing with whatever is
        in the way (TikTok is trap-dense: LIVE, ads, rewards promos, profiles).
        Each round tries, in order: dismiss a modal via a safe control → press the
        top-left back arrow → tap the feed tab. Vision advisor only as a last
        resort. Returns True only when a real feed is confirmed."""
        import time
        for _round in range(tries):
            if self.on_feed():
                return True
            if self.dismiss_popup():         # modal with a safe dismiss (Cancel/X/Not Now)
                continue
            # State-aware escape. On a PROFILE (@handle visible) go straight to
            # the feed tab — the top-left "back" coordinate there is TikTok's
            # person-add icon, and tapping it walks INTO the Add-Friends trap
            # (which cascades to suggested-video sub-pages). Back-press only on
            # screens that aren't a profile.
            obs = self._shot_obs()
            on_profile = any(getattr(o, "text", "").strip().startswith("@")
                             for o in obs)
            # Composer guard: TikTok/IG can RESTORE a half-finished post
            # (create / caption / upload / draft) when the app cold-launches. On
            # such a screen the escape routine's corner-✕ (0.93,0.068) sits right
            # next to the live Post button — a stray tap would PUBLISH. So detect
            # a composer and back OUT (top-left, always safe) instead of escaping.
            blob = " ".join(getattr(o, "text", "") for o in obs).lower()
            on_composer = any(m in blob for m in (
                "add description", "add sound", "add a caption", "new post",
                "your story", "add yours", "post to", "who can view",
                "continue editing", "save draft", "edit cover"))
            if on_composer:
                w, h = self._size()
                self.wda.tap(int(0.06 * w), int(0.075 * h))   # back — never commits
                time.sleep(1.4)
            elif not on_profile:
                self._escape(_round)         # back / sheet-swipe / corner ✕
                if self.on_feed():
                    return True
            self.tap_tab(self.feed_tab)      # force the feed tab
            time.sleep(1.8)
        if not self.on_feed() and self.advisor is not None:
            self._ask_vision()               # last resort
            self.tap_tab(self.feed_tab)
            time.sleep(1.5)
        return self.on_feed()

    def _ask_vision(self) -> None:
        """Last resort: hand the screenshot to a vision advisor and perform the
        single recovery action it suggests (tap x,y / swipe / back). Rare, so the
        cost is acceptable; never used on the fast path."""
        import os
        import tempfile
        import time
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            if not self.wda.screenshot(path):
                return
            action = self.advisor.suggest(path, self.platform)
            if self.rec:
                self.rec.log(f"vision advisor -> {action}")
            w, h = self._size()
            if not action:
                return
            if action.get("type") == "tap":
                self.wda.tap(int(action["x"] * w), int(action["y"] * h))
            elif action.get("type") == "back":
                # exit a composer/create/post/draft screen the safe way — the
                # top-left back/close arrow never commits a post.
                self.wda.tap(int(0.06 * w), int(0.075 * h))
            elif action.get("type") == "feed_tab":
                self.tap_tab(self.feed_tab)
            time.sleep(1.5)
        except Exception:
            pass
