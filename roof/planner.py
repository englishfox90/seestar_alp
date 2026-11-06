"""Decide what to image on a night, from the target list.

A lite version of what N.I.N.A.'s Target Scheduler does: drop targets that can't
be imaged, score the rest, take the best, repeat. Moon position and altitude are
predictable, so one pass over the night produces a whole schedule; an interruption
replans from then rather than needing a decision before every block.

Run ``python -m roof.planner --nights 7`` to see the plan without touching the scope.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import ephem

from roof import schedule_tools
from roof.sun import night_key, sun_altitude_deg

STEP_S = 300
# Parking for a gap this long beats leaving the arm open; the automation replans after.
PARK_GAP_S = 1800


@dataclass(frozen=True)
class PlanConfig:
    lat: float
    lon: float
    sun_max_deg: float = -18.0
    min_alt_deg: float = 20.0
    horizon: object = None  # device.horizon.HorizonProfile, or None for flat
    horizon_margin_deg: float = 0.0
    switch_overhead_s: float = 180.0  # slew + plate solve + autofocus
    shortest_tail_s: float = 900.0  # least worth continuing a target for
    # Stacked time per wall-clock time, until a target has its own measurement.
    # Framed is from one 2026-11-05 session (59%); single is an assumption.
    rate_framed: float = 0.6
    rate_single: float = 0.85
    # Target Scheduler's default weights.
    weight_priority: float = 0.5
    weight_percent_complete: float = 0.5
    weight_setting_soonest: float = 0.5
    weight_stay_on_target: float = 0.67
    weight_last_chance: float = 1.0
    # Moon separation/width in deg/days at full moon. LP is Target Scheduler's
    # default; no filter is a stricter starting guess.
    moon_lp: tuple = (60.0, 7.0)
    moon_no_filter: tuple = (120.0, 14.0)


@dataclass
class Block:
    target: object
    start: float
    end: float
    overhead_s: float
    expected_hours: float = 0.0

    @property
    def stack_s(self) -> int:
        return int(self.end - self.start - self.overhead_s)


@dataclass
class NightPlan:
    key: str
    dark_start: float | None
    dark_end: float | None
    moon_percent: float = 0.0
    blocks: list[Block] = field(default_factory=list)
    skipped: dict = field(default_factory=dict)  # target id -> reason


def moon_required_separation(age_days: float, separation: float, width: float) -> float:
    """Lorentzian moon avoidance (BAIT/ACP), as Target Scheduler uses it."""
    return separation / (1.0 + ((0.5 - age_days / 29.5) / (width / 29.5)) ** 2)


def night_noon(key: str, lon: float) -> float:
    """Solar noon that opens the night named ``key`` (see sun.night_key)."""
    day = datetime.fromisoformat(key).replace(tzinfo=timezone.utc)
    return (day + timedelta(hours=12 - lon / 15.0)).timestamp()


class _Night:
    """Sampled sky for one night: dark steps, moon, and each target's usability."""

    def __init__(self, key: str, cfg: PlanConfig):
        self.key, self.cfg = key, cfg
        self.noon = night_noon(key, cfg.lon)
        self.times: list[float] = []
        for k in range(int(86400 / STEP_S)):
            when = self.noon + k * STEP_S
            moment = datetime.fromtimestamp(when, timezone.utc)
            if sun_altitude_deg(cfg.lat, cfg.lon, moment) < cfg.sun_max_deg:
                self.times.append(when)
            elif self.times:
                break
        self._observer = ephem.Observer()
        self._observer.lat, self._observer.lon = str(cfg.lat), str(cfg.lon)
        self._observer.pressure = 0
        self._moon = []
        self.moon_percent = 0.0
        moon = ephem.Moon()
        for when in self.times:
            self._observer.date = self._ephem_date(when)
            moon.compute(self._observer)
            age = self._observer.date - ephem.previous_new_moon(self._observer.date)
            self._moon.append((math.degrees(moon.alt), float(moon.ra), float(moon.dec), age))
            self.moon_percent = max(self.moon_percent, moon.phase)

    @staticmethod
    def _ephem_date(when: float):
        return ephem.Date(datetime.fromtimestamp(when, timezone.utc).replace(tzinfo=None))

    def masks(self, target) -> tuple[list[bool], list[bool]]:
        """(high enough, high enough and clear of the moon) for each dark step."""
        cfg = self.cfg
        body = ephem.FixedBody()
        body._ra = ephem.hours(math.radians(target.ra * 15.0))
        body._dec = ephem.degrees(math.radians(target.dec))
        body._epoch = ephem.J2000
        default = cfg.moon_lp if target.lp_filter else cfg.moon_no_filter
        separation = target.moon_sep_deg if target.moon_sep_deg is not None else default[0]
        width = target.moon_width_days if target.moon_width_days is not None else default[1]
        high, clear = [], []
        for when, (moon_alt, moon_ra, moon_dec, age) in zip(self.times, self._moon):
            self._observer.date = self._ephem_date(when)
            body.compute(self._observer)
            altitude = math.degrees(body.alt)
            floor = cfg.min_alt_deg
            if cfg.horizon is not None:
                floor = max(
                    floor,
                    cfg.horizon.altitude_at(math.degrees(body.az)) + cfg.horizon_margin_deg,
                )
            above = altitude >= floor
            distance = math.degrees(ephem.separation((body.ra, body.dec), (moon_ra, moon_dec)))
            moon_ok = moon_alt <= 0 or distance >= moon_required_separation(age, separation, width)
            high.append(above)
            clear.append(above and moon_ok)
        return high, clear


