"""The scope's MyWorks folder: missing or empty means its share isn't connected.

On 2026-11-18 the folder was gone all night. Every Stacked card went without its
picture, two framed runs were judged on length instead of their image, and nothing
said why.
"""

from types import SimpleNamespace

from roof import alerts as roof_alerts
from roof.alerts import FolderWatch, folder_state
from roof.scope import ScopeState
from roof.sessions import COVERAGE_GIVE_UP_S, COVERAGE_RECHECK_S, SessionLog
from tests.test_roof_alerts_rich import NIGHT, Clock, _alerts, _observe
from tests.test_roof_queue import _ended_run, framed

# ---- folder_state ----------------------------------------------------------------------


def test_no_folder_set_is_off():
    assert folder_state("") == "off"


def test_a_folder_that_is_gone_is_missing(tmp_path):
    assert folder_state(str(tmp_path / "unmounted")) == "missing"


def test_an_empty_mount_point_is_empty_even_with_finder_litter(tmp_path):
    (tmp_path / ".DS_Store").write_bytes(b"")
    assert folder_state(str(tmp_path)) == "empty"


def test_a_folder_with_targets_is_ok(tmp_path):
    (tmp_path / "M 42").mkdir()
    assert folder_state(str(tmp_path)) == "ok"


def test_a_home_relative_folder_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "MyWorks" / "M 42").mkdir(parents=True)
    assert folder_state("~/MyWorks") == "ok"


# ---- FolderWatch -----------------------------------------------------------------------


class Runs:
    """Holds the watch's checks until a test runs them, as its thread would."""

    def __init__(self):
        self.waiting = []

    def __call__(self, fn):
        self.waiting.append(fn)

    def finish(self):
        while self.waiting:
            self.waiting.pop(0)()


def _watch(folder="/share", answer="ok", every_s=300):
    clock, runs, checks = Clock(), Runs(), []
    where = {"folder": folder}

    def check(directory):
        checks.append(directory)
        return answer() if callable(answer) else answer

    watch = FolderWatch(lambda: where["folder"], every_s=every_s, clock=clock, check=check, run=runs)
    return watch, clock, runs, checks, where


def test_the_watch_answers_at_once_and_checks_on_its_own_thread():
    watch, _, runs, checks, _ = _watch(answer="missing")
    assert watch.state() == "unknown"
    assert checks == []  # nothing looked at the folder on the caller's thread
    runs.finish()
    assert watch.state() == "missing"


def test_the_watch_checks_at_most_every_interval():
    answers = ["missing", "ok"]
    watch, clock, runs, checks, _ = _watch(answer=lambda: answers.pop(0))
    watch.state()
    runs.finish()
    for _ in range(5):
        watch.state()
    runs.finish()
    assert len(checks) == 1
    clock.t += 300
    watch.state()
    runs.finish()
    assert watch.state() == "ok"


def test_one_slow_check_at_a_time():
    watch, clock, runs, _, _ = _watch()
    watch.state()
    clock.t += 1000
    watch.state()
    assert len(runs.waiting) == 1  # the first hasn't answered, so no second is started


def test_a_changed_folder_is_checked_again_and_none_is_off():
    watch, _, runs, checks, where = _watch()
    watch.state()
    runs.finish()
    where["folder"] = "/other"
    assert watch.state() == "unknown"
    runs.finish()
    assert checks == ["/share", "/other"]
    where["folder"] = ""
    assert watch.state() == "off"
    assert runs.waiting == []


def test_a_check_that_raises_counts_as_missing():
    def boom(directory):
        raise OSError("share gone")

    runs = Runs()
    watch = FolderWatch(lambda: "/share", clock=Clock(), check=boom, run=runs)
    watch.state()
    runs.finish()
    assert watch.state() == "missing"


# ---- judging framed runs -----------------------------------------------------------------


def _log(tmp_path):
    log = SessionLog(str(tmp_path / "s.json"))
    _ended_run(log, lapse=2 * 3600)
    return log, log.rows()[0]["last_seen"]


def test_an_unconnected_folder_never_gives_up_on_a_run(tmp_path):
    log, ended = _log(tmp_path)
    looked = []
    find = lambda d, n, e: looked.append(n)  # noqa: E731
    for state in ("missing", "empty", "unknown"):
        assert log.review("share", ended + COVERAGE_GIVE_UP_S * 2, folder=state, find=find) == 0
    assert "covered" not in log.rows()[0]
    assert looked == []


def test_once_connected_a_waiting_run_is_judged_by_its_image(tmp_path):
    log, ended = _log(tmp_path)
    log.review("share", ended + COVERAGE_GIVE_UP_S * 2, folder="missing", find=lambda d, n, e: None)
    judged = log.review(
        "share", ended + COVERAGE_GIVE_UP_S * 2, folder="ok", find=lambda d, n, e: "/x/s.jpg", measure=lambda p: 0.83
    )
    assert judged == 1
    row = log.rows()[0]
    assert row["covered"] is False and row["stack"] == "s.jpg"


