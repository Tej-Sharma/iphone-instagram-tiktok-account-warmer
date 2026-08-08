"""App-specific flows — open, verify the live account, switch accounts.

Navigation uses the FIXED bottom-nav coordinates from `screens.TABS` — one
calibrated source of truth (no more divergent per-file heuristics). Account
identity is resolved **a11y-first on Instagram**: we match the handle in the
accessibility tree (the app's own text — exact, no OCR recognition error), and
fall back to Apple Vision OCR only if the tree doesn't yield it. TikTok stays
OCR-based (its a11y tree is non-semantic). OCR is never in the scroll loop — only
here, at the account-safety gate and account switching.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .device import GoIos, WDA
from .engage import _iter_elems
from .perceive import AccountMatch, Obs, Perceptor
from .screens import TABS

BUNDLE = {
    "instagram": "com.burbn.instagram",
    "tiktok": "com.zhiliaoapp.musically",
}

# Fixed location of the profile-header control (name + chevron) that opens the
# account switcher. IG: nav title top-centre (live-proven both directions,
# 2026-07-29). TikTok: NO nav title — the switcher is the display-name row
# BELOW the avatar (~y 0.27); the old top-centre coord hit the "Thoughts"
# bubble and opened a composer (trap!). TikTok is OCR-anchored first (see
# _open_switcher); this coord is only its last-resort fallback.
SWITCHER_TRIGGER = {"instagram": (0.50, 0.068), "tiktok": (0.50, 0.266)}

# Keyword-search entry points + first-result coords (normalized). Best-effort:
# the engine ALWAYS re-confirms a scrollable content surface after search and
# falls back to the home feed if it can't (never scroll an unconfirmed screen).
# IG search is a bottom tab; TikTok search is the top-right magnifier on the FYP.
# Calibrated live on iphone-1 (iPhone SE, 2026-07-29). IG: search is a bottom
# tab; the field is the top pill; results come back as EITHER a Reel-feed
# (already scrollable/engageable) OR a thumbnail grid — search() handles both.
_SEARCH = {
    "instagram": {"tab": (0.650, 0.945), "field": (0.45, 0.065), "first": (0.25, 0.42)},
    # tiktok: magnifier top-right of FYP -> field auto-focuses -> submit ->
    # results tabs; tap Videos for a clean grid -> first thumbnail opens the
    # scrollable results player. Calibrated live on iphone-1 (2026-07-29).
    "tiktok":    {"mag": (0.92, 0.055), "videos_tab": (0.38, 0.13), "first": (0.25, 0.38)},
}

# IG end-of-session story + DM coords (normalized). first = a friend's story
# circle in the top rail (past "Your story"); next = right-side tap to advance;
# dm = the direct-messages icon top-right of home. Calibrated live 2026-07-30.
# Calibrated live on iphone-1 (2026-07-30). first = the first friend's story
# circle in the top rail (past "Your story"); next = right-side tap to advance a
# story; dm = the direct-messages paper-plane in the BOTTOM nav centre (this IG
# layout puts DMs there, not top-right — top-right is the Activity heart).
_STORIES = {
    "instagram": {"first": (0.31, 0.19), "next": (0.85, 0.50), "dm": (0.50, 0.945),
                  "thread": (0.30, 0.54)},   # first real conversation under "Messages"
}
# TikTok end-of-session inbox browse. tab = the bottom-nav Inbox; thread = a
# message row to open + read. Calibrated live 2026-08-01.
_INBOX = {
    "tiktok": {"tab": (0.70, 0.955), "thread": (0.35, 0.58)},   # first activity/message row
}

# Distance from the @handle line up to the display-name/chevron row (TikTok).
_TT_NAME_ABOVE_HANDLE = 0.032


def _norm(s: str) -> str:
    """Lowercase, keep only [a-z0-9] so 'Example_Handle ˅' ~ 'example_handle'."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


@dataclass
class VerifyResult:
    ok: bool
    match: AccountMatch
    shot_path: str


