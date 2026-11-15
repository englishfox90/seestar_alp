"""Decide what to image on a night, from the target list.

A lite version of what N.I.N.A.'s Target Scheduler does: drop targets that can't
be imaged, score the rest, take the best, repeat. Moon position and altitude are
predictable, so one pass over the night produces a whole schedule; an interruption
replans from then rather than needing a decision before every block.

Run ``python -m roof.planner --nights 7`` to see the plan without touching the scope.
"""

import math
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import ephem

from roof import schedule_tools
from roof.sun import night_key, sun_altitude_deg

STEP_S = 300
# Parking for a gap this long beats leaving the arm open; the automation replans after.
PARK_GAP_S = 1800
# Sun's centre at apparent sunset: refraction plus its radius.
SUN_HORIZON_DEG = -0.833
# Below this chance of the roof being open, a step isn't planned at all.
MIN_OPEN_CHANCE = 0.15
# How far ahead a target's season is looked at, sampling one night in SEASON_STRIDE.
SEASON_DAYS = 150
SEASON_STRIDE = 5


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
    # Season pressure: the wall time a target still needs against what's left of its
    # season. A target about to drop out of reach for the year scores up to this.
    weight_season: float = 1.5
    # Chance a night is open, for judging how much of a season is really usable.
    typical_open_chance: float = 0.5
    # The telescope's (RA, Dec) field in degrees, for how far a mosaic's panels spread.
    # None: the widest model's, so a mosaic is never thought lower than it is.
    fov_deg: tuple | None = None
    # The wide angle camera's field, for a wide mosaic. None: the S30 Pro's, the
    # only one measured.
    wide_fov_deg: tuple | None = None
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
    open_chance: float = 1.0  # mean chance the roof is open over the dark steps
    blocks: list[Block] = field(default_factory=list)
    skipped: dict = field(default_factory=dict)  # target id -> reason


def moon_required_separation(age_days: float, separation: float, width: float) -> float:
    """Lorentzian moon avoidance (BAIT/ACP), as Target Scheduler uses it."""
    return separation / (1.0 + ((0.5 - age_days / 29.5) / (width / 29.5)) ** 2)


def night_noon(key: str, lon: float) -> float:
    """Solar noon that opens the night named ``key`` (see sun.night_key)."""
    day = datetime.fromisoformat(key).replace(tzinfo=timezone.utc)
    return (day + timedelta(hours=12 - lon / 15.0)).timestamp()


def night_times(key: str, lat: float, lon: float, sun_max_deg: float) -> dict:
    """Sunset, sunrise and the dark window of one night, as timestamps or None.

    The dark window samples the sun exactly as _Night does, so it matches the plan.
    """
    noon = night_noon(key, lon)
    altitudes = [
        (noon + k * STEP_S, sun_altitude_deg(lat, lon, datetime.fromtimestamp(noon + k * STEP_S, timezone.utc)))
        for k in range(int(86400 / STEP_S) + 1)
    ]
    dark = []
    for when, altitude in altitudes:
        if altitude < sun_max_deg:
            dark.append(when)
        elif dark:
            break
    sunset = sunrise = None
    for (t0, a0), (t1, a1) in zip(altitudes, altitudes[1:]):
        crossing = t0 + (a0 - SUN_HORIZON_DEG) / (a0 - a1) * (t1 - t0) if a0 != a1 else t0
        if sunset is None and a0 >= SUN_HORIZON_DEG > a1:
            sunset = crossing
        elif sunset is not None and a0 < SUN_HORIZON_DEG <= a1:
            sunrise = crossing
            break
    return {
        "sunset": sunset,
        "sunrise": sunrise,
        "dark_start": dark[0] if dark else None,
        "dark_end": dark[-1] + STEP_S if dark else None,
    }


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

    @staticmethod
    def _body(ra_h, dec):
        body = ephem.FixedBody()
        body._ra = ephem.hours(math.radians(ra_h * 15.0))
        body._dec = ephem.degrees(math.radians(dec))
        body._epoch = ephem.J2000
        return body

    def _above(self, body) -> bool:
        cfg = self.cfg
        altitude = math.degrees(body.alt)
        floor = cfg.min_alt_deg
        if cfg.horizon is not None:
            floor = max(floor, cfg.horizon.altitude_at(math.degrees(body.az)) + cfg.horizon_margin_deg)
        return altitude >= floor

    def masks(self, target) -> tuple[list[bool], list[bool]]:
        """(high enough, high enough and clear of the moon) for each dark step. A
        mosaic is high enough when every one of its panels is."""
        cfg = self.cfg
        body = self._body(target.ra, target.dec)
        panels = [self._body(ra, dec) for ra, dec in panel_centres(target, cfg)]
        default = cfg.moon_lp if target.lp_filter else cfg.moon_no_filter
        separation = target.moon_sep_deg if target.moon_sep_deg is not None else default[0]
        width = target.moon_width_days if target.moon_width_days is not None else default[1]
        high, clear = [], []
        for when, (moon_alt, moon_ra, moon_dec, age) in zip(self.times, self._moon):
            self._observer.date = self._ephem_date(when)
            body.compute(self._observer)
            if panels:
                above = True
                for panel in panels:
                    panel.compute(self._observer)
                    if not self._above(panel):
                        above = False
                        break
            else:
                above = self._above(body)
            distance = math.degrees(ephem.separation((body.ra, body.dec), (moon_ra, moon_dec)))
            moon_ok = moon_alt <= 0 or distance >= moon_required_separation(age, separation, width)
            high.append(above)
            clear.append(above and moon_ok)
        return high, clear