def test_a_run_judged_on_length_is_judged_again_when_its_image_turns_up(tmp_path):
    log, ended = _log(tmp_path)
    log.review("share", ended + COVERAGE_GIVE_UP_S, folder="ok", find=lambda d, n, e: None)
    assert log.rows()[0]["covered"] is None
    assert log.progress(framed()).counted_hours > 0  # two hours, so its length counted
    judged = log.review(
        "share", ended + COVERAGE_GIVE_UP_S + 600, folder="ok", find=lambda d, n, e: "/x/s.jpg", measure=lambda p: 0.83
    )
    assert judged == 1
    assert log.rows()[0]["covered"] is False
    assert log.progress(framed()).counted_hours == 0  # its image shows holes


def test_a_run_judged_on_length_with_still_no_image_is_left_as_it_was(tmp_path):
    log, ended = _log(tmp_path)
    log.review("share", ended + COVERAGE_GIVE_UP_S, folder="ok", find=lambda d, n, e: None)
    assert log.review("share", ended + COVERAGE_GIVE_UP_S + 600, folder="ok", find=lambda d, n, e: None) == 0
    assert log.rows()[0]["covered"] is None


def test_an_old_run_judged_on_length_is_not_looked_at_again(tmp_path):
    log, ended = _log(tmp_path)
    log.review("share", ended + COVERAGE_GIVE_UP_S, folder="ok", find=lambda d, n, e: None)
    looked = []
    log.review("share", ended + COVERAGE_RECHECK_S, folder="ok", find=lambda d, n, e: looked.append(n))
    assert looked == []


def test_a_run_judged_by_its_image_is_not_judged_again(tmp_path):
    log, ended = _log(tmp_path)
    log.review("share", ended + 600, folder="ok", find=lambda d, n, e: "/x/s.jpg", measure=lambda p: 0.07)
    looked = []
    log.review("share", ended + 1200, folder="ok", find=lambda d, n, e: looked.append(n))
    assert looked == []


def test_with_no_folder_set_a_run_still_falls_back_to_length(tmp_path):
    log, ended = _log(tmp_path)
    assert log.review("", ended + COVERAGE_GIVE_UP_S, folder="off") == 1
    assert log.rows()[0]["covered"] is None


def test_without_a_watch_the_review_looks_at_the_folder_itself(tmp_path):
    log, ended = _log(tmp_path)
    assert log.review(str(tmp_path / "unmounted"), ended + COVERAGE_GIVE_UP_S) == 0
    assert "covered" not in log.rows()[0]


# ---- the warning ---------------------------------------------------------------------------


class Fixed:
    def __init__(self, state):
        self.value = state

    def state(self):
        return self.value


def _imaging(alerts, night=NIGHT):
    alerts.session_started("Target list", ["M 42"], 1, False, night)
    _observe(alerts, ScopeState(connected=True, sched_state="working"), dark=True, night=night)


WARNING = "Stacks folder not connected"


def test_a_missing_folder_warns_once_a_night_while_imaging(tmp_path):
    folder = Fixed("missing")
    alerts, sender = _alerts(tmp_path, image_dir=lambda: "/Volumes/EMMC Images/MyWorks", folder=folder)
    _observe(alerts, ScopeState(connected=True), dark=False)
    assert WARNING not in sender.titles()  # not imaging yet
    _imaging(alerts)
    _imaging(alerts)  # a second session the same night
    assert sender.titles().count(WARNING) == 1
    text = sender.find(WARNING)[1]
    assert "/Volumes/EMMC Images/MyWorks isn't there" in text
    _imaging(alerts, night="2026-11-19")
    assert sender.titles().count(WARNING) == 2


def test_an_empty_folder_warns_too(tmp_path):
    alerts, sender = _alerts(tmp_path, image_dir=lambda: "/share", folder=Fixed("empty"))
    _imaging(alerts)
    assert "/share is empty" in sender.find(WARNING)[1]


def test_a_folder_that_comes_back_and_goes_again_warns_again(tmp_path):
    folder = Fixed("missing")
    alerts, sender = _alerts(tmp_path, image_dir=lambda: "/share", folder=folder)
    _imaging(alerts)
    folder.value = "ok"
    _imaging(alerts)
    folder.value = "missing"
    _imaging(alerts)
    assert sender.titles().count(WARNING) == 2


def test_no_warning_when_connected_unset_still_checking_or_switched_off(tmp_path):
    for state, muted in (("ok", ()), ("off", ()), ("unknown", ()), ("missing", ("stacks_folder",))):
        alerts, sender = _alerts(tmp_path, image_dir=lambda: "/share", folder=Fixed(state), muted=muted)
        _imaging(alerts)
        assert WARNING not in sender.titles(), state


def test_the_warning_has_its_own_switch_on_the_page():
    assert "stacks_folder" in roof_alerts.KINDS


def test_no_stack_picture_is_waited_for_while_the_folder_is_not_connected(tmp_path):
    alerts, _ = _alerts(tmp_path, image_dir=lambda: "/share", folder=Fixed("missing"))
    photos = []
    alerts._sender.send_photo = lambda *a: photos.append(a)
    alerts._send_stack_image(SimpleNamespace(name="M 42", frames=10, started=0, summary=lambda: "10 frames"))
    assert photos == []
    alerts._folder = Fixed("ok")
    alerts._send_stack_image(SimpleNamespace(name="M 42", frames=10, started=0, summary=lambda: "10 frames"))
    assert len(photos) == 1
