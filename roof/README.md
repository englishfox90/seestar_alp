# Roof automation (fork-only)

Runs one stored schedule whenever PFR Sentinel reads the roof safe and it's dark,
and stops + parks when it isn't. Page: `http://localhost:5432/roof`.

## Behaviour

| Situation | What happens |
|---|---|
| Roof safe for `start_settle_s` and sun below `sun_max_deg` | Opens the arm (never polar aligns), imports and starts the selected schedule |
| Roof unsafe for `stop_grace_s` | Stops + parks, even a session started elsewhere |
| Roof safe again, same night | Resumes at the interrupted item, which runs its full time again |
| Next night | Starts from item 1 |
| Sun rises above `sun_max_deg` | Stops + parks |
| Schedule finishes | Parks and waits for the next night |
| Stop + Park button, or scheduler stopped elsewhere | Holds until Start or the next night |
| Start button | Ignores the roof until it next reads safe, but still requires dark |
| Different schedule selected | Applies at the next start; clears the resume point and the "finished tonight" state |
| Session started elsewhere, roof safe | Left alone |

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
sun_max_deg = -15.0
poll_s = 10
start_settle_s = 120
stop_grace_s = 30
```

Changes to this section apply on the next tick, with no restart.

## Service

```
roof/service.sh install     # start now and at every login
roof/service.sh status
roof/service.sh logs        # follow roof_automation.log
roof/service.sh restart     # refuses while imaging; --force overrides
roof/service.sh stop
```

## Upstream merges

```
git fetch upstream && git merge upstream/main
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/test_roof.py -q
```

Upstream files touched: one hook in `FrontMain.start` (`front/app.py`) and one
line for the badge in `front/templates/nav.html`. Everything else is new files.

## Files

- `automation.py`: the loop and every decision
- `scope.py`: device calls, plus the dry-run stand-in
- `sentinel.py`, `sun.py`: the two gates
- `schedule_tools.py`: listing, validation, app-plan conversion
- `web.py`: page, buttons and badge
