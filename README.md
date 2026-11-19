# Seestar ALP: roof automation edition

A fork of [Seestar ALP](https://github.com/smart-underworld/seestar_alp) for Seestars
in a roll-off observatory. It images on its own whenever the roof is open and it's
dark, parks when the roof closes or dawn comes, and puts the night on one screen,
Tonight, that works from your phone.

Everything upstream Seestar ALP does still works. This version adds the observatory
side on top, and takes in upstream's bug fixes.

## What it adds

- **Roof automation.** Watches an ASCOM Alpaca safety monitor, such as
  [PFR Sentinel](https://github.com/englishfox90/PFRSentinel) or any other. When the
  roof reads safe and it's astronomically dark, it opens the arm and starts your
  targets. When the roof closes, or at dawn, it stops and parks. It picks up again if
  the roof reopens the same night, and skips targets that sink below your altitude
  limit.
- **Target list and night planner.** Add targets with their framing, exposure,
  filter and camera. Each night it plans around the dark hours, the moon, your
  horizon and how much time each target still needs, and replans when the roof
  closes or a target fails.
- **Tonight.** The sky chart with sun, moon and roof, the plan, the live stack, the
  pier camera, and what the automation is doing and why.
- **Mosaics.** Framed and traditional mosaics, a guard that catches a framed mosaic
  that lost its framing, and wide-camera mosaics on the S30 Pro.
- **Wide angle camera** on the S30 Pro, alone or both cameras at once.
- **Discord alerts** for the night's events, alarms and finished stacks.
- **Automatic dew heater,** switched on and off by the dew point margin.
- **Starfront site status** (optional): your building's roof state and camera, the
  Clear Sky chart and site announcements.
- **A phone-first interface** you can add to your home screen.

## Install

Tested on macOS (Apple silicon), running from source. Windows and Linux run the same
code from source but haven't been tested with this fork, and the start-at-boot
script below is Mac only.

You need Python 3.13 and git. On a Mac, with [Homebrew](https://brew.sh):

```bash
brew install python@3.13 git
git clone https://github.com/englishfox90/seestar_alp.git ~/seestar_alp
cd ~/seestar_alp
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python root_app.py
```

Then open <http://localhost:5432>. The first start creates `device/config.toml`,
with the automation off and in a dry run.

To start it at boot, with no one logged in (Mac):

```bash
roof/service.sh install     # asks for your password
roof/service.sh status      # also: logs, restart, stop, uninstall
```

To update: `git pull`, then `.venv/bin/pip install -r requirements.txt`, then
`roof/service.sh restart`. It refuses while the scope is imaging, so do it in the
day.

## Set up

Everything is done in the app, with no files to edit.

1. **Scope and site.** In System → Config, set your Seestar's IP address and your
   site's latitude and longitude. Newer Seestar firmware also needs a client key
   file (`interop_pem` on the same page): get one by following upstream Seestar
   ALP's instructions. Restart after saving.
2. **Safety monitor.** In Plan → Automation, under Roof safety, press **Find safety
   monitors** and **Use** yours, or enter its address. **Test** shows what it reads
   now. Any ASCOM Alpaca SafetyMonitor works; [roof/README.md](roof/README.md) lists
   the options.
3. **Optional, on the same page:** PFR Sentinel's web server (pier camera and
   weather), your Starfront building number, the scope's MyWorks folder, and
   Discord alerts.
4. **Targets.** Add them in Plan → Targets. Then, in Automation, choose **Target
   list** as the image source and press **Enable automation**.
5. **Go live.** It starts in a dry run that only logs what it would do. Watch a
   night of that, then press **Go live**.

[roof/README.md](roof/README.md) explains what the automation does in each situation.

## Your data stays on your computer

Your config, schedules, plans, targets, session history and logs are git-ignored,
so `git pull` never touches them and none of it ends up in the repository.

## Upstream Seestar ALP

This is built on Seestar ALP from the Smart Telescope Underworld community. For
everything it shares with upstream, upstream's documentation and support apply:

- Wiki: <https://github.com/smart-underworld/seestar_alp/wiki>
- Discord: <https://discord.gg/B3zDCAMP4V>
- Source and releases: <https://github.com/smart-underworld/seestar_alp>

For anything specific to this fork (roof automation, Tonight, the planner), open an
issue here rather than upstream.
