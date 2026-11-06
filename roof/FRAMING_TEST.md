# Framed mosaic test: why IC 1805 lost its framing

We don't yet know why it failed. On 2026-11-06 it stacked at scale 1, angle 0 at 02:36, as item 3
of 3, after a single-frame item. Earlier that night it held 1.7 when it ran first. The
one solid clue: the `Setting` event that reset it has the same firmware timestamp as
the `iscope_start_stack` reply. Autofocus, 02:35 to 02:36, produced no `Setting` events.

The guard and these log lines have never run on the real scope. This test is their
first run too.

## Before you start

- Run this branch's code and restart the app in the usual way. Check nothing is imaging.
- Leave roof gating on, with `enabled = true` and `dry_run = false` as normal. Change
  nothing in Sentinel. Don't use Start to get around an unsafe roof: Start ignores the
  roof until it next reads safe, so press it only when Tonight shows the roof safe.
- Use a stored schedule, not the target list, so the list's replanning stays out of it.
- Don't run `pytest` from `~/src/seestar_alp` until you've read the results. Test runs
  from that folder have rotated `alpyca.log` and added fake alarms to
  `roof_automation.log`: "Horizon margin 'abc'" is in the rotated logs from 09:51.

## Where to look

`roof_framing_diag.log`, beside `roof_automation.log`. It never rotates and uses local
time. `alpyca.log` has the same lines, stamped in UTC.

```
tail -f roof_framing_diag.log
```

One line per read, while the framing is checked:

```
framing Seestar Alpha IC 1805 after_goto: asked scale 1.7 angle 67.0 scope scale 1.7 angle 67.0 est_h=1.375 star_map=69.054 ratio=1.7 match
```

| Step, in order | Taken |
|---|---|
| `after_goto` | After the view start and goto |
| `after_autofocus` | After autofocus, if the item uses it |
| `after_set_stack_type` | Before the stack starts |
| `after_start_stack` | Right after `iscope_start_stack`: the guard's decision |
| `retry_after_view`, `retry_after_set_stack_type`, `retry_after_start_stack` | During a retry only |
| `guard: held`, `unverified`, `retrying`, `restored`, `unconfirmed`, `skipped`, `interrupted` | The outcome |

Also save, right after each run, the `Setting` events from the log:

```
grep "'Event': 'Setting'" alpyca.log
```

## The runs

Each run takes 3 to 5 minutes to give its answer. Then press Stop + Park, select the
next schedule, wait until Tonight shows the roof safe, and press Start.

The baseline item is the one from last night, in `schedule/Cygnus.json` format, with
`stack_l` set to 30000 in the `set_setting` item before it:

```json
{"action": "start_framed_mosaic", "params": {"target_name": "IC 1805", "ra": 2.711, "dec": 61.246,
 "is_j2000": true, "is_use_lp_filter": true, "is_use_autofocus": true, "panel_time_sec": 10920,
 "gain": 200, "mosaic_scale": 1.7, "mosaic_angle": 67.0, "mosaic_star_map_angle": 69.054001}}
```

- [ ] **Run 1:** baseline, first in the schedule.
- [ ] **Run 2:** a short single-frame `start_mosaic` (`panel_time_sec` 300) on any target that's up, then the baseline.

Run these only if run 1 or run 2 goes wrong, on whichever arrangement failed, changing
one thing each time:

- [ ] **Run 3:** `is_use_autofocus` false.
- [ ] **Run 4:** `stack_l` 20000.
- [ ] **Run 5:** `panel_time_sec` 4800, under the scope's 1.375 h estimate.

## What each result means

| What you see | What it means |
|---|---|
| All lines `match`, `guard: held` | It held in this run. Say nothing about a cause from a single pass |
| `after_set_stack_type` `match`, then `after_start_stack` `MISMATCH` | Same as last night: it changed across the stack start |
| `after_autofocus` `match`, `after_set_stack_type` `MISMATCH` | It changed at `set_stack_type`, which last night's log didn't show |
| `after_goto` `match`, `after_autofocus` `MISMATCH` | It changed during autofocus, which last night's log didn't show |
| `after_goto` `MISMATCH` | The view start didn't take the mosaic. Check the ALARM and the first `Setting` event |
| `guard: restored` | The retry worked: stacking at the right framing, about a minute later than planned. Note it's a stack with no autofocus just before it |
| `retry_after_view` `match`, `retry_after_start_stack` `MISMATCH`, `guard: skipped` | The stack start undoes it even straight after a view start, so a retry can't fix this and the item is skipped |
| `retry_after_view` `MISMATCH`, `guard: skipped` | The view start didn't take the mosaic the second time. No second stack was started |
| `guard: skipped` with "had not finished" or "did not complete" | The firmware didn't finish a view start when already on target. The guard's assumption is wrong, so tell Claude |
| `scope unreadable`, `guard: unverified` | The read failed, so it kept stacking as it did before the guard. Tonight's backstop alarm is the only check |
| `guard: held`, then a later `Setting` event at scale 1 | It reset after the guard looked, which the guard can't catch. The backstop may also miss it, as it checks each item once |

Whatever the outcome, the stack must be stopped, or the next item started, within a
minute or two of `skipped`. If Tonight shows no alarm after `retrying`, `restored`,
`unconfirmed` or `skipped`, that's a bug.

## The four candidates

| Candidate | In if | Out if |
|---|---|---|
| Followed a single-frame item | Run 1 holds and run 2 resets, and a repeat of the pair agrees | Run 1 resets too, or run 2 holds |
| Autofocus just before | Run 3 holds where its baseline reset | Run 3 resets anyway. `restored` on a retry, which has no autofocus, is a hint only |
| 30 s subs, not 20 s | Run 4 holds where its baseline reset | Run 4 resets anyway |
| Planned 3.03 h against the 1.375 h estimate | Run 5 holds where its baseline reset | Run 5 resets anyway. The code doesn't send `panel_time_sec` to the scope, so this looks unlikely, but the firmware's own reasons aren't visible |

A single run proves little if the fault is intermittent. Treat a cause as ruled in only
after it has gone wrong and come right at least twice. If runs 1 and 2 both hold, the
fault didn't appear and none of the four is ruled in or out. If nothing explains it,
keep the guard running and compare `roof_framing_diag.log` across nights.

## Afterwards

Keep `roof_framing_diag.log`, the `grep` output and the alarm text. Set `schedule` back
to the normal one, and check the scope is parked and Tonight shows no new alarm.