def _run_steps(mask: list[bool], index: int) -> int:
    steps = 0
    while index + steps < len(mask) and mask[index + steps]:
        steps += 1
    return steps


def _longest_run(mask: list[bool]) -> int:
    best = run = 0
    for ok in mask:
        run = run + 1 if ok else 0
        best = max(best, run)
    return best


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h"


def _rate(target, cfg, rates) -> float:
    measured = (rates or {}).get(target.id)
    if measured:
        return measured
    return cfg.rate_framed if target.framed else cfg.rate_single


def plan_night(targets, remaining: dict, key: str, cfg: PlanConfig, start=None, rates=None) -> NightPlan:
    """Plan one night. ``remaining`` maps target id to integration hours still
    wanted, and is updated with what the plan expects to deliver."""
    night = _Night(key, cfg)
    plan = NightPlan(
        key=key,
        dark_start=night.times[0] if night.times else None,
        dark_end=night.times[-1] + STEP_S if night.times else None,
        moon_percent=night.moon_percent,
    )
    masks, active = {}, []
    for target in targets:
        if not target.enabled:
            plan.skipped[target.id] = "disabled"
        elif remaining.get(target.id, 0) <= 0:
            plan.skipped[target.id] = "goal reached"
        else:
            high, clear = night.masks(target)
            masks[target.id] = (high, clear)
            active.append(target)

    index = 0
    if start is not None:
        while index < len(night.times) and night.times[index] < start:
            index += 1
    first_index = index
    previous = None
    while index < len(night.times) and active:
        best, best_score, best_choice = None, -1.0, None
        for target in active:
            if remaining[target.id] <= 0:
                continue
            clear = masks[target.id][1]
            run_s = _run_steps(clear, index) * STEP_S
            continuing = previous is not None and previous.target is target
            overhead = 0.0 if continuing else cfg.switch_overhead_s
            available = run_s - overhead
            minimum_s = target.min_session_hours * 3600
            needed_s = remaining[target.id] * 3600 / _rate(target, cfg, rates)
            if continuing:
                # Same stack keeps growing, so any useful tail will do.
                length = min(minimum_s, needed_s, available)
                if available < min(cfg.shortest_tail_s, needed_s):
                    continue
            else:
                # A framed run needs its whole minimum even to finish a goal; a
                # single frame only needs what's left.
                length = minimum_s if target.framed else min(minimum_s, needed_s)
                if available < length:
                    continue
            score = _score(target, remaining, cfg, night, index, run_s, continuing, available)
            if score > best_score:
                best, best_score, best_choice = target, score, (overhead, length)
        if best is None:
            index += 1
            previous = None
            continue
        overhead, length = best_choice
        steps = max(1, math.ceil((overhead + length) / STEP_S))
        start_at = night.times[index]
        end_at = start_at + steps * STEP_S
        stack_s = end_at - start_at - overhead
        delivered = stack_s * _rate(best, cfg, rates) / 3600
        remaining[best.id] -= delivered
        if previous is not None and previous.target is best:
            previous.end = end_at
            previous.expected_hours += delivered
        else:
            previous = Block(best, start_at, end_at, overhead, delivered)
            plan.blocks.append(previous)
        index += steps

    planned = {b.target.id for b in plan.blocks}
    for target in active:
        if target.id not in planned:
            plan.skipped[target.id] = _why_not(target, masks[target.id], cfg, first_index)
    return plan


