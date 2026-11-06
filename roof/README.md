# Roof automation (fork-only)

Runs one stored schedule whenever PFR Sentinel reads the roof safe and it's dark,
and stops + parks when it isn't. Page: `http://localhost:5432/roof`.

## Behaviour

| Situation | What happens |
|---|---|
| Roof safe for `start_settle_s`, sun below `sun_max_deg`, and a target above `min_target_alt_deg` | Opens the arm (never polar aligns), imports and starts the selected schedule at the first target that's high enough |
| Roof opens before dark | Waits, parked, until the sun drops below `sun_max_deg` |
| Current target falls below `min_target_alt_deg` | Skips to the next item. A skipped target doesn't come back that night |
| Roof unsafe for `stop_grace_s` | Stops + parks, even a session started elsewhere |
| Roof safe again, same night | Resumes at the interrupted item, which runs its full time again |
| Next night | Starts from item 1 |
| Sun rises above `sun_max_deg` | Stops + parks |
| Schedule finishes | Parks and waits for the next night |
| Stop + Park button, or scheduler stopped elsewhere | Holds until Start or the next night |
| Start button | Ignores the roof until it next reads safe, but still requires dark and a high enough target |
| Target list selected | Each start plans the rest of the night from the list instead of resuming, and the plan ends at any gap of 30 min or more; it parks, then replans when the next target is due |
| Plan from the target list ends early | Under half its planned time means a target failed: alarm, and done until Start or the next night. At most 8 runs a night |
| Different schedule selected | Applies at the next start; clears the resume point and the "finished tonight" state |
| Session started elsewhere, roof safe | Left alone |

The dew heater uses the smaller margin from Sentinel's weather and Open-Meteo, stays
on if neither answers, and turns off when a session ends. Both are regional
estimates, not a sensor at the scope.

Nights change at local solar noon. Hold, finished and resume state are kept in
`roof_state.json` beside config.toml, so an ALP restart doesn't forget them.

## Config

In `device/config.toml`. Site lat/long come from `[seestar_initialization]`.

```toml
[roof]
enabled = false         # also the page's toggle
dry_run = true          # only a literal false arms it; deliberately not on the page
schedule = ""           # .json name in schedule/; also the page's selector
device_num = 1
sentinel_url = "http://127.0.0.1:11111"
safety_device = 0
sun_max_deg = -18.0     # astronomical darkness; also parks when the sun rises past it
min_target_alt_deg = 20.0  # 0 turns this off
dew_enabled = false     # automatic dew heater during sessions
dew_on_margin_c = 4.0   # on when temperature minus dew point falls to this
dew_off_margin_c = 6.0  # off when it rises to this
dew_power = 30          # percent
dew_sentinel_url = "http://127.0.0.1:8085/status"
poll_s = 10
start_settle_s = 120
stop_grace_s = 30
```

Changes to this section apply on the next tick, with no restart.

## Service

```
roof/service.sh install     # start now and at every boot (asks for your password);
                            # refuses while imaging; --force overrides
roof/service.sh status
roof/service.sh logs        # follow roof_automation.log
roof/service.sh restart     # refuses while imaging; --force overrides
roof/service.sh stop
```

`run.sh` waits for the `[network] ip_address` to exist before starting, so binding to
a ZeroTier address survives a reboot.

Build the venv from `/opt/homebrew/bin/python3.13`. Under launchd, macOS gave the
Intel Python in `/usr/local` "No route to host" reaching the scope.

## Upstream merges

```
git fetch upstream && git merge upstream/main
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest tests/test_roof.py -q
```

Upstream files touched: one hook in `FrontMain.start` (`front/app.py`), and in
`front/templates/nav.html` one line for the badge and one menu entry for Target List. Everything else is new files.

## Files

- `automation.py`: the loop and every decision
- `scope.py`: device calls, plus the dry-run stand-in
- `sentinel.py`, `sun.py`: the two gates
- `schedule_tools.py`: listing, validation, app-plan conversion
- `web.py`: page, buttons and badge
- `queue.py`, `queue_web.py`: the target list and its page, `/targets`
- `planner.py`: picks targets for a night (`python -m roof.planner --nights 7`)
- `dashboard.py`: `/dashboard`, including tonight's plan and goal progress from the list
- `sessions.py`: what was actually stacked, kept in `roof_sessions.json`
- `night_history.py`: when the roof read safe or unsafe, kept in `roof_night_history.json`
- `night_web.py`: read-only roof history, morning summary and inline image routes
