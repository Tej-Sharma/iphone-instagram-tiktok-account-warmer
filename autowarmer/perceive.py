"""Perception — turn a screenshot into decisions.

Wraps the Apple Vision OCR helper (tools/vision_ocr.swift, the Mac-native
on-device text reader) plus the fuzzy account matcher that is
the make-or-break for multi-account safety: it must confirm we are "roughly" on
the expected account, and it must tolerate the on-screen handle being
TRUNCATED (e.g. "example_han…") — a failure this hit in practice
on a small iPhone.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "tools" / "vision_ocr.swift"
_BIN = _ROOT / "bin" / "vision_ocr"


@dataclass
class Obs:
    text: str
    conf: float
    x: float      # top-left origin, normalized [0,1]
    y: float
    w: float
    h: float

    @property
    def center(self):
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)


@dataclass
class AccountMatch:
    ok: bool
    reason: str                 # "exact" | "prefix" | "substring" | "none"
    matched_text: Optional[str] = None
    obs: Optional[Obs] = None


def normalize_handle(s: str) -> str:
    """Lowercase, drop a leading @, keep only [a-z0-9._] then strip separators
    for comparison so 'Example_Handle' ~ 'example_handle'."""
    s = s.strip().lstrip("@").lower()
    return "".join(ch for ch in s if ch.isalnum())


class Perceptor:
    def __init__(self):
        self._ensure_built()

    def _ensure_built(self) -> None:
        if _BIN.exists() and _BIN.stat().st_mtime >= _SRC.stat().st_mtime:
            return
        _BIN.parent.mkdir(exist_ok=True)
        subprocess.run(["swiftc", "-O", str(_SRC), "-o", str(_BIN)], check=True)

    def ocr(self, image_path: str) -> List[Obs]:
        out = subprocess.run([str(_BIN), image_path], capture_output=True,
                             text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(f"vision_ocr failed: {out.stderr.strip()}")
        return [Obs(**o) for o in json.loads(out.stdout or "[]")]

    # -- the load-bearing check --------------------------------------------
    def find_account(self, obs: List[Obs], expected: str,
                     min_prefix: int = 6) -> AccountMatch:
        """Is the expected handle 'roughly' on screen?

        Tolerant, in priority order:
          exact      normalized on-screen text == normalized expected
          prefix     on-screen text is a >=min_prefix prefix of expected
                     (handles UI truncation: 'example_han' -> 'example_handle')
          substring  expected fully contained in an on-screen token
        """
        want = normalize_handle(expected)
        if not want:
            return AccountMatch(False, "none")
        best_prefix: Optional[AccountMatch] = None
        for o in obs:
            # Variants of the on-screen text, most-specific first. A truncated
            # nav title drags UI junk into OCR — e.g. IG's 'corporatero... v'
            # (chevron read as the letter v), which breaks a naive prefix test
            # (…'corporaterov' vs 'example_user'). Everything AFTER an
            # ellipsis is junk, so match on the pre-ellipsis part too; token-
            # wise variants catch space-separated artifacts.
            raw = o.text
            variants = [raw]
            for mark in ("…", "..."):
                if mark in raw:
                    variants.append(raw.split(mark, 1)[0])
                    break
            variants.extend(raw.split())
            seen = set()
            for v in variants:
                got = normalize_handle(v)
                if not got or got in seen:
                    continue
                seen.add(got)
                if got == want:
                    return AccountMatch(True, "exact", o.text, o)
                if want.startswith(got) and len(got) >= min_prefix:
                    cand = AccountMatch(True, "prefix", o.text, o)
                    if best_prefix is None or len(got) > len(normalize_handle(best_prefix.matched_text)):
                        best_prefix = cand
                elif want in got:
                    return AccountMatch(True, "substring", o.text, o)
        if best_prefix:
            return best_prefix
        return AccountMatch(False, "none")

    def find_text(self, obs: List[Obs], needle: str) -> Optional[Obs]:
        n = needle.strip().lower()
        for o in obs:
            if n in o.text.strip().lower():
                return o
        return None
