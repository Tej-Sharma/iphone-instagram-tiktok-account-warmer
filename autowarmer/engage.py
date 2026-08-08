"""Engage layer — fast, coordinate-driven in-feed actions (NO per-frame OCR).

The warm loop has to be quick *and* human. Apple Vision OCR is expensive, so it
runs ONLY once per session, for account verification (see perceive/apps). Inside
the feed we never do computer vision per post. Instead:

  * LIKE is a **double-tap on the video** — the native like gesture on both
    TikTok and Reels. No element lookup at all: instant and maximally human
    (this is literally how people like on their phones).
  * SAVE / COMMENT / SHARE / FOLLOW use the WDA **accessibility tree**, parsed
    exactly ONCE per session to locate the right-rail controls, then tapped by
    cached coordinates (with gaussian scatter). One cheap `source()` call — not
    vision, not per-frame.
  * SCROLL is a plain up-swipe (humanize.swipe_vector).

Control labels drift across app versions/locales, so when a control is missing
from the tree we fall back to per-platform normalized heuristics. Those are the
one place that still needs on-device calibration (marked TODO).

Design stance, unchanged from the rest of autowarmer: the *decision* to engage is
never made here — it comes from humanize's coin-flips against the incubation
ramp. This module only knows HOW to perform an action once the plan says to.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import humanize as H
from .device import WDA

Box = Tuple[float, float, float, float]   # x, y, w, h in screen points

# PRIMARY strategy: exact accessibility-identifier match on the `name` attribute.
# Instagram exposes stable, semantic ids (confirmed on-device 2026-07-22) — this
# is bulletproof and version-robust. TikTok is
# deliberately omitted: its rail icons carry only generic SF-Symbol names
# ('Camera', 'Clock', 'Settings', 'Music'), so it can't be tree-calibrated and
# falls through to fixed coordinates below.
_IDENT: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "instagram": {
        "like":    ("like-button", "ufi-like-button"),
        "comment": ("comment-button", "ufi-comment-button"),
        "share":   ("send-button", "ufi-send-button"),      # paper-plane = share/send
        "follow":  ("follow-button",),
        "more":    ("ufi-more-options-button",),            # opens the sheet that holds Save
        "save":    ("save-unsave-action",),                 # only present once the sheet is open
        "repost":  ("repost-button",),                      # future: reshare to feed/story
    },
}

# SECONDARY strategy: broad substring match on name+label — only used for kinds
# not resolved by an identifier (and platforms with no identifier table).
_SYN = {
    "like":    ("like", "heart", "digg"),
    "comment": ("comment", "read comment"),
    "save":    ("save", "favorite", "favourite", "collect", "bookmark"),
    "share":   ("share", "forward", "send to"),
    "follow":  ("follow",),
}
# Tokens that DISQUALIFY a synonym match (e.g. an already-followed / count label).
_NEG = {
    "like":    ("unlike", "liked", "likes"),
    "follow":  ("following", "unfollow", "requested", "followers", "follow back"),
    "save":    ("saved", "unsave"),
}

# TERTIARY strategy: fixed normalized centres (x, y). Calibrated on-device
# (iPhone SE 3rd gen, iOS 26.3.1, 2026-07-22). TikTok relies on these entirely;
# IG uses them only if an identifier is somehow missing.
_FALLBACK: Dict[str, Dict[str, Tuple[float, float]]] = {
    "instagram": {
        "like":    (0.500, 0.500),   # double-tap centre regardless
        "comment": (0.917, 0.417),
        "share":   (0.917, 0.637),
        "more":    (0.919, 0.742),   # more-options button
        "save":    (0.500, 0.520),   # the "Save reel" row inside the more sheet
        "follow":  (0.619, 0.768),
    },
    "tiktok": {
        # Measured on-device 2026-07-26 (iPhone SE). Right rail nx≈0.917.
        "follow":  (0.917, 0.362),   # red "+" just below the avatar
        "like":    (0.917, 0.437),   # heart — tapped directly (double-tap didn't register)
        "comment": (0.917, 0.513),
        "save":    (0.917, 0.611),   # bookmark
        "share":   (0.917, 0.712),
    },
}

# Where the comment composer's Send/Post control sits, and how to leave it.
# Heuristic; TODO calibrate the composer on-device. Send is bottom-right.
_COMMENT_SEND = {"instagram": (0.94, 0.905), "tiktok": (0.94, 0.925)}

# TikTok comment SHEET coords — stable regardless of which video (the sheet is a
# fixed overlay; only the rail comment-icon that opens it shifts per video).
# Measured + confirmed on-device 2026-07-26 (posted a real comment).
_TT_COMMENT = {"field": (0.35, 0.945), "post": (0.896, 0.615), "close": (0.93, 0.42)}


def _iter_elems(xml: str):
    """Yield (type, name, text, box, visible) for every element with a frame."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return
    for el in root.iter():
        a = el.attrib
        try:
            x, y = float(a["x"]), float(a["y"])
            w, h = float(a["width"]), float(a["height"])
        except (KeyError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        name = (a.get("name") or "").strip()
        text = " ".join(v for v in (a.get("name"), a.get("label"),
                                    a.get("value")) if v).lower()
        typ = a.get("type", el.tag)
        vis = a.get("visible", "true") != "false"
        yield typ, name, text, (x, y, w, h), vis


def parse_controls(xml: str, screen: Tuple[int, int],
                   platform: Optional[str] = None) -> Dict[str, Box]:
    """Locate right-rail controls in a WDA source dump.

    Identifier-first: if `platform` has an `_IDENT` table (Instagram), match the
    element whose `name` equals a known accessibility id — exact, semantic, and
    version-robust. Any control not resolved that way falls back to broad
    synonym matching (used wholesale for platforms without an id table). Among
    equally-good candidates the smallest-area, Button-typed element wins (an icon
    beats the big container that merely repeats the label).
    """
    sw, sh = screen
    elems = [e for e in _iter_elems(xml) if e[4]]        # visible only
    out: Dict[str, Box] = {}

    # -- pass 1: exact accessibility-identifier match --------------------
    ids = _IDENT.get((platform or "").lower(), {})
    for kind, idset in ids.items():
        hits = [(0 if "button" in typ.lower() else 1, box[2] * box[3], box)
                for typ, name, _t, box, _v in elems if name in idset]
        if hits:
            hits.sort(key=lambda t: (t[0], t[1]))
            out[kind] = hits[0][2]

    # -- pass 2: synonym fallback for whatever is still missing ----------
    for kind, syns in _SYN.items():
        if kind in out:
            continue
        cands = []
        for typ, _name, text, box, _v in elems:
            if not text:
                continue
            x, y, w, h = box
            if x < 0 or y < 0 or x + w > sw * 1.02 or y + h > sh * 1.02:
                continue
            if w > sw * 0.6 or h > sh * 0.6:              # too big to be an icon
                continue
            if not any(s in text for s in syns):
                continue
            if any(n in text for n in _NEG.get(kind, ())):
                continue
            cands.append((0 if "button" in typ.lower() else 1, w * h, box))
        if cands:
            cands.sort(key=lambda t: (t[0], t[1]))
            out[kind] = cands[0][2]
    return out


@dataclass
class EngageResult:
    kind: str
    ok: bool
    how: str          # "double-tap" | "tree" | "fallback" | "disabled" | "error"


class Engager:
    """Performs one in-feed action at a time, coordinate-driven.

    Calibrates lazily: the first non-like engage triggers a single `source()`
    parse; results are cached for the whole session (controls don't move between
    posts). `like` never needs calibration.
    """

    def __init__(self, platform: str, wda: WDA, rng, screen=None, perceptor=None):
        self.platform = platform
        self.wda = wda
        self.rng = rng
        self.p = perceptor          # for TikTok comment sheet-open confirmation (OCR)
        self._screen: Optional[Tuple[int, int]] = screen   # threaded in (avoid window_size on TikTok)
        self._controls: Optional[Dict[str, Box]] = None

    # -- geometry ----------------------------------------------------------
    def _wh(self) -> Tuple[int, int]:
        if self._screen is None:
            self._screen = self.wda.window_size()
        return self._screen

    def calibrate(self, force: bool = False) -> Dict[str, Box]:
        if self._controls is not None and not force:
            return self._controls
        # Only platforms with an identifier table are worth a source() call.
        # TikTok's tree is non-semantic AND slow, so skip it entirely and drive
        # from the fixed fallback coordinates (calibrated on-device).
        if self.platform not in _IDENT:
            self._controls = {}
            return self._controls
        try:
            self._controls = parse_controls(self.wda.source(), self._wh(), self.platform)
        except Exception:
            self._controls = {}
        return self._controls

    def _box(self, kind: str) -> Box:
        """Cached tree box if found, else a small box around the fallback point."""
        ctrl = self.calibrate()
        if kind in ctrl:
            return ctrl[kind]
        w, h = self._wh()
        nx, ny = _FALLBACK[self.platform][kind]
        side = min(w, h) * 0.09                 # ~icon-sized synthetic box
        return (nx * w - side / 2, ny * h - side / 2, side, side)

    def _tap_kind(self, kind: str) -> str:
        box = self._box(kind)
        x, y = H.tap_point(self.rng, box)
        self.wda.tap(x, y)
        return "tree" if (self._controls and kind in self._controls) else "fallback"

    # -- actions -----------------------------------------------------------
    def like(self) -> EngageResult:
        """Like the current video.

        TikTok: tap the heart icon directly — double-tap-to-like does NOT reliably
        register via injected events (confirmed on-device), whereas a single tap
        on the heart does. On a fresh For-You feed the video is unliked, so the
        toggle is a like. Instagram: double-tap the video body (native gesture,
        which does register on IG and can't accidentally unlike)."""
        if self.platform == "tiktok":
            how = self._tap_kind("like")            # heart coord (0.917, 0.437)
            return EngageResult("like", True, f"heart-{how}")
        w, h = self._wh()
        cx = int(self.rng.normalvariate(w * 0.42, w * 0.10))
        cy = int(self.rng.normalvariate(h * 0.45, h * 0.10))
        cx = min(int(w * 0.70), max(int(w * 0.12), cx))
        cy = min(int(h * 0.70), max(int(h * 0.22), cy))
        self.wda.double_tap(cx, cy)
        return EngageResult("like", True, "double-tap")

    def save(self) -> EngageResult:
        # On IG Reels there is no save icon in the rail — Save lives inside the
        # more-options sheet, so it's a two-step: open the sheet, tap "Save reel".
        if self.platform == "instagram":
            return self._ig_save()
        how = self._tap_kind("save")
        return EngageResult("save", True, how)

    def _ig_save(self) -> EngageResult:
        """Open the more-options sheet and tap the Save row — located in the a11y
        tree ONLY. If the sheet doesn't open or the row isn't found, we DISMISS
        and skip: never blind-tap a guessed coordinate inside a menu (that once
        wandered into iOS Settings). Correctness beats completing the save."""
        self._tap_kind("more")                       # ufi-more-options-button
        box = None
        for _ in range(3):                           # wait for the sheet to animate in
            time.sleep(H.jittered_delay(self.rng, 0.6))
            try:
                ctrl = parse_controls(self.wda.source(), self._wh(), "instagram")
                box = ctrl.get("save")
            except Exception:
                box = None
            if box:
                break
        if not box:                                  # sheet never yielded a Save row
            w, h = self._wh()                        # dismiss the sheet, do nothing risky
            self.wda.swipe(int(w * 0.5), int(h * 0.5), int(w * 0.5), int(h * 0.98), ms=260)
            return EngageResult("save", False, "no-save-row")
        x, y = H.tap_point(self.rng, box)
        self.wda.tap(x, y)
        # Tapping "Save reel" pops a "Collect the posts you love" confirmation
        # sheet that BLOCKS scrolling until closed. Close it via its X (this was
        # the stuck-feed bug). Only acts if that sheet is actually detected.
        time.sleep(H.jittered_delay(self.rng, 0.9))
        self._dismiss_collect_sheet()
        return EngageResult("save", True, "menu-tree")

    def _dismiss_collect_sheet(self) -> None:
        try:
            src = self.wda.source()
        except Exception:
            return
        low = src.lower()
        if not ("collect the posts" in low or "start a collection" in low):
            return                                   # no confirmation sheet up
        close = self._find_el(lambda t: t.strip() in ("close", "dismiss", "done"),
                              types=("Button",))
        if close is not None:
            self._tap_box(close)                     # the X, via a11y
        else:                                        # known X location, top-right
            w, h = self._wh()
            self.wda.tap(int(0.924 * w), int(0.22 * h))
        time.sleep(H.jittered_delay(self.rng, 0.8))

    def follow(self) -> EngageResult:
        how = self._tap_kind("follow")
        return EngageResult("follow", True, how)

    def share(self) -> EngageResult:
        """Open the share sheet, linger, then dismiss (swipe the sheet down).

        A warm-up 'share' registers the intent tap without actually sending to
        anyone — the real reshare/repost path is deliberately future work.
        """
        how = self._tap_kind("share")
        time.sleep(H.jittered_delay(self.rng, 1.1))
        w, h = self._wh()                       # dismiss the bottom sheet
        self.wda.swipe(int(w * 0.5), int(h * 0.55), int(w * 0.5), int(h * 0.98), ms=260)
        return EngageResult("share", True, how)

    def _find_el(self, pred, types=None) -> Optional[Box]:
        """First visible element whose lowercased text satisfies `pred` (and, if
        given, whose type matches one of `types`). Returns its box or None."""
        try:
            src = self.wda.source()
        except Exception:
            return None
        for typ, _name, text, box, vis in _iter_elems(src):
            if not vis:
                continue
            if types and not any(t.lower() in typ.lower() for t in types):
                continue
            if pred((text or "").lower()):
                return box
        return None

    def _tap_box(self, box: Box) -> None:
        x, y = H.tap_point(self.rng, box)
        self.wda.tap(x, y)

    def _dismiss_sheet(self) -> None:
        w, h = self._wh()
        self.wda.swipe(int(w * 0.5), int(h * 0.5), int(w * 0.5), int(h * 0.98), ms=260)

    def _ocr_screen(self):
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

    def _tiktok_comment(self, text: str) -> EngageResult:
        """TikTok comment via fixed coords (a11y is non-semantic). Open the sheet
        (rail comment icon), CONFIRM it opened by OCR before typing (never
        blind-type into the video), type, tap Post, close. Sheet coords are stable
        across videos; only the opening icon shifts."""
        self._tap_kind("comment")                     # open sheet via rail icon
        time.sleep(H.jittered_delay(self.rng, 1.1))
        blob = " ".join(getattr(o, "text", "") for o in self._ocr_screen()).lower()
        if "comment" not in blob:                     # sheet didn't open — abort safely
            self._dismiss_sheet()
            return EngageResult("comment", False, "sheet-not-open")
        w, h = self._wh()
        fx, fy = _TT_COMMENT["field"]
        self.wda.tap(int(fx * w), int(fy * h))        # composer field
        time.sleep(H.jittered_delay(self.rng, 0.6))
        for chunk in _chunks(text, self.rng):
            self.wda.send_keys(chunk)
            time.sleep(H.jittered_delay(self.rng, 0.25))
        time.sleep(H.jittered_delay(self.rng, 0.5))
        px, py = _TT_COMMENT["post"]
        self.wda.tap(int(px * w), int(py * h))        # Post
        time.sleep(H.jittered_delay(self.rng, 1.0))
        cx, cy = _TT_COMMENT["close"]
        self.wda.tap(int(cx * w), int(cy * h))        # close the sheet
        time.sleep(H.jittered_delay(self.rng, 0.6))
        return EngageResult("comment", True, "posted")

    def comment(self, text: Optional[str]) -> EngageResult:
        """Open the composer, type `text`, post. TikTok uses fixed sheet coords
        (with an OCR sheet-open confirmation); IG is a11y-gated. Either way we
        never blind-tap 'send'. `text` is None to skip entirely."""
        if not text:
            return EngageResult("comment", False, "disabled")
        if self.platform == "tiktok":
            return self._tiktok_comment(text)
        self._tap_kind("comment")                         # open the comment sheet (IG)
        time.sleep(H.jittered_delay(self.rng, 1.0))
        field = (self._find_el(lambda t: "add a comment" in t or "comment as" in t
                               or "write a comment" in t)
                 or self._find_el(lambda t: True, types=("TextView", "TextField")))
        if field is None:                                 # composer didn't open
            self._dismiss_sheet()
            return EngageResult("comment", False, "composer-not-open")
        self._tap_box(field)
        time.sleep(H.jittered_delay(self.rng, 0.6))
        for chunk in _chunks(text, self.rng):             # human-ish keystroke bursts
            self.wda.send_keys(chunk)
            time.sleep(H.jittered_delay(self.rng, 0.25))
        time.sleep(H.jittered_delay(self.rng, 0.5))
        send = self._find_el(lambda t: t.strip() in ("post", "send"), types=("Button",))
        if send is None:                                  # never blind-tap send
            self._dismiss_sheet()
            return EngageResult("comment", False, "no-send-button")
        self._tap_box(send)
        time.sleep(H.jittered_delay(self.rng, 0.8))
        self._dismiss_sheet()
        return EngageResult("comment", True, "posted")

    def scroll_next(self) -> EngageResult:
        self.wda.swipe(*H.swipe_vector(self.rng, self._wh()))
        return EngageResult("scroll", True, "swipe")


def _chunks(text: str, rng):
    """Split a comment into 1-3 human keystroke bursts."""
    if len(text) <= 4:
        return [text]
    n = rng.randint(1, 3)
    if n == 1:
        return [text]
    cuts = sorted(rng.sample(range(1, len(text)), n - 1))
    out, prev = [], 0
    for c in cuts + [len(text)]:
        out.append(text[prev:c])
        prev = c
    return out