def _score(target, remaining, cfg, night, index, run_s, continuing, available) -> float:
    priority = {"low": 0.0, "normal": 0.5, "high": 1.0}.get(target.priority, 0.5)
    goal = max(target.goal_hours, 0.001)
    done = max(0.0, min(1.0, 1 - remaining[target.id] / goal))
    window_end = night.times[index] + run_s
    setting = 1 - (window_end - night.noon) / 86400
    score = (
        cfg.weight_priority * priority
        + cfg.weight_percent_complete * done
        + cfg.weight_setting_soonest * setting
        + (cfg.weight_stay_on_target if continuing else 0.0)
    )
    # Greedy picks could eat a framed target's only long window an hour at a time.
    minimum_s = target.min_session_hours * 3600
    if target.framed and not continuing and available - 3600 < minimum_s:
        score += cfg.weight_last_chance
    return score


def _why_not(target, masks, cfg, first_index) -> str:
    """Why a target got no time, judged on what's left of the night."""
    high, clear = masks[0][first_index:], masks[1][first_index:]
    if not any(high):
        return f"not above {cfg.min_alt_deg:g} deg in the dark for the rest of the night"
    if not any(clear):
        return "too close to the moon whenever it's up"
    longest = _longest_run(clear) * STEP_S - cfg.switch_overhead_s
    if longest < target.min_session_hours * 3600:
        kind = "framed run" if target.framed else "session"
        return (
            f"longest clear window {_hours(max(longest, 0))} is under its "
            f"{target.min_session_hours:g} h {kind}"
        )
    return "other targets scored higher"


def remaining_hours(targets, log) -> dict:
    """Integration hours still wanted per target id."""
    return {
        t.id: max(0.0, t.goal_hours - t.prior_hours - log.progress(t).counted_hours)
        for t in targets
    }


def measured_rates(targets, log) -> dict:
    rates = {}
    for target in targets:
        rate = log.progress(target).measured_rate
        if rate:
            rates[target.id] = max(0.05, min(1.0, rate))
    return rates


def forecast(targets, log, cfg: PlanConfig, now: float, nights: int = 14) -> list[NightPlan]:
    """Plans for this night and the next, assuming every one is clear."""
    remaining = remaining_hours(targets, log)
    rates = measured_rates(targets, log)
    day = datetime.fromisoformat(night_key(now, cfg.lon))
    plans = []
    for offset in range(nights):
        key = (day + timedelta(days=offset)).date().isoformat()
        plans.append(
            plan_night(targets, remaining, key, cfg, start=now if offset == 0 else None, rates=rates)
        )
    return plans


def finish_nights(plans: list[NightPlan], targets, log) -> dict:
    """Target id -> key of the night its goal is expected to be met, if in range."""
    remaining = remaining_hours(targets, log)
    finished = {}
    for plan in plans:
        for block in plan.blocks:
            remaining[block.target.id] -= block.expected_hours
            if remaining[block.target.id] <= 1e-6 and block.target.id not in finished:
                finished[block.target.id] = plan.key
    return finished