class AppFlow:
    def __init__(self, platform: str, ios: GoIos, wda: WDA,
                 perceptor: Optional[Perceptor] = None,
                 screen: Optional[Tuple[int, int]] = None):
        self.platform = platform
        self.bundle = BUNDLE[platform]
        self.ios = ios
        self.wda = wda
        self.p = perceptor or Perceptor()
        # Screen size is threaded in from the caller (captured once at a safe
        # moment). Avoids re-calling window_size() while a still-loading or
        # never-idling app (TikTok) is foreground, which can hang WDA.
        self._size: Optional[Tuple[int, int]] = screen

    def _wh(self) -> Tuple[int, int]:
        if self._size is None:
            self._size = self.wda.window_size()
        return self._size

    def _tap_norm(self, nx: float, ny: float) -> None:
        w, h = self._wh()
        self.wda.tap(int(nx * w), int(ny * h))

    def _shot_ocr(self, tag: str) -> Tuple[str, List[Obs]]:
        path = f"/tmp/mw_{self.platform}_{tag}_{int(time.time())}.png"
        self.wda.screenshot(path)                # via WDA — tunnel-agnostic
        return path, self.p.ocr(path)

    # -- a11y handle resolution (Instagram) --------------------------------
    def _a11y_labels(self) -> List[Tuple[float, float, str]]:
        """[(nx, ny, label)] for every visible element carrying text."""
        out: List[Tuple[float, float, str]] = []
        try:
            src = self.wda.source()
        except Exception:
            return out
        w, h = self._wh()
        for _typ, _name, text, box, vis in _iter_elems(src):
            if not vis or not text:
                continue
            # `text` is already the joined, lowercased name+label+value blob.
            x, y, bw, bh = box
            out.append(((x + bw / 2) / w, (y + bh / 2) / h, text))
        return out

    def _a11y_match(self, target: str, exact: bool) -> Optional[Tuple[float, float, str]]:
        """Locate an element whose text IS (exact) or CONTAINS the handle."""
        nt = _norm(target.lstrip("@"))
        if len(nt) < 4:
            return None
        for nx, ny, label in self._a11y_labels():
            nl = _norm(label)
            if (nl == nt) if exact else (nt in nl):
                return (nx, ny, label)
        return None

    # -- flows -------------------------------------------------------------
    def open(self) -> None:
        # COLD launch (terminate + relaunch) on BOTH platforms — always start
        # from a clean home feed with the nav bar visible, so verify + the search
        # transition can't be defeated by the app resuming wherever the last
        # session died (a fullscreen search Reel on IG, a stuck DM/profile on
        # TikTok). The old reason TikTok used warm-activate — cold starts made
        # XCTest app-frame snapshots wedge, hanging coordinate taps — is obsolete:
        # we now inject taps snapshot-free (WDA /wda/mw/*), so cold-start taps are
        # fast (verified on iPhone SE, 2026-07-29). TikTok cold-start lands on the
        # For You feed; any onboarding modal is cleared by the navigator.
        try:
            self.wda.terminate_app(self.bundle); time.sleep(1.5)
        except Exception:
            pass
        self.wda.launch_app(self.bundle)
        time.sleep(4.0 if self.platform == "tiktok" else 3.0)

    def goto_profile(self) -> None:
        self._tap_norm(*TABS[self.platform]["profile"])   # calibrated profile tab
        time.sleep(1.5)

    def verify_account(self, expected: str) -> VerifyResult:
        """Open profile, confirm it's `expected`. IG: a11y-exact first (a dedicated
        element whose text IS the handle), else OCR. TikTok: OCR."""
        self.goto_profile()
        if self.platform == "instagram":
            hit = self._a11y_match(expected, exact=True)
            if hit:
                path = f"/tmp/mw_{self.platform}_verify_{int(time.time())}.png"
                self.wda.screenshot(path)
                m = AccountMatch(ok=True, reason="a11y-exact", matched_text=hit[2])
                return VerifyResult(True, m, path)
        shot, obs = self._shot_ocr("verify")
        match = self.p.find_account(obs, expected)
        return VerifyResult(match.ok, match, shot)

    def browse_stories_and_dms(self, rng, max_stories: int = 6) -> str:
        """End-of-session human touch (Instagram): open a story from the top
        rail, tap through a few (coin-flip each — heads advance, tails stop),
        swipe down to exit, then hop into the DM inbox and scroll it before
        leaving. Best-effort; never raises (returns a short summary). Coords
        calibrated on iphone-1 (iPhone SE, 2026-07-30)."""
        if self.platform != "instagram":
            return "n/a"
        c = _STORIES["instagram"]
        w, h = self._wh()
        self._tap_norm(*TABS["instagram"]["home"]); time.sleep(1.5)   # home timeline (story rail)
        self._tap_norm(*c["first"]); time.sleep(2.2)                  # open first friend's story
        seen = 0
        # GUARD: make sure we opened a friend's STORY VIEWER, not the create-story
        # composer. Tapping "Your story" or an empty rail opens "Add to Story";
        # poking around in there risks a draft/accidental post. If we see composer
        # markers, bail out immediately (X top-left, then a swipe as backup) and
        # skip story-watching for this session — DMs still run below.
        _, obs = self._shot_ocr("storychk")
        blob = " ".join(getattr(o, "text", "").lower() for o in obs)
        if any(m in blob for m in ("add to story", "your story", "add yours",
                                   "add a sticker", "your note")):
            self._tap_norm(0.06, 0.05); time.sleep(1.0)              # X out of the composer (empty)
            self.wda.swipe(int(0.5 * w), int(0.28 * h), int(0.5 * w), int(0.9 * h), 320)
            time.sleep(1.2)
        else:
            for _ in range(max_stories):
                time.sleep(1.0 + rng.random() * 3.0)                # watch this story
                seen += 1
                if rng.random() >= 0.6:                             # tails -> stop viewing
                    break
                self._tap_norm(*c["next"]); time.sleep(0.8)        # heads -> next story
            # exit the story viewer (swipe down is the universal dismiss)
            self.wda.swipe(int(0.5 * w), int(0.28 * h), int(0.5 * w), int(0.9 * h), 320)
            time.sleep(1.5)
        # DM inbox: the direct-messages icon (bottom-nav paper-plane)
        self._tap_norm(*c["dm"]); time.sleep(2.2)
        opened = ""
        convo = self._ig_first_conversation()                    # None if there are no real DMs
        if convo is not None and rng.random() < 0.85:            # open + read only a REAL conversation
            self._tap_norm(*convo); time.sleep(2.2)              # (never a "Suggestions · Tap to chat" row)
            time.sleep(1.5 + rng.random() * 3.0)                  # read it
            if rng.random() < 0.5:                                # sometimes read the history
                self.wda.swipe(int(0.5 * w), int(0.35 * h), int(0.5 * w), int(0.7 * h), 300)
                time.sleep(1.0)
            self._tap_norm(0.06, 0.075); time.sleep(1.4)          # back out to the inbox
            opened = " + read-dm"
        for _ in range(rng.randint(0, 2)):                        # browse-scroll the inbox either way
            self.wda.swipe(int(0.5 * w), int(0.70 * h), int(0.5 * w), int(0.42 * h), 320)
            time.sleep(0.9)
        self._tap_norm(*TABS["instagram"]["home"]); time.sleep(1.2)  # back to home, then session closes
        return f"stories~{seen} + dm-scroll{opened or ' (no dms)'}"

    def _ig_first_conversation(self):
        """(nx, ny) of the first REAL DM conversation, or None if the inbox has
        none. Real conversations sit between the 'Messages' and 'Suggestions'
        headers; the 'Suggestions · Tap to chat' rows below are NOT conversations
        (tapping one opens a brand-new empty chat), so they're excluded — that's
        the no-DMs case, where we open nothing."""
        try:
            _, obs = self._shot_ocr("iginbox")
        except Exception:
            return None
        rows = [(o.center[0], o.center[1], o.text.strip().lower()) for o in obs]
        msgs_y = next((y for x, y, t in rows if t == "messages"), None)
        if msgs_y is None:
            return None
        sugg_y = next((y for x, y, t in rows if "suggestion" in t), 1.0)
        cands = [(x, y) for x, y, t in rows
                 if msgs_y + 0.01 < y < sugg_y and x < 0.6 and len(t) > 2
                 and "tap to chat" not in t and t not in ("messages", "requests")]
        return min(cands, key=lambda c: c[1]) if cands else None

    def browse_inbox(self, rng) -> str:
        """TikTok end-of-session: open the Inbox, scroll activity, and tap into an
        incoming message to read it (human wind-down). Best-effort; never raises;
        leaves the app on a benign screen (the close step terminates it)."""
        if self.platform != "tiktok":
            return "n/a"
        c = _INBOX["tiktok"]
        w, h = self._wh()
        self._tap_norm(*c["tab"]); time.sleep(2.4)                 # Inbox tab
        opened = ""
        row = self._tt_first_inbox_row()                          # a real message, else an activity row
        if row is not None and rng.random() < 0.65:
            self._tap_norm(0.35, row); time.sleep(2.2)            # open it (tap the row, not the promo)
            time.sleep(1.5 + rng.random() * 3.0)                  # read it
            self._tap_norm(0.05, 0.05); time.sleep(1.4)           # back arrow -> inbox
            opened = " + read-msg"
        for _ in range(rng.randint(0, 2)):                        # then browse-scroll the inbox
            self.wda.swipe(int(0.5 * w), int(0.70 * h), int(0.5 * w), int(0.42 * h), 320)
            time.sleep(0.9)
        return f"inbox-scroll{opened or ' (nothing to open)'}"

    def _tt_first_inbox_row(self):
        """ny of the first openable inbox row: a real DM ('messaged you'/'sent
        you…') if present, else an activity row ('Activity'/'New followers').
        None if nothing recognizable is there (so we don't tap the promo banner
        or empty space when the inbox is bare)."""
        try:
            _, obs = self._shot_ocr("ttinbox")
        except Exception:
            return None
        rows = [(o.center[1], o.text.strip().lower()) for o in obs if 0.2 < o.center[1] < 0.85]
        dm = next((y for y, t in rows if "messaged you" in t or "sent you" in t
                   or "sent a" in t), None)
        if dm is not None:
            return dm
        act = next((y for y, t in rows if t in ("activity", "new followers")
                    or "viewed your profile" in t or "new followers" in t), None)
        return act

    def search(self, keyword: str) -> bool:
        """Navigate to keyword-search results and open a scrollable result.

        The MASTER SOP transition: once an account has aged past feed-only, a
        share of sessions should search a niche keyword and engage within the
        results (trains the algorithm's niche classification). Best-effort — the
        caller MUST re-confirm a content surface before scrolling. Returns False
        on any hiccup so the caller falls back to the home feed."""
        try:
            cfg = _SEARCH[self.platform]
            if self.platform == "instagram":
                self._tap_norm(*cfg["tab"]); time.sleep(1.6)     # Explore/search tab
                self._tap_norm(*cfg["field"]); time.sleep(1.0)   # focus the search field
                self.wda.send_keys(keyword); time.sleep(1.4)
                self.wda.send_keys("\n"); time.sleep(2.4)        # submit
                # Results are EITHER a Reel-feed (already engageable) or a grid.
                # If not already a feed, push the variable-height Meta-AI summary
                # off and open the first thumbnail; confirm before returning.
                from .screens import ig_is_feed
                if ig_is_feed(self.wda.source()):
                    return True
                w, h = self._wh()
                self.wda.swipe(int(0.5 * w), int(0.55 * h),
                               int(0.5 * w), int(0.20 * h), 340); time.sleep(1.5)
                if ig_is_feed(self.wda.source()):
                    return True
                self._tap_norm(*cfg["first"]); time.sleep(2.2)   # open a result post
                return ig_is_feed(self.wda.source())
            else:                                                # tiktok
                self._tap_norm(*cfg["mag"]); time.sleep(1.6)     # magnifier -> search page (field focused)
                self.wda.send_keys(keyword); time.sleep(1.4)
                self.wda.send_keys("\n"); time.sleep(2.4)        # submit -> results (Top)
                self._tap_norm(*cfg["videos_tab"]); time.sleep(1.6)   # Videos tab -> video grid
                # Poll until the grid actually RENDERS (weak signal can take
                # >8s); tapping before thumbnails appear misses. Grid loaded ==
                # several text runs (captions / view counts) in the body area.
                for _ in range(6):
                    time.sleep(2.0)
                    _, obs = self._shot_ocr("ttgrid")
                    if sum(1 for o in obs if 0.18 < o.center[1] < 0.66) >= 3:
                        break
                self._tap_norm(*cfg["first"]); time.sleep(2.6)   # open first video (ONE tap — a
                # blind retry can land on a profile/DM; the engine's on_content
                # gate re-checks and recovers to the FYP feed if this missed).
                try:
                    _, obs = self._shot_ocr("ttsearch")
                    t = " ".join(o.text.lower() for o in obs)
                    return not ("videos" in t and "users" in t and "sounds" in t)
                except Exception:
                    return True
        except Exception:
            return False

    def _open_switcher(self) -> None:
        """Open the account switcher from the profile.

        IG: fixed nav-title coord (stable, live-proven). TikTok: the trigger is
        the display-name+chevron row below the avatar — OCR-anchor it via the
        '@handle' line right under it (distinctive, position-robust) and tap
        just above; fixed coord only if OCR finds no @-line."""
        if self.platform == "tiktok":
            # TikTok's account-name+chevron (the switcher trigger) sits in the
            # top-centre nav on the current profile layout, but has appeared
            # centred (below the avatar) on other layouts/versions. Try each
            # candidate and CONFIRM the "Switch account" sheet actually opened
            # (OCR) — a blind fixed tap silently missed and dropped us onto the
            # grid/search. Dismiss any wrong screen between tries.
            for nx, ny in ((0.42, 0.06), (0.42, 0.27), (0.50, 0.266)):
                self._tap_norm(nx, ny); time.sleep(1.6)
                _, obs = self._shot_ocr("switchchk")
                if any("switch account" in o.text.lower() for o in obs):
                    return                              # sheet is open
                self._dismiss_overlay()                 # close whatever opened, try next
            return
        self._tap_norm(*SWITCHER_TRIGGER[self.platform])
        time.sleep(1.5)

    def _dismiss_overlay(self) -> None:
        """Escape hatch: close whatever sheet/composer a mis-tap opened, so a
        failed switch never leaves a stray screen behind.

        OCR-aware, never blind: prefer a safe dismiss word ("Got it"/"Cancel"/
        "Close"); else tap top-left X/back ONLY when we're clearly NOT on the
        profile (no @handle on screen) — on the TikTok profile top-left is the
        person-add icon, and blind-tapping it opened the Connect Now trap."""
        from .screens import find_dismiss
        _, obs = self._shot_ocr("dismiss")
        hit = find_dismiss(obs)
        if hit is not None:
            self._tap_norm(*hit.center)
        elif not any(o.text.strip().startswith("@") for o in obs):
            self._tap_norm(0.06, 0.075)          # top-left X / back chevron
        time.sleep(1.2)

    def switch_account(self, target: str, max_tries: int = 4) -> VerifyResult:
        """Confirm `target` is live; if not, open the switcher and tap its row.

        Row selection is a11y-driven on IG (match the handle in the tree — exact
        app text), OCR fallback otherwise. Always re-verifies after tapping, so a
        mis-tap can never leave us acting on the wrong account."""
        for _ in range(max_tries):
            res = self.verify_account(target)
            if res.ok:
                return res
            self._open_switcher()
            tapped = False
            if self.platform == "instagram":
                hit = self._a11y_match(target, exact=False)
                if hit:
                    self._tap_norm(hit[0], hit[1])   # tap the matching account row
                    time.sleep(2.5)
                    tapped = True
            if not tapped:
                _, obs = self._shot_ocr("switcher")
                row = self.p.find_account(obs, target)
                if row.ok and row.obs is not None:
                    cx, cy = row.obs.center
                    self._tap_norm(cx, cy)
                    time.sleep(4.0)                  # TikTok switch-reload is slow
                    tapped = True
                else:
                    # No target row visible. If TikTok is mid switch-reload
                    # ("Loading…" modal — slow on older phones), the trigger tap
                    # was swallowed: wait it out and RETRY, don't give up.
                    txt = " ".join(o.text.lower() for o in obs)
                    if "loading" in txt:
                        time.sleep(4.0)
                    else:
                        self._dismiss_overlay()      # close whatever we opened
        return self.verify_account(target)
