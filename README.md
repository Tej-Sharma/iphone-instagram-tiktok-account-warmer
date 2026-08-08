# AutoWarmer

Warm up **your own** Instagram and TikTok accounts on **your own** iPhone.

AutoWarmer drives a real iPhone over USB and behaves like a person using it:
it opens the app, checks it is on the right account, watches a feed, and
occasionally likes, saves or follows. Every account follows a day-by-day ramp
measured from the date it was created, so a fresh account browses quietly and
an older one does more.

Everything runs on your Mac. No account is created for you, nothing is
uploaded, and there is no server involved.

## Download

Grab the latest **AutoWarmer-x.y.z.zip** from
[Releases](https://github.com/Tej-Sharma/iphone-instagram-tiktok-account-warmer/releases/latest), unzip it, then **right-click
`AutoWarmer.command` and choose Open** the first time (macOS blocks anything
downloaded from the internet on a plain double-click).

Or run it from a clone:

```bash
python3 -m autowarmer
```

The dashboard opens at `http://127.0.0.1:8790`.

## What you need

| Thing | What it does | Who installs it |
|---|---|---|
| macOS + full Xcode | builds the helper app that drives your phone | you, from the App Store |
| git | fetches that helper app's source | you, `xcode-select --install` |
| go-ios | talks to the phone over USB | **AutoWarmer** |
| pymobiledevice3 | starts the helper app on the phone | **AutoWarmer** |
| An Apple Developer membership | Apple requires the phone's owner to sign the helper app | you (paid tier; free Apple IDs cannot issue API keys) |

Setup asks for an App Store Connect API key (`.p8`) plus its Key ID, Issuer ID
and Team ID. AutoWarmer keeps a private copy in `~/.autowarmer/keys` (readable
only by you) and never sends it anywhere.

## How it works

```
config.json ─▶ incubation ─▶ humanize ─▶ engine ─▶ apps ─▶ device ─▶ iPhone
 accounts       day → phase   coin-flips  orchestr. open/    go-ios +
 + interests    ramp          + skewed              verify   pymobiledevice3
                              dwell times                    + WebDriverAgent
```

Nothing emits a quota. Each like, save or follow is an independent chance;
watch times are drawn from a right-skewed distribution; sessions land on a
daily rhythm with a sleep gap. The loop refuses to scroll a screen it has not
confirmed is a feed, adapts when the feed stops advancing, and stops after
repeated failures.

**Practice mode** (`--no-engage`, or the Practice button) drives everything for
real but holds every like, follow and save. Use it the first time on any phone.

## Command line

```bash
python3 -m autowarmer doctor          # what's installed, what's missing
python3 -m autowarmer install all     # fetch what can be fetched
python3 -m autowarmer status          # each account's day, phase and rates
python3 -m autowarmer warm <handle>   # dry run; --live to drive the phone
python3 -m autowarmer warm-all        # every connected phone, in turn
python3 tests/test_autowarmer.py      # the test suite
```

## Scope

AutoWarmer warms accounts. It does not post. Automated posting, multi-phone
fleet operation and managed warmed accounts are commercial products — contact
**team@earshot.to**.

Use it only on accounts and devices you own, and follow the terms of the
platforms you use it with.

## Licence

[Business Source License 1.1](LICENSE). In short: use it freely for your own
accounts on your own devices, including commercially. You may not use it to
provide a service to third parties or to compete with the Licensor. It converts
to Apache 2.0 on 2030-08-07. For any other arrangement, contact team@earshot.to.

— built by [Earshot](https://earshot.to)