# The widest field of any model (the S30 Pro's), in degrees (RA, Dec).
WIDEST_FOV_DEG = (134.6 / 60, 239.2 / 60)
# The S30 Pro's wide angle camera, measured from a plate-solved wide stack.
WIDE_FOV_DEG = (1866.0 / 60, 3159.0 / 60)


def panel_fov_deg(target, cfg) -> tuple:
    """The field a mosaic's panels are spaced by: its camera's."""
    if getattr(target, "camera", "main") == "wide":
        return cfg.wide_fov_deg or WIDE_FOV_DEG
    return cfg.fov_deg or WIDEST_FOV_DEG


def panel_centres(target, cfg) -> list[tuple[float, float]]:
    """A mosaic's panel centres (RA hours, Dec deg), stepped exactly as ALP's
    mosaic_thread_fn steps them; empty for one panel."""
    if not getattr(target, "mosaic", False):
        return []
    ra_deg, dec_deg = panel_fov_deg(target, cfg)
    keep = 1 - target.panel_overlap / 100.0

    def ra_step(dec):  # hours
        return 1.0 if abs(dec) > 85 else ra_deg / 15.0 * keep / math.cos(math.radians(dec))

    dec_step = dec_deg * keep
    centre_ra, centre_dec = target.ra, target.dec
    if target.panels_ra % 2 == 0:
        centre_ra += ra_step(target.dec) / 2
    if target.panels_dec % 2 == 0:
        centre_dec += dec_step / 2
    out = []
    dec = centre_dec - (target.panels_dec // 2) * dec_step
    for _ in range(target.panels_dec):
        step = ra_step(dec)
        ra = centre_ra - (target.panels_ra // 2) * step
        for _ in range(target.panels_ra):
            out.append((ra % 24.0, max(-90.0, min(90.0, dec))))
            ra += step
        dec += dec_step
    return out


def switch_overhead(target, cfg) -> float:
    """Seconds lost starting a target: a slew, plate solve and focus for each panel."""
    return cfg.switch_overhead_s * getattr(target, "panels", 1)


def goal_total(target) -> float:
    """Integration wanted across the whole target; a mosaic's goal is per panel."""
    return target.goal_hours * getattr(target, "panels", 1)


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


_season_cache: dict = {}


def _season_key(target, cfg, key):
    horizon = tuple(cfg.horizon.points) if cfg.horizon is not None else None
    return (
        key, cfg.lat, cfg.lon, cfg.sun_max_deg, cfg.min_alt_deg, horizon, cfg.horizon_margin_deg,
        cfg.moon_lp, cfg.moon_no_filter, cfg.switch_overhead_s,
        target.ra, target.dec, target.lp_filter, target.moon_sep_deg, target.moon_width_days,
        target.framed, target.min_session_hours, cfg.fov_deg, cfg.wide_fov_deg, target.camera,
        getattr(target, "panels_ra", 1), getattr(target, "panels_dec", 1), getattr(target, "panel_overlap", 0),
    )  # fmt: skip


def _usable_s(night, target, cfg) -> float:
    """Seconds of a night clear for the target in runs of at least its minimum session."""
    floor_s = max(target.min_session_hours * 3600, STEP_S)
    usable_s, run = 0.0, 0
    for ok in night.masks(target)[1] + [False]:
        if ok:
            run += 1
            continue
        run_s = run * STEP_S - switch_overhead(target, cfg)
        if run_s >= floor_s:
            usable_s += run_s
        run = 0
    return usable_s


def last_usable_night(target, cfg: PlanConfig, sampled_last: str) -> str:
    """The exact last usable night, from the last sampled one season_left gives."""
    day, last = datetime.fromisoformat(sampled_last), sampled_last
    for offset in range(1, SEASON_STRIDE):
        key = (day + timedelta(days=offset)).date().isoformat()
        if _usable_s(_Night(key, cfg), target, cfg) <= 0:
            break
        last = key
    return last


def season_profile(targets, cfg: PlanConfig, key: str) -> dict:
    """Target id -> [(night key, usable hours)], one sample every SEASON_STRIDE nights
    from ``key`` for SEASON_DAYS, each standing for SEASON_STRIDE nights.

    Usable hours are dark, above the floor and clear of the moon, in runs long enough
    for the target's minimum session. Worked out once a day per target.
    """
    out, missing = {}, []
    for target in targets:
        cached = _season_cache.get(_season_key(target, cfg, key))
        if cached is None:
            missing.append(target)
        else:
            out[target.id] = cached
    if missing:
        if len(_season_cache) > 500:
            _season_cache.clear()
        day = datetime.fromisoformat(key)
        profiles = {t.id: [] for t in missing}
        for offset in range(0, SEASON_DAYS, SEASON_STRIDE):
            night_key_ = (day + timedelta(days=offset)).date().isoformat()
            night = _Night(night_key_, cfg)
            for target in missing:
                profiles[target.id].append((night_key_, _usable_s(night, target, cfg) * SEASON_STRIDE / 3600))
        for target in missing:
            _season_cache[_season_key(target, cfg, key)] = profiles[target.id]
            out[target.id] = profiles[target.id]
    return out


# A month with no usable time ends a season; what comes after is next year's.
SEASON_GAP_DAYS = 30
# A season ending within this many days gets full pressure; one ending later, less in
# proportion (twice as far off, half as much).
SEASON_URGENT_DAYS = 30


def season_left(profile, key: str) -> tuple[float, str] | None:
    """(usable hours, last usable night) left in the target's current or next season
    from the night ``key`` on, up to the first gap of SEASON_GAP_DAYS with none. None
    when the season runs past what's sampled: it isn't ending, so no pressure."""
    total, gap, last = 0.0, 0, None
    for night, hours in profile:
        if night < key:
            continue
        if hours > 0:
            total, gap, last = total + hours, 0, night
        elif last is not None:
            gap += SEASON_STRIDE
            if gap >= SEASON_GAP_DAYS:
                return total, last
    return (0.0, key) if last is None else None


def season_pressure(needed_wall_h: float, season, key: str, cfg: PlanConfig) -> float:
    """How hard a target's season is pushing, 0 to 1: what it still needs against what's
    left (at the typical open chance), scaled down when the season ends far off."""
    if needed_wall_h <= 0 or season is None:
        return 0.0
    hours, last = season
    usable = hours * max(cfg.typical_open_chance, 0.05)
    share = 1.0 if usable <= 0 else min(1.0, needed_wall_h / usable)
    days = (datetime.fromisoformat(last) - datetime.fromisoformat(key)).days + SEASON_STRIDE
    return share * min(1.0, SEASON_URGENT_DAYS / max(days, 1))


def plan_night(
    targets, remaining: dict, key: str, cfg: PlanConfig, start=None, rates=None, open_chance=None, season=None
) -> NightPlan:
    """Plan one night. ``remaining`` maps target id to integration hours still
    wanted, and is updated with what the plan expects to deliver.

    ``open_chance(when)`` is the chance the roof is open at a time, 0 to 1; None means
    a clear night. Steps under MIN_OPEN_CHANCE aren't planned, and each block is
    expected to deliver its stacked time scaled by the chance over its steps.

    ``season`` maps target id to its season_profile; without it there's no season
    pressure in the score.
    """
    night = _Night(key, cfg)
    chance = [1.0 if open_chance is None else max(0.0, min(1.0, open_chance(t))) for t in night.times]
    roof_ok = [c >= MIN_OPEN_CHANCE for c in chance]
    plan = NightPlan(
        key=key,
        dark_start=night.times[0] if night.times else None,
        dark_end=night.times[-1] + STEP_S if night.times else None,
        moon_percent=night.moon_percent,
        open_chance=sum(chance) / len(chance) if chance else 0.0,
    )
    masks, active = {}, []
    for target in targets:
        if not target.enabled:
            plan.skipped[target.id] = "disabled"
        elif remaining.get(target.id, 0) <= 0:
            plan.skipped[target.id] = "goal reached"
        else:
            high, clear = night.masks(target)
            usable = [c and ok for c, ok in zip(clear, roof_ok)]
            masks[target.id] = (high, clear, usable)
            active.append(target)

    pressure = {}
    for target in active:
        if season and target.id in season:
            needed_wall_h = remaining[target.id] / _rate(target, cfg, rates)
            pressure[target.id] = season_pressure(needed_wall_h, season_left(season[target.id], key), key, cfg)

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
            usable = masks[target.id][2]
            run_s = _run_steps(usable, index) * STEP_S
            continuing = previous is not None and previous.target is target
            overhead = 0.0 if continuing else switch_overhead(target, cfg)
            available = run_s - overhead
            minimum_s = target.min_session_hours * 3600
            # Book enough wall time to deliver what's left at the chance it opens.
            needed_s = remaining[target.id] * 3600 / (_rate(target, cfg, rates) * max(chance[index], MIN_OPEN_CHANCE))
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
            score += cfg.weight_season * pressure.get(target.id, 0.0)
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
        span = chance[index : index + steps] or [chance[index]]
        delivered = stack_s * _rate(best, cfg, rates) * (sum(span) / len(span)) / 3600
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
    goal = max(goal_total(target), 0.001)
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
    high, clear, usable = (m[first_index:] for m in masks)
    if not any(high):
        return f"not above {cfg.min_alt_deg:g} deg in the dark for the rest of the night"
    if not any(clear):
        return "too close to the moon whenever it's up"
    if not any(usable):
        return "roof likely closed while it's up"
    longest = _longest_run(usable) * STEP_S - switch_overhead(target, cfg)
    if longest < target.min_session_hours * 3600:
        kind = "framed run" if target.framed else "session"
        return (
            f"longest usable window {_hours(max(longest, 0))} is under its "
            f"{target.min_session_hours:g} h {kind}"
        )
    return "other targets scored higher"


def remaining_hours(targets, log) -> dict:
    """Integration hours still wanted per target id. A mosaic's is what its panels
    are short of their goal, added up."""
    out = {}
    for t in targets:
        if t.mosaic:
            out[t.id] = sum(
                max(0.0, t.goal_hours - t.prior_hours - have) for have in log.panel_hours(t).values()
            )
        else:
            out[t.id] = max(0.0, t.goal_hours - t.prior_hours - log.progress(t).counted_hours)
    return out


# A mosaic's panels are kept within this of each other: a night shoots only the
# panels this close to the one furthest behind.
PANEL_CATCH_UP_H = 0.5


def panels_to_shoot(target, log) -> list[str]:
    """A mosaic's panels still short of their goal and furthest behind, in order."""
    have = {p: target.prior_hours + h for p, h in log.panel_hours(target).items()}
    short = {p: h for p, h in have.items() if h < target.goal_hours - 1e-6}
    if not short:
        return []
    least = min(short.values())
    return [p for p in target.panel_names if p in short and short[p] <= least + PANEL_CATCH_UP_H]


def measured_rates(targets, log) -> dict:
    rates = {}
    for target in targets:
        rate = log.progress(target).measured_rate
        if rate:
            rates[target.id] = max(0.05, min(1.0, rate))
    return rates


def forecast(targets, log, cfg: PlanConfig, now: float, nights: int = 14, open_chance=None) -> list[NightPlan]:
    """Plans for this night and the next, all targets sharing each night and carrying
    progress forward. Every night is clear unless ``open_chance`` (see plan_night)
    says how likely the roof is to be open."""
    remaining = remaining_hours(targets, log)
    rates = measured_rates(targets, log)
    first_key = night_key(now, cfg.lon)
    day = datetime.fromisoformat(first_key)
    season = season_profile([t for t in targets if t.enabled and remaining.get(t.id, 0) > 0], cfg, first_key)
    plans = []
    for offset in range(nights):
        key = (day + timedelta(days=offset)).date().isoformat()
        plans.append(
            plan_night(
                targets,
                remaining,
                key,
                cfg,
                start=now if offset == 0 else None,
                rates=rates,
                open_chance=open_chance,
                season=season,
            )
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


def to_schedule(plan: NightPlan, panels: dict | None = None) -> dict:
    """A schedule the automation can run, up to the first gap worth parking for.

    ``panels`` maps a mosaic's target id to the panels to shoot (panels_to_shoot);
    without it every panel is. A mosaic's block is shared equally between them.
    """
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
        grid = None
        panel_time = block.stack_s
        if target.mosaic:
            grid = {
                "ra": target.panels_ra,
                "dec": target.panels_dec,
                "overlap": target.panel_overlap,
                "camera": "wide" if target.camera == "wide" else "main",
            }
            chosen = (panels or {}).get(target.id) or target.panel_names
            if len(chosen) < target.panels:
                grid["selected"] = chosen
            # The block's overhead was one slew, solve and focus per panel.
            per_panel = block.overhead_s / target.panels
            panel_time = max(60, int((block.end - block.start - per_panel * len(chosen)) / len(chosen)))
        item = schedule_tools.target_item(
            {
                "target_name": target.name,
                "target_ra_dec": [target.ra, target.dec],
                "lp_filter": target.lp_filter,
                "mosaic": mosaic,
                "panels": grid,
                "camera": target.camera,
            }
        )
        item["params"].update(panel_time_sec=panel_time, gain=target.gain)
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
    panels = {b.target.id: panels_to_shoot(b.target, log) for b in plan.blocks if b.target.mosaic}
    schedule_tools.write_schedule(
        schedule_tools.PLAN_FILE.removesuffix(".json"), to_schedule(plan, panels), directory
    )
    _write_plan_times(plan, targets, directory)
    return True, ""


def plan_summary(plan: NightPlan, targets) -> dict:
    """A plan as plain data: the blocks in order, and why other targets got no time."""
    names = {t.id: t.name for t in targets}
    return {
        "night": plan.key,
        "blocks": [
            {
                "id": b.target.id,
                "name": b.target.name,
                "kind": "Framed" if b.target.framed else ("Mosaic" if b.target.mosaic else "Single"),
                "ra": b.target.ra,
                "dec": b.target.dec,
                "start": b.start,
                "end": b.end,
            }
            for b in plan.blocks
        ],
        "skipped": [
            {"id": target_id, "name": names[target_id], "why": why}
            for target_id, why in plan.skipped.items()
            if target_id in names
        ],
    }


def _write_plan_times(plan, targets, directory):
    # Only the dashboard reads this, so failing to write it mustn't stop a start.
    path = os.path.join(directory, schedule_tools.PLAN_TIMES_FILE)
    try:
        with open(f"{path}.tmp", "w") as f:
            json.dump(plan_summary(plan, targets), f)
        os.replace(f"{path}.tmp", path)
    except OSError:
        pass


def read_plan_times(directory: str, night: str) -> dict | None:
    """The summary of the plan last started on this night, if there is one."""
    try:
        with open(os.path.join(directory, schedule_tools.PLAN_TIMES_FILE)) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("night") == night else None


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
        typical_open_chance=settings.typical_open_chance,
        fov_deg=_scope_fov(settings),
        wide_fov_deg=_scope_fov(settings, wide=True),
    )


def _scope_fov(settings, wide=False):
    """The connected scope's field (its wide angle camera's with ``wide``), or None
    when it hasn't said what model it is, or that field isn't measured."""
    try:
        from device import telescope
        from device.seestar_util import Util

        device = telescope.seestar_dev.get(settings.device_num)
        model = getattr(device, "product_model", "") if device is not None else ""
        if not model:
            return None
        return Util.model_wide_fov_deg(model) if wide else Util.model_fov_deg(model)
    except Exception:
        return None


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