def to_schedule(plan: NightPlan) -> dict:
    """A schedule the automation can run, up to the first gap worth parking for."""
    items: list[dict] = []
    cursor = plan.blocks[0].start if plan.blocks else None
    exposure = None
    for block in plan.blocks:
        target = block.target
        gap = int(block.start - cursor)
        if gap >= PARK_GAP_S:
            break
        if gap > 0:
            items.append({"action": "wait_for", "params": {"timer_sec": gap}})
        cursor = block.end
        exposure_ms = int(target.exposure_s * 1000)
        if exposure_ms != exposure:
            items.append(
                {"action": "set_setting", "params": {"exp_ms": {"stack_l": exposure_ms, "continuous": 500}}}
            )
            exposure = exposure_ms
        mosaic = None
        if target.framed:
            mosaic = {"scale": target.mosaic_scale, "angle": target.mosaic_angle}
            if target.mosaic_star_map_angle is not None:
                mosaic["star_map_angle"] = target.mosaic_star_map_angle
        item = schedule_tools.target_item(
            {
                "target_name": target.name,
                "target_ra_dec": [target.ra, target.dec],
                "lp_filter": target.lp_filter,
                "mosaic": mosaic,
            }
        )
        item["params"].update(panel_time_sec=block.stack_s, gain=target.gain)
        items.append(item)
    return {"list": items}


def write_night_plan(settings, targets, log, now: float, directory: str) -> tuple[bool, str]:
    """Plan from now and write the schedule if a target is ready.

    Returns (ready, why not). A first target that isn't due yet is not ready, so the
    arm stays closed until then.
    """
    cfg = config_from_settings(settings)
    plan = forecast(targets, log, cfg, now, 1)[0]
    if not plan.blocks:
        return False, "no target on the list can be imaged for the rest of this night"
    first = plan.blocks[0]
    if first.start > now + STEP_S:
        when = datetime.fromtimestamp(first.start).strftime("%H:%M")
        return False, f"next target is {first.target.name} at {when}"
    schedule_tools.write_schedule(
        schedule_tools.PLAN_FILE.removesuffix(".json"), to_schedule(plan), directory
    )
    return True, ""


def describe(plans, targets, log, tz=None) -> str:
    lines = []
    fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M")  # noqa: E731
    for plan in plans:
        if plan.dark_start is None:
            lines.append(f"{plan.key}: no astronomical darkness")
            continue
        lines.append(
            f"{plan.key}  dark {fmt(plan.dark_start)}-{fmt(plan.dark_end)}  moon {plan.moon_percent:.0f}%"
        )
        for block in plan.blocks:
            kind = f"framed {block.target.mosaic_scale:g}x" if block.target.framed else "single"
            lines.append(
                f"  {fmt(block.start)}-{fmt(block.end)}  {block.target.name:<14} {kind:<12} "
                f"~{block.expected_hours:.1f} h stacked"
            )
    done = finish_nights(plans, targets, log)
    lines.append("")
    for target in targets:
        progress = log.progress(target)
        have = target.prior_hours + progress.counted_hours
        lines.append(
            f"{target.name:<14} {have:.1f}/{target.goal_hours:g} h  "
            f"goal met: {done.get(target.id, 'not in this forecast')}"
        )
    return "\n".join(lines)


def config_from_settings(settings) -> PlanConfig:
    from device import horizon
    from device.config import Config

    profile, _error = horizon.configured_profile()
    return PlanConfig(
        lat=settings.lat,
        lon=settings.lon,
        sun_max_deg=settings.sun_max_deg,
        min_alt_deg=settings.min_target_alt_deg,
        horizon=profile,
        horizon_margin_deg=float(Config.horizon_margin or 0.0),
    )


def main(argv=None):
    import argparse
    import os
    import time

    from device.config import Config
    from roof import config as roof_config
    from roof.queue import TargetQueue
    from roof.sessions import SessionLog

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nights", type=int, default=7)
    args = parser.parse_args(argv)
    base = os.path.dirname(Config.path_to_dat)
    targets = TargetQueue(os.path.join(base, "roof_targets.json")).all()
    log = SessionLog(os.path.join(base, "roof_sessions.json"))
    cfg = config_from_settings(roof_config.load())
    print(describe(forecast(targets, log, cfg, time.time(), args.nights), targets, log))


if __name__ == "__main__":
    main()
