"""Optional vision advisor — the last-resort escalation for unknown screens.

The warm loop is deterministic and fast: known coordinates, cheap screen checks,
no per-frame perception. But occasionally we land somewhere the fixed-UI rules
don't recognize (a new interstitial, an A/B layout, an ad). Rather than guess, we
can hand that ONE screenshot to a vision model and do the single recovery action
it suggests. This is rare by construction, so the latency/cost is acceptable — it
must never be called on the happy path.

Contract — an advisor implements:
    suggest(image_path: str, platform: str) -> dict | None
returning one of:
    {"type": "tap", "x": <0..1>, "y": <0..1>}   # tap this point (e.g. a Close/X)
    {"type": "feed_tab"}                          # just go back to the feed tab
    {"type": "none"}                              # nothing actionable
Coordinates are normalized so they're device-independent.

`ClaudeCLIAdvisor` shells out to the local `claude` CLI headlessly. It is OFF by
default (enable with config `"vision_recovery": true`); treat it as experimental.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Optional

_PROMPT = (
    "Read the image at {path}. It is a screenshot of the {platform} iPhone app "
    "during an automated content-feed session. If a popup/dialog/interstitial is "
    "blocking the feed, reply with JSON to tap its dismiss/close/'Not Now' control: "
    '{{"type":"tap","x":<0..1>,"y":<0..1>}} using NORMALIZED coordinates. '
    'If it is simply the wrong screen, reply {{"type":"feed_tab"}}. '
    'If nothing is actionable, reply {{"type":"none"}}. '
    "Reply with ONLY the JSON object, no prose."
)


class ClaudeCLIAdvisor:
    """Best-effort advisor backed by the local `claude` CLI (headless)."""

    def __init__(self, binary: str = "claude", timeout: int = 60):
        self.binary = binary
        self.timeout = timeout

    def suggest(self, image_path: str, platform: str) -> Optional[dict]:
        prompt = _PROMPT.format(path=image_path, platform=platform)
        try:
            r = subprocess.run([self.binary, "-p", prompt],
                               capture_output=True, text=True, timeout=self.timeout)
        except Exception:
            return None
        return _parse_action(r.stdout)


def _parse_action(text: str) -> Optional[dict]:
    m = re.search(r'\{[^{}]*"type"[^{}]*\}', text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    t = d.get("type")
    if t == "tap" and _is01(d.get("x")) and _is01(d.get("y")):
        return {"type": "tap", "x": float(d["x"]), "y": float(d["y"])}
    if t in ("feed_tab", "back", "none"):
        return {"type": t}
    return None


def _is01(v) -> bool:
    return isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0


def make_advisor(cfg, goal: str = "feed") -> Optional[object]:
    """Build an advisor from config, or None when vision recovery is disabled.

    Prefers OpenRouter (a real vision model) when a key is present; falls back to
    the local `claude` CLI. `goal` selects the recovery objective the model is
    told to pursue: "feed" (warm-up — reach the feed, NEVER publish) or "post"
    (posting flow — advance the create/upload steps). Default is the safe
    never-publish "feed" goal."""
    if not getattr(cfg, "vision_recovery", False):
        return None
    from .ai import OpenRouterClient, OpenRouterAdvisor
    client = OpenRouterClient()
    if client.available:
        return OpenRouterAdvisor(client, getattr(cfg, "ai_vision_model",
                                                 "openai/gpt-4o-mini"), goal=goal)
    return ClaudeCLIAdvisor(binary=getattr(cfg, "claude_binary", "claude"))


# --- step verifier: a vision fallback for the POSTING flow ------------------

_VERIFY_PROMPT = (
    "This is an iPhone screenshot of the {platform} app during an automated "
    "post. The flow expects to be on this screen now: {expect}\n"
    "{context}"
    "Reply with ONLY a JSON object:\n"
    '  {{"match": true}} if the screen matches the expectation;\n'
    '  {{"match": false, "tap": {{"x": <0..1>, "y": <0..1>}}, "why": "<12 words>"}} '
    "if a popup/interstitial is covering it and tapping ONE control would clear "
    "it OR advance toward the goal above (give NORMALIZED coordinates of that "
    "control);\n"
    '  {{"match": false, "why": "<12 words>"}} if it is simply a different screen.\n'
    "Never suggest tapping Post, Share, Publish, Delete, Discard, Log out, or "
    "Allow — those commit or destroy something."
)

# Controls the verifier must never be able to make us tap, whatever it replies.
_NEVER_TAP = ("post", "share", "publish", "delete", "discard", "log out",
              "logout", "allow", "continue")


class VisionVerifier:
    """Asks a vision model whether the screen is what the flow expects.

    Deliberately a FALLBACK, not a per-step gate: the deterministic expects poll
    and pass in well under a second, while a model call costs a round trip and
    real money. This runs only when a step's own check has already failed —
    which is precisely where slow networks and unknown interstitials land — and
    it can only ever suggest ONE non-committing tap.
    """

    def __init__(self, client, model: str, platform: str, timeout: int = 25):
        self.client, self.model, self.platform = client, model, platform
        self.timeout = timeout

    def verify(self, image_path: str, expect: str, context: str = "") -> Optional[dict]:
        try:
            ctx = (context.strip() + "\n") if context else ""
            raw = self.client.chat_image(
                _VERIFY_PROMPT.format(platform=self.platform, expect=expect, context=ctx),
                image_path, model=self.model, max_tokens=120)
        except Exception:                                   # noqa: BLE001
            return None
        if not raw:
            return None
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            out = json.loads(m.group(0))
        except ValueError:
            return None
        tap = out.get("tap") or {}
        if tap and not (_is01(tap.get("x")) and _is01(tap.get("y"))):
            out.pop("tap", None)
        return out


def make_verifier(cfg, platform: str) -> Optional["VisionVerifier"]:
    """Build the posting-flow verifier, or None when it isn't configured."""
    if not getattr(cfg, "vision_recovery", False):
        return None
    try:
        from .ai import OpenRouterClient, openrouter_key
    except Exception:                                       # noqa: BLE001
        return None
    if not openrouter_key():
        return None
    model = getattr(cfg, "ai_vision_model", "") or "google/gemini-2.5-flash-lite"
    return VisionVerifier(OpenRouterClient(), model, platform)
