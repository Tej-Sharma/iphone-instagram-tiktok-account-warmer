"""Orchestrator — ties config + incubation + humanize together.

`plan_all()` and `status()` are pure/read-only and safe to run anytime. Live
execution (`run_account`) is gated and, in this milestone, only performs the
device-safe parts (open/verify/scroll/watch); engage actions that need OCR are
surfaced but not yet clicked. See README for the milestone map.
"""
from __future__ import annotations

import datetime as _dt
import random
from pathlib import Path
from typing import List, Optional

from . import humanize as H
from .apps import AppFlow, BUNDLE
from .device import Config, Driver, GoIos, LaneBusy
from .engage import Engager
from .incubation import AccountState, Store
from .perceive import Perceptor
from .screens import Navigator
from .vision import make_advisor


class Engine:
    def __init__(self, cfg: Config, state_root: Path):
        self.cfg = cfg
        self.state_root = Path(state_root)
        self.store = Store(state_root)

    def _seeded_rng(self, account: str, salt: str = "") -> random.Random:
        # deterministic per account+day+salt so a session is reproducible for
        # dry-run review, yet different every day and across accounts.
        day = _dt.date.today().toordinal()
        return random.Random(hash((account, day, salt)) & 0xFFFFFFFF)

    def _acct_cfg(self, username: str, platform: Optional[str] = None) -> Optional[dict]:
        # A username is NOT unique on its own: the same handle can exist on both
        # IG and TikTok (e.g. @danielainamerica, @marco_the_salazar). When the
        # platform is known, match on (username, platform) so we don't run one
        # platform twice and skip the other. Falls back to first-by-username.
        for a in self.cfg.accounts:
            if a["username"] == username and (platform is None or a["platform"] == platform):
                return a
        return None

    def plan_all(self) -> List[H.SessionPlan]:
        plans = []
        for a in self.cfg.accounts:
            st = self.store.load_or_init(a["platform"], a["username"],
                                         created_at=a.get("created_at"))
            ph = st.phase()
            rng = self._seeded_rng(a["username"])
            plans.append(H.plan_session(rng, ph, a["username"], a.get("keywords", [])))
        return plans

    def status(self) -> List[dict]:
        rows = []
        for a in self.cfg.accounts:
            st = self.store.load_or_init(a["platform"], a["username"],
                                         created_at=a.get("created_at"))
            ph = st.phase()
            rows.append(dict(username=st.username, platform=st.platform,
                             day=st.day(), phase=ph.name,
                             like_rate=ph.like_rate, follow_rate=ph.follow_rate,
                             mode="search" if ph.search_mode else "feed",
                             sessions_per_day=ph.sessions_per_day,
                             posting_allowed=ph.posting_allowed,
                             completed_sessions=st.completed_sessions,
                             note=ph.note))
        return rows

    def verify_account(self, username: str) -> dict:
        """Live but low-risk: open the app, tap to profile, OCR-confirm the
        account is roughly `username`. Opens a WDA session (port 8100 — only one
        driver may run against a phone at a time)."""
        acfg = self._acct_cfg(username)
        if acfg is None:
            raise ValueError(f"account not in config: {username}")
        driver = Driver(self.cfg, owner="autowarmer verify")
        try:
            try:
                if not driver.ensure_lane():
                    return {"error": "WDA lane did not come up (phone offline or "
                                     "another driver holds port 8100)"}
            except LaneBusy as e:
                return {"error": str(e)}
            driver.wda.new_session()
            driver.wda.configure()      # disable idle-wait (essential for TikTok)
            flow = AppFlow(acfg["platform"], GoIos(self.cfg), driver.wda, Perceptor())
            flow.open()
            res = flow.verify_account(username)
            return {"account": username, "on_correct_account": res.ok,
                    "match_reason": res.match.reason,
                    "onscreen_text": res.match.matched_text, "screenshot": res.shot_path}
        finally:
            driver.close()

    # -- live execution (gated) --------------------------------------------
    def run_account(self, username: str, live: bool = False,
                    trace_level: Optional[str] = None,
                    engage_live: bool = True, max_attempts: int = 2,
                    platform: Optional[str] = None) -> dict:
        acfg = self._acct_cfg(username, platform)
        if acfg is None:
            raise ValueError(f"account not in config: {username}"
                             + (f" ({platform})" if platform else ""))
        st = self.store.load_or_init(acfg["platform"], acfg["username"],
                                     created_at=acfg.get("created_at"))
        ph = st.phase()
        rng = self._seeded_rng(username, salt="live")
        plan = H.plan_session(rng, ph, username, acfg.get("keywords", []))

        if not live:
            return {"dry_run": True, "plan": plan}

        # Retry on any non-complete outcome (stuck / off-feed / wedged WDA /
        # lane-not-up). Each _drive tears its lane down in finally() and the next
        # attempt's open() cold-launches (terminate+relaunch) the app — so a
        # retry always CLOSES the stuck app and starts from a clean feed, which
        # is exactly what recovers a stuck session. A locked phone still just
        # fails twice quickly (harmless).
        import time
        level = trace_level or self.cfg.trace_level
        res = {}
        for attempt in range(1, max_attempts + 1):
            # Attempt 1 runs the full SOP (incl. keyword search). Retries warm the
            # home FEED ONLY: TikTok's keyword search is the main WDA-wedge trigger,
            # so a retry that re-runs the same search just re-wedges — FYP-only is
            # reliable and still a valid warm session.
            res = self._drive(acfg, plan, rng, st, level, engage_live,
                              allow_search=(attempt == 1))
            res["attempts"] = attempt
            if res.get("outcome") == "complete":
                return res
            if attempt < max_attempts:
                self._teardown_lane()               # force a fully clean retry
                time.sleep(H.jittered_delay(rng, 6.0))
        return res

    def _teardown_lane(self) -> None:
        """Force THIS device's WDA lane down between retry attempts, so the next
        attempt rebuilds it from scratch (recovers a wedged WDA / stuck app).

        Scoped to one device (Driver.force_teardown): the earlier blanket
        `pkill -f "dvt xcuitest"` also killed other phones' runners — fine while
        warm-all was strictly sequential, fatal once the posting agent drives a
        second phone at the same time. It also declines to touch a lane another
        process owns.
        """
        Driver(self.cfg, owner="autowarmer teardown", exclusive=False).force_teardown()

    # -- recorded live driving --------------------------------------------
    def _drive(self, acfg: dict, plan, rng, st, trace_level: str,
               engage_live: bool = True, allow_search: bool = True) -> dict:
        import time
        from .trace import RunRecorder
        username = acfg["username"]
        rec = RunRecorder(self.state_root / "runs", username, acfg["platform"],
                          level=trace_level, perceptor=Perceptor())
        driver = Driver(self.cfg)
        outcome, detail, completed = "incomplete", "", False
        try:
            with rec.step("ensure_lane", critical=True):
                if not driver.ensure_lane():
                    raise RuntimeError("WDA lane did not come up (phone offline / "
                                       "locked, or another driver holds port 8100)")
            driver.wda.new_session()
            driver.wda.configure()          # disable idle-wait (essential for TikTok)
            rec.bind(driver.wda)
            # Screen size: prefer the configured value (window_size() itself
            # wedges intermittently on TikTok). Thread it everywhere so nothing
            # calls window_size() while a never-idling app is foreground.
            if self.cfg.screen_points:
                w, h = int(self.cfg.screen_points[0]), int(self.cfg.screen_points[1])
            else:
                w, h = driver.wda.window_size()
            flow = AppFlow(acfg["platform"], GoIos(self.cfg), driver.wda, rec.p, screen=(w, h))
            eng = Engager(acfg["platform"], driver.wda, rng, screen=(w, h), perceptor=rec.p)
            nav = Navigator(acfg["platform"], driver.wda, rec.p,
                            advisor=make_advisor(self.cfg), recorder=rec, screen=(w, h))

            # 1) open + confirm the right account BEFORE any action (safety gate).
            with rec.step("open", arg=acfg["platform"], phase="setup") as s:
                flow.open()
                s.ok("launched")
            # 1a) settle on a KNOWN screen first. TikTok resumes wherever the
            #     last session died (video sub-pages, Add-Friends, composers) —
            #     verify's profile-tab tap is meaningless from a trap screen, so
            #     clear it BEFORE the account gate. Non-fatal: verify still has
            #     its own recovery.
            with rec.step("settle", phase="setup") as s:
                s.ok("feed" if nav.ensure_ready() else "not-feed")
            with rec.step("verify", arg=username, phase="setup", critical=True) as s:
                ver = flow.switch_account(username)
                if not ver.ok:
                    # a blocking onboarding modal (e.g. TikTok "Add your college")
                    # can hide the handle — clear it and retry once before failing.
                    n = nav.clear_blocking()
                    if n:
                        rec.log(f"cleared {n} modal(s) before re-verifying")
                        ver = flow.switch_account(username)
                s.extra["match_reason"] = ver.match.reason
                s.extra["onscreen"] = ver.match.matched_text
                if not ver.ok:
                    raise RuntimeError(f"could not confirm account @{username}: "
                                       f"{ver.match.reason}")
                s.ok("confirmed")

            # 2) get onto a real CONTENT FEED and confirm it. We NEVER scroll a
            #    non-feed screen (scrolling a static profile is a ban signal).
            with rec.step("goto_feed", phase="setup", critical=True) as s:
                if not nav.ensure_ready():
                    raise RuntimeError("could not reach a content feed "
                                       "(refusing to scroll a non-feed screen)")
                s.ok(nav.feed_tab)

            # 3) warm loop — a small state machine. Every action recorded; the feed
            #    is re-confirmed and adapted to (dismiss popups / re-navigate) so we
            #    never repeat a no-op or drift off-feed unnoticed.
            DEVICE = ("scroll", "like", "save", "share", "follow", "comment")
            NAV_ENGAGE = ("save", "share", "follow", "comment")   # can open sheets / navigate
            engage_kinds = ("like", "save", "share", "follow", "comment")
            dev_fail_streak = stuck = scrolls_since_check = 0
            engageable = None       # per-video: is the current item a standard post?
            _, last_sig = nav.probe()

            def try_reorient() -> bool:
                ok = False          # ensure_ready may raise (WDA timeout); rec.step
                                    # swallows it, so ok must be pre-bound or the
                                    # `return ok` below hits UnboundLocalError.
                with rec.step("reorient", phase="adapt") as rs:
                    # Dead-WDA gate: if the runner has wedged/died, ensure_ready()
                    # would tap a dead socket for ~90s (each tap blocks the full
                    # timeout) and park the app foregrounded. Bail in ~4s instead
                    # so the watchdog trips fast and the retry cold-relaunches the
                    # whole lane — the ONLY thing that recovers a wedged WDA.
                    if not driver.wda.alive():
                        rs.skip("wda-dead")
                        return False
                    ok = nav.ensure_ready()
                    rs.ok("on-feed") if ok else rs.skip("failed")
                return ok

            for act in plan.actions:
                if act.kind in ("open", "verify", "close"):
                    continue
                time.sleep(act.pre_delay)
                # SOP keyword transition: aged accounts search a niche keyword and
                # warm within the RESULTS instead of the home feed. Best-effort +
                # hard-gated — if we can't confirm a scrollable results surface,
                # fall back to the home feed (never scroll an unconfirmed screen).
                if act.kind == "search":
                    with rec.step("search", arg=act.arg, phase="setup") as s:
                        if not allow_search:
                            nav.ensure_ready()
                            s.skip("search off (retry: FYP-only)")
                        elif act.arg and flow.search(str(act.arg)) and nav.on_content():
                            s.ok(f"results:{act.arg}")
                        else:
                            # search hiccup/wedge — never scroll an unconfirmed
                            # screen; recover to the home feed and warm there.
                            nav.ensure_ready()
                            s.skip("search unconfirmed — home-feed fallback")
                    engageable = None
                    continue
                # End-of-session stories + DM browse (IG). Best-effort; held in
                # safe-live; never fails the run.
                if act.kind in ("stories", "inbox"):
                    with rec.step(act.kind, phase="warm") as s:
                        if not engage_live:
                            s.skip("held (safe-live)")
                        else:
                            try:
                                s.ok(flow.browse_stories_and_dms(rng)
                                     if act.kind == "stories" else flow.browse_inbox(rng))
                            except Exception as e:            # noqa: BLE001
                                s.skip(f"{act.kind}: {type(e).__name__}")
                    engageable = None
                    continue
                if act.kind in engage_kinds and not engage_live:
                    with rec.step(act.kind, arg=act.arg, phase="warm") as s:
                        s.skip("engagement held (safe-live: navigation only)")
                    continue
                # On TikTok, hold follow/share via fixed coords: the follow "+" is a
                # tiny target next to the avatar/promo overlays and share opens a
                # sheet — both drift-prone. like/save are in-place toggles on
                # isolated icons (reliable). follow/share will move to the vision
                # driver. IG keeps all actions (a11y-addressable, stable).
                if acfg["platform"] == "tiktok" and act.kind in ("follow", "share"):
                    with rec.step(act.kind, arg=act.arg, phase="warm") as s:
                        s.skip("tiktok: deferred to vision driver (drift-prone via fixed coords)")
                    continue
                # Skip engagement on non-standard TikTok items (LIVE = tap-trap,
                # ad = shifted rail / shop-link follow). Checked once per video.
                if act.kind in engage_kinds:
                    if engageable is None:
                        engageable = (nav.content_kind() == "video")
                        if not engageable:
                            rec.log("non-engageable feed item (live/ad) — holding engagement")
                    if not engageable:
                        with rec.step(act.kind, arg=act.arg, phase="warm") as s:
                            s.skip("live/ad — not engageable")
                        continue

                with rec.step(act.kind, arg=act.arg, phase="warm") as s:
                    if act.kind == "scroll":
                        eng.scroll_next(); s.ok("swipe"); engageable = None  # new video
                    elif act.kind in ("watch", "idle", "skip"):
                        time.sleep(float(act.arg)); s.ok("wait")
                    elif act.kind == "like":
                        s.ok(eng.like().how)
                    elif act.kind == "save":
                        r = eng.save(); s.ok(r.how) if r.ok else s.skip(r.how)
                    elif act.kind == "share":
                        s.ok(eng.share().how)
                    elif act.kind == "follow":
                        s.ok(eng.follow().how)
                    elif act.kind == "comment":
                        r = eng.comment(self._comment_text(acfg, driver.wda, rec.p, rng))
                        s.ok(r.how) if r.ok else s.skip(f"comment {r.how}")
                status = s.status

                if act.kind not in DEVICE:      # watch / idle / skip are pure waits
                    continue

                # watchdog: only DEVICE failures count (sleeps never reset it).
                if status == "fail":
                    dev_fail_streak += 1
                    # A wedged WDA cannot be recovered in-attempt — every further
                    # action just eats a ~20s timeout (that is how a TikTok-search
                    # wedge burned ~11 min before). Bail NOW so the retry cold-
                    # relaunches the lane (and, on retry, warms FYP-only).
                    if not driver.wda.alive():
                        outcome, detail = "aborted_wda", "WDA wedged — cold relaunch on retry"
                        rec.log(detail); break
                    try_reorient()
                    if dev_fail_streak >= 3:
                        outcome, detail = "aborted_wda", "3 consecutive device-action failures"
                        rec.log(detail); break
                    continue
                dev_fail_streak = 0

                # navigating engage (save/share/follow/comment) can leave the feed —
                # confirm IMMEDIATELY (catches drift in 1 step, not 5).
                if act.kind in NAV_ENGAGE:
                    if not nav.on_content():
                        rec.log(f"{act.kind} left the feed — recovering")
                        if not try_reorient():
                            outcome, detail = "aborted_offfeed", f"{act.kind} navigated away"
                            rec.log(detail); break
                        last_sig = None
                    continue

                if act.kind != "scroll":
                    continue

                # scroll: ONE cheap probe -> immediate drift + stuck detection (IG).
                is_feed, sig = nav.probe()
                if is_feed is False:                 # IG drifted off feed after scroll
                    rec.log("off feed after scroll — recovering")
                    if not try_reorient():
                        outcome, detail = "aborted_offfeed", "left the feed, could not return"
                        rec.log(detail); break
                    last_sig, stuck, scrolls_since_check = None, 0, 0
                    continue
                advanced = (sig is None) or (sig != last_sig)
                if not advanced:
                    stuck += 1
                    rec.log(f"feed did not advance (sig={sig!r}); adapting (stuck={stuck})")
                    with rec.step("unstick", phase="adapt") as us:
                        if stuck == 1:               # try a different, stronger swipe
                            eng.scroll_next(); us.ok("re-swipe")
                        else:                        # a modal is likely blocking — clear it
                            cleared = nav.force_clear()
                            ok = nav.ensure_ready()
                            us.ok(f"cleared={cleared}") if ok else us.skip("stuck")
                    if stuck >= 3:
                        outcome, detail = "aborted_stuck", "feed not advancing after adapts"
                        rec.log(detail); break
                else:
                    stuck = 0
                if sig is not None:
                    last_sig = sig

                # TikTok (probe returns None): periodic OCR drift check every 5 scrolls
                if is_feed is None:
                    scrolls_since_check += 1
                    if scrolls_since_check >= 5:
                        scrolls_since_check = 0
                        if not nav.on_content():
                            rec.log("drifted off the feed — re-navigating")
                            if not try_reorient():
                                outcome, detail = "aborted_offfeed", "left the feed, could not return"
                                rec.log(detail); break
            else:
                completed = True

            # 3) never leave the app foregrounded (that's a bot signal).
            with rec.step("close", arg=flow.bundle, phase="teardown") as s:
                # A dead WDA can't terminate anything (the call just blocks the
                # full timeout, then fails) — skip fast; the retry's lane
                # teardown + next attempt's cold open() closes the app instead.
                if driver.wda.alive():
                    driver.wda.terminate_app(flow.bundle); s.ok("terminated")
                else:
                    s.skip("wda-dead — retry cold-launch will close it")

            if completed:
                outcome, detail = "complete", ""
        except Exception as e:                          # noqa: BLE001
            if outcome == "incomplete":
                outcome = "aborted"
                detail = f"{type(e).__name__}: {e}"
            rec.log(f"aborted: {detail}")
        finally:
            # Never leave an app foregrounded — a bot signal. The happy/break
            # paths already terminate it; this catches the exception paths too
            # (best-effort; a wedged WDA may not respond, and that's fine).
            try:
                bundle = BUNDLE.get(acfg["platform"])
                if bundle and driver.wda.alive():   # fast bounded probe (see WDA.alive)
                    driver.wda.terminate_app(bundle)
            except Exception:
                pass
            driver.close()

        if outcome == "complete":
            st.completed_sessions += 1
            st.last_session_at = _dt.datetime.now().isoformat(timespec="seconds")
            self.store.save(st)
        run_dir = rec.finish(outcome, detail)
        return {"dry_run": False, "outcome": outcome, "detail": detail,
                "counts": dict(rec.counts), "run_dir": str(run_dir),
                "report": str(run_dir / "report.md")}

    def _comment_text(self, acfg: dict, wda, perceptor, rng) -> Optional[str]:
        """Produce a comment string for the current post. AI-first: OCR the screen
        for context and generate a short, casual, lowercase, <=5-word question.
        Falls back to the account's curated pool. None => skip commenting."""
        if self.cfg.ai_comments:
            from .ai import OpenRouterClient, generate_comment
            client = OpenRouterClient()
            if client.available:
                import os
                import tempfile
                fd, path = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                ctx = ""
                try:
                    if wda.screenshot(path):
                        ctx = " ".join(getattr(o, "text", "") for o in perceptor.ocr(path))
                except Exception:
                    ctx = ""
                text = generate_comment(client, ctx, acfg["platform"], self.cfg.ai_text_model)
                if text:
                    return text
        return self._pick_comment(acfg, rng)

    def _pick_comment(self, acfg: dict, rng) -> Optional[str]:
        """Return a curated comment for this account, or None to skip.

        Auto-commenting generic text is the single strongest spam signal, so we
        NEVER synthesize one: the account must opt in with `allow_comments` and
        supply its own niche-appropriate `comments` pool in config.
        """
        if not acfg.get("allow_comments"):
            return None
        pool = acfg.get("comments") or []
        return rng.choice(pool) if pool else None
