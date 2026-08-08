"""OpenRouter-backed AI — comment generation + optional vision recovery.

Used sparingly and only off the hot path:
  * comment generation — occasional, content-aware, tightly constrained (see
    `generate_comment`): all lowercase, <=5 words, no em dash, a genuine casual
    question. The style rules are ENFORCED in code after generation, not trusted
    to the model.
  * vision advisor — a fallback for unrecognized screens (see screens.Navigator);
    hands one screenshot to a vision model and returns a single recovery action.

The API key is NEVER stored in config.json or the repo. It is read from the
`OPENROUTER_API_KEY` env var, else `~/.config/autowarmer/secrets.env` (chmod 600).
It is kept only in memory and never logged.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import List, Optional

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_SECRETS = Path(os.path.expanduser("~/.config/autowarmer/secrets.env"))


def openrouter_key() -> Optional[str]:
    """Env var first, then the restricted secrets file. Never returned to logs."""
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k.strip()
    if _SECRETS.exists():
        for line in _SECRETS.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


class OpenRouterClient:
    def __init__(self, key: Optional[str] = None, timeout: int = 40):
        self.key = key or openrouter_key()
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _post(self, body: dict) -> Optional[str]:
        if not self.key:
            return None
        req = urllib.request.Request(
            _ENDPOINT, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json",
                     "HTTP-Referer": "https://autowarmer.local",
                     "X-Title": "autowarmer"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode())
            return data["choices"][0]["message"]["content"]
        except Exception:
            return None

    def chat(self, prompt: str, model: str, max_tokens: int = 40,
             temperature: float = 0.9) -> Optional[str]:
        return self._post({"model": model, "max_tokens": max_tokens,
                           "temperature": temperature,
                           "messages": [{"role": "user", "content": prompt}]})

    def chat_image(self, prompt: str, image_path: str, model: str,
                   max_tokens: int = 60) -> Optional[str]:
        try:
            b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
        except Exception:
            return None
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"}}]
        return self._post({"model": model, "max_tokens": max_tokens,
                           "messages": [{"role": "user", "content": content}]})


# --------------------------------------------------------------------------
# Comment generation — content-aware, tightly constrained, style enforced.
# --------------------------------------------------------------------------

_COMMENT_PROMPT = (
    "You are a real person casually scrolling {platform}. Here is context from the "
    "current post (its caption / on-screen text, possibly messy OCR):\n"
    "\"\"\"{context}\"\"\"\n\n"
    "Write ONE short, genuine, casual question a normal viewer might leave as a "
    "comment — curious and relevant to the post. STRICT rules:\n"
    "- all lowercase\n- 5 words maximum\n- no em dash, no hashtags, no emojis, no @mentions\n"
    "- it must be a natural question ending with '?'\n"
    "Reply with ONLY the comment text."
)

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿]")


def sanitize_comment(text: str) -> Optional[str]:
    """Enforce the style rules regardless of what the model returned.

    Returns a clean <=5-word, lowercase, punctuation-safe question, or None if
    nothing usable survives (caller then simply skips commenting)."""
    if not text:
        return None
    t = text.strip().strip('"').strip("'")
    t = t.replace("—", " ").replace("–", " ")     # kill em/en dashes
    t = _EMOJI.sub("", t)
    t = re.sub(r"[#@]\w+", "", t)                  # drop hashtags / mentions
    t = t.splitlines()[0] if t.splitlines() else t
    t = t.lower()
    # keep only words + a trailing question mark; collapse whitespace
    words = re.findall(r"[a-z0-9']+", t)
    if not words:
        return None
    words = words[:5]
    q = " ".join(words)
    if len(q) < 3:
        return None
    return q + "?"


def generate_comment(client: OpenRouterClient, context: str, platform: str,
                     model: str) -> Optional[str]:
    if not client.available:
        return None
    raw = client.chat(_COMMENT_PROMPT.format(platform=platform,
                                             context=(context or "")[:400]),
                      model=model, max_tokens=24, temperature=0.9)
    return sanitize_comment(raw)


# --------------------------------------------------------------------------
# Vision advisor (recovery) — OpenRouter implementation of the screens contract.
# --------------------------------------------------------------------------

# Goal-directed recovery prompts. The advisor is shared by two flows with
# OPPOSITE objectives, so it must be told which goal it serves:
#   * "feed"  — warm-up: reach the scrollable content feed and NEVER publish.
#   * "post"  — posting flow: clear whatever blocks the create/upload steps.
_ADVISOR_PROMPT_FEED = (
    "This is a screenshot of the {platform} iPhone app during an automated "
    "content-CONSUMPTION (warm-up) session. The ONLY goal is to reach the "
    "scrollable video/content FEED (TikTok 'For You'; Instagram Reels/Home) so "
    "the session can watch and scroll. You are a recovery step — something is "
    "blocking that goal.\n"
    "CRITICAL SAFETY: this account must NOT post, publish, upload, or send "
    "anything. If the screen is a CREATE / POST / CAPTION / UPLOAD / new-story / "
    "composer / draft / 'Continue editing this post' screen, LEAVE it WITHOUT "
    "committing. NEVER suggest tapping 'Post', 'Publish', 'Send', 'Next', "
    "'Share', 'Add to story', 'Your story', 'Add yours', or any similar commit "
    "control — those are the most dangerous taps on the screen.\n"
    "Reply with ONLY one JSON action:\n"
    '- Exit a composer/create/post/draft screen or any sub-page by backing out: '
    '{{"type":"back"}} (taps the top-left back/close arrow — always safe).\n'
    '- Dismiss a popup/interstitial by tapping its close/"Not Now"/X control: '
    '{{"type":"tap","x":<0..1>,"y":<0..1>}} (NORMALIZED 0..1 coords).\n'
    '- If it is just the wrong tab, {{"type":"feed_tab"}}.\n'
    '- If nothing is actionable, {{"type":"none"}}.\n'
    "Reply with ONLY the JSON."
)

_ADVISOR_PROMPT_POST = (
    "This is a screenshot of the {platform} iPhone app during an automated "
    "content-feed session. If a popup/dialog/interstitial blocks the feed, reply "
    'with JSON to tap its dismiss/close/"Not Now" control: '
    '{{"type":"tap","x":<0..1>,"y":<0..1>}} (NORMALIZED coords). If it is simply '
    'the wrong screen, reply {{"type":"feed_tab"}}. If nothing is actionable, reply '
    '{{"type":"none"}}. Reply with ONLY the JSON.'
)


class OpenRouterAdvisor:
    def __init__(self, client: OpenRouterClient, model: str, goal: str = "feed"):
        self.client = client
        self.model = model
        self.goal = goal          # "feed" (warm-up, never-publish) | "post"

    def suggest(self, image_path: str, platform: str) -> Optional[dict]:
        from .vision import _parse_action
        prompt = _ADVISOR_PROMPT_POST if self.goal == "post" else _ADVISOR_PROMPT_FEED
        out = self.client.chat_image(prompt.format(platform=platform),
                                     image_path, self.model)
        return _parse_action(out or "")
