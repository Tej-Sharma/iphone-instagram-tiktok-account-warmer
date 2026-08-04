# iPhone Instagram / TikTok Account Warmer: Full Technique

A human-mimicking iOS warm-up driver. It drives **your own** iPhones over USB
(via WebDriverAgent / XCUITest) to warm up **your own** Instagram & TikTok
accounts on a day-by-day incubation ramp — behavior drawn from human-shaped
distributions rather than fixed quotas. Built for legitimate creators and account
managers running self-publishing at scale. **Not** a spam farm.

---

## How it works

Python drives real iPhones through a self-signed WebDriverAgent runner (go-ios
kernel tunnel + pymobiledevice3 = a no-password lane on iOS 26). It opens the
app, confirms the correct account, then behaves like a human on an incubation
ramp — every action an independent coin-flip, never a quota. Because the taps are
real HID touches on a real device, the hardest-to-fake signals (sensors, GPU,
touch, IP) are authentic; the software only closes the behavioral gap.

## Why it's different

The load-bearing insight: platform detection scores the **statistical shape** of
behavior (skewed, noisy, circadian) — not fixed volume. So this never emits
quotas: right-skewed watch times, multiplicative delay jitter, scattered taps,
and a circadian rhythm with a shifting sleep window.

## Technique overview

Detection isn't beaten by hiding — it's beaten by being statistically
indistinguishable from a real person on a real phone. The approach stacks three
layers:

**1. Real device, real touches.** Everything runs on physical iPhones through a
self-signed WebDriverAgent/XCUITest runner. Taps are injected as ordinary
`UITouch` HID events — there is no synthetic-input flag, and the runner's
identity is invisible across the app sandbox boundary. Authentic sensors, GPU,
battery, and residential IP are things a data-center emulator can never fake.

**2. Human-shaped behavior, never quotas.** The core finding: platforms score the
*distribution* of behavior, not its volume. A bot that likes exactly N posts
every 15 minutes with ±3s jitter is still trivially wrong-shaped. So the driver
emits no fixed numbers:
- **Per-item coin-flips** — each like / follow / save / comment is an independent
  probability draw, so counts vary naturally run to run.
- **Right-skewed dwell times** — watch durations follow a lognormal curve (many
  short, a few long), the way real attention actually decays.
- **Multiplicative delay jitter + scattered taps** — no two gaps or tap points
  are the same; nothing lands on a fixed pixel.
- **Circadian scheduling** — ~2 randomized sessions/day inside a daily rhythm with
  a shifting sleep window; no 3 a.m. activity, no metronomic cadence.

**3. Ramp + content discipline.** An age-based incubation curve raises engagement
rates gradually (a week-old account behaves nothing like a month-old one), and the
session logic always *consumes before it engages* and warms the home feed before
graduating to keyword search — mirroring how a real user discovers content.

The result: the easy-to-fake layer (behavior) is shaped to match humans, and the
hard-to-fake layer (a genuine iPhone) is simply real.

## Features

**Lane & Safety**
- Own WDA/XCUITest runner over a no-password iOS-26 lane; injects real HID touches, no bot flag.
- Account verified on-screen before any action; one-driver-per-phone locking; native permission prompts auto-denied.

**Human Behavior**
- Right-skewed watch times, jittered delays, scattered taps, ~2 randomized sessions/day on a circadian schedule.
- Age-based incubation ramp: consume-before-engage, then keyword-search — like / save / share / follow / comment.

**Reliability**
- Feed-gated scrolling (never scrolls a non-feed screen) with stuck/drift adaptation and popup escape.
- Fast wedged-runner detection with cold-relaunch auto-retry for unattended operation.

**Observability**
- Recorded runs (screenshots + OCR + accessibility trail) with green/red status and a live log.
- Dashboard: phones × accounts grid tracking keywords, day-since-creation, stage, and run history.

---

## Status

Actively developed and running against a live fleet. Public code release is being
prepared — check back soon.

## License

TBD.
