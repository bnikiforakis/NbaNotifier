"""Tracker decisions with a fake clock, scripted scoreboards and a recording PiButler."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from nba_notifier.api import ApiError, parse_scoreboard, parse_summary, parse_teams
from nba_notifier.butler import ButlerError
from nba_notifier.config import Settings
from nba_notifier.models import Game, Status
from nba_notifier.reports import DASH
from nba_notifier.store import EventStore
from nba_notifier.tracker import Notifier, build_manifest, button_rows, game_night, us_day

FIXTURES = Path(__file__).parent / "fixtures"
NIGHT = date(2026, 10, 21)  # US game day
TIPOFF = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)  # 19:30 Eastern, 02:30 Athens (quiet hours)


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


SCHEDULED = parse_scoreboard(load("scoreboard_20261021_scheduled.json"), NIGHT)
GAME = replace(SCHEDULED[0], tipoff=TIPOFF)
QUARTERS = [(25, 20), (30, 28), (22, 25), (20, 22)]  # home, away: 97-95
BOX = parse_summary(load("summary_401898395_halftime.json"))


def box(home: int = 97, away: int = 95, live: bool = True, quarters=None):
    return replace(
        BOX,
        home=replace(BOX.home, points=home),
        away=replace(BOX.away, points=away),
        live=live,
        quarter_scores=list(QUARTERS if quarters is None else quarters),
    )


FINAL_BOX = box(live=False)

PRE = Status()


def playing(period: int) -> Status:
    return Status("in", "STATUS_IN_PROGRESS", period, "5:00")


def end(period: int) -> Status:
    return Status("in", "STATUS_END_PERIOD", period, "0.0")


HALF = Status("in", "STATUS_HALFTIME", 2, "0.0")
FINAL = Status("post", "STATUS_FINAL", 4, "0.0", completed=True)


def live(status: Status, quarters=QUARTERS, game: Game = GAME) -> Game:
    """``game`` as the scoreboard shows it: periods up to the current one, score = their sum."""
    played = list(quarters[: status.period])
    if status.state == "pre":
        return replace(game, status=status, quarter_scores=[], home_score=None, away_score=None)
    return replace(
        game,
        status=status,
        quarter_scores=played,
        home_score=sum(h for h, _ in played),
        away_score=sum(a for _, a in played),
    )


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeEspn:
    """Scripted scoreboards (one entry per request; the last one repeats) and box scores."""

    def __init__(self, boards: list[list[Game]] | None = None, boxes=None, days=None):
        self.boards = list(boards or [[GAME]])
        self.boxes = list(boxes or [FINAL_BOX])
        self.days = days  # day -> games, for schedule refreshes
        self.board_calls = 0
        self.summary_calls = 0
        self.fail_boards = False
        self.fail_teams = 0

    async def scoreboard(self, day):
        self.board_calls += 1
        if self.fail_boards:
            raise ApiError("down")
        if self.days is not None:
            return self.days.get(day, [])
        return self.boards.pop(0) if len(self.boards) > 1 else self.boards[0]

    async def summary(self, event_id):
        self.summary_calls += 1
        return self.boxes.pop(0) if len(self.boxes) > 1 else self.boxes[0]

    async def teams(self):
        if self.fail_teams:
            self.fail_teams -= 1
            raise ApiError("down")
        return parse_teams(load("teams.json"))


class FakeButler:
    def __init__(self):
        self.sent: list[dict] = []
        self.manifest = None
        self.fail_next = 0

    async def notify(self, key, text, tags=None, expires_at=None, **extra):
        if self.fail_next:
            self.fail_next -= 1
            raise ButlerError("down")
        self.sent.append({"key": key, "text": text, "tags": tags or {}, "expires_at": expires_at, **extra})
        return {"recipients": 1, "duplicate": False}

    async def register(self, manifest):
        self.manifest = manifest

    @property
    def keys(self) -> list[str]:
        return [n["key"] for n in self.sent]

    def get(self, suffix: str) -> dict:
        return next(n for n in self.sent if n["key"].endswith(suffix))


@pytest.fixture
async def events():
    store = await EventStore.open(":memory:")
    yield store
    await store.close()


def make(events, statuses=None, start=TIPOFF - timedelta(hours=1), boxes=None, espn=None, **settings):
    clock = FakeClock(start)
    butler = FakeButler()
    espn = espn or FakeEspn([[live(s)] for s in statuses or [PRE]], boxes)
    config = Settings(pibutler_url="http://x", pibutler_api_key="k", poll_seconds=30, **settings)
    notifier = Notifier(config, espn, butler, events, clock=clock, sleep=clock.sleep)
    return notifier, butler, clock


def suffixes(butler: FakeButler) -> list[str]:
    return [k.split(":", 1)[1].split(":")[0] for k in butler.keys]


# --- one game, live ------------------------------------------------------------------------------


async def test_full_regulation_game(events):
    statuses = [PRE, PRE, playing(1), end(1), playing(2), HALF, HALF, playing(3), end(3), playing(4), end(4)]
    notifier, butler, clock = make(events, [*statuses, FINAL])
    await notifier.track(GAME)
    # Q4 ended with a winner, so the final covers it: no p4 card.
    assert suffixes(butler) == ["reminder", "start", "p1", "p2", "p3", "final"]
    assert clock() < TIPOFF + timedelta(hours=1)  # stopped polling once final


async def test_quarter_card_scores_and_followers_only(events):
    notifier, butler, _ = make(events, [end(1), HALF, FINAL], start=TIPOFF)
    await notifier.track(GAME)
    p1, p2 = butler.get(":p1"), butler.get(":p2")
    away, home = GAME.away.short_name, GAME.home.short_name
    assert f"End of Q1</b> · {away} 20{DASH}25 {home}" in p1["text"]  # visitors first
    assert f"End of Q2</b> · {away} 48{DASH}55 {home}" in p2["text"]
    for n in butler.sent:
        assert n["audience"] == {"topic": GAME.id, "following": True}, n["key"]
        assert n["topics"][0]["auto_follow"] == {"teams": [GAME.home.code, GAME.away.code]}
    assert "<pre>" not in p1["text"] and "<pre>" in p1["buttons"][0][0]["reveal"]
    assert p1["buttons"][1][0]["follow"] == GAME.id
    final = butler.get(":final")
    assert final["buttons"][0][1]["url"] == f"https://www.espn.com/nba/game/_/gameId/{GAME.id}"
    assert p1["expires_at"] and final["expires_at"] is None


async def test_missed_break_still_reports_the_period_from_its_scores(events):
    # Polled during Q1, then already in Q2: Q1 is over, its score comes from the period scores.
    notifier, butler, _ = make(events, [playing(1), playing(2), FINAL], start=TIPOFF)
    await notifier.track(GAME)
    assert suffixes(butler) == ["start", "p1", "final"]
    assert f"20{DASH}25" in butler.get(":p1")["text"]


async def test_overtime_reports_a_tied_q4_and_each_tied_ot(events):
    quarters = [*QUARTERS[:3], (20, 24), (10, 10), (12, 8)]  # 97-97 after Q4, 107-107 after OT1
    states = [end(4), playing(5), end(5), playing(6), end(6), replace(FINAL, period=6)]
    boards = [[live(s, quarters)] for s in states]
    espn = FakeEspn(boards, [box(119, 115, live=False, quarters=quarters)])
    notifier, butler, _ = make(events, start=TIPOFF, espn=espn)
    await notifier.track(GAME)
    assert suffixes(butler) == ["p4", "p5", "final"]  # OT2 ended with a winner; no late tip-off
    assert "End of OT1" in butler.get(":p5")["text"]
    assert "(2OT)" in butler.get(":final")["text"]


async def test_followed_messages_are_silent_in_the_quiet_hours(events):
    notifier, butler, _ = make(events, [PRE, playing(1), end(1), FINAL])  # tip-off 02:30 Athens
    await notifier.track(GAME)
    assert butler.sent and all(n["silent"] for n in butler.sent)


async def test_evening_games_ring(events):
    evening = replace(GAME, tipoff=datetime(2026, 10, 21, 17, 0, tzinfo=UTC))  # 20:00 Athens
    espn = FakeEspn([[live(s, game=evening)] for s in (PRE, playing(1), end(1), FINAL)])
    notifier, butler, _ = make(events, start=evening.tipoff - timedelta(hours=1), espn=espn)
    await notifier.track(evening)
    assert suffixes(butler) == ["reminder", "start", "p1", "final"]
    assert not any(n["silent"] for n in butler.sent)


async def test_quiet_hours_end_mid_game(events):
    # 06:00 Athens tip-off: the reminder is silent, the final (after 09:00) rings.
    morning = replace(GAME, tipoff=datetime(2026, 10, 22, 3, 0, tzinfo=UTC))
    statuses = [PRE, *[playing(1)] * 400, FINAL]  # ~3.3 h of play at 30 s per poll
    espn = FakeEspn([[live(s, game=morning)] for s in statuses])
    notifier, butler, _ = make(events, start=morning.tipoff - timedelta(hours=1), espn=espn)
    await notifier.track(morning)
    assert butler.get(":reminder:2026-10-22T03:00:00Z")["silent"]
    assert not butler.get(":final")["silent"]


async def test_cancelled_game_stops_without_messages(events):
    postponed = Status("post", "STATUS_POSTPONED", 0, "0.0")
    notifier, butler, clock = make(events, [postponed], start=TIPOFF)
    await notifier.track(GAME)
    assert butler.keys == []
    assert clock() == TIPOFF  # stopped at the first poll


async def test_restart_mid_game_skips_already_sent_and_old_quarters(events):
    for key in (f"reminder:{TIPOFF:%Y-%m-%dT%H:%M:%SZ}", "start", "p1"):
        await events.add(f"{GAME.id}:{key}")
    notifier, butler, _ = make(events, [end(3), FINAL], start=TIPOFF + timedelta(minutes=90))
    await notifier.track(GAME)
    assert suffixes(butler) == ["p3", "final"]


async def test_restart_after_the_final_sends_nothing_more(events):
    await events.add(f"{GAME.id}:final")
    notifier, butler, _ = make(events, [FINAL], start=TIPOFF + timedelta(hours=3))
    await notifier.track(GAME)
    assert butler.keys == []


async def test_final_waits_for_a_lagging_box_score(events):
    lagging = box(90, 95, live=True)
    notifier, butler, _ = make(events, [FINAL], start=TIPOFF + timedelta(hours=2), boxes=[lagging, FINAL_BOX])
    await notifier.track(GAME)
    assert suffixes(butler) == ["final"]
    assert "97" in butler.get(":final")["text"]


async def test_final_sent_anyway_when_the_box_score_never_catches_up(events):
    stuck = box(90, 95, live=True)
    notifier, butler, clock = make(events, [FINAL], start=TIPOFF + timedelta(hours=2), boxes=[stuck])
    await notifier.track(GAME)
    assert suffixes(butler) == ["final"]
    assert clock() >= TIPOFF + timedelta(hours=2, minutes=3)


async def test_quarter_card_waits_for_a_lagging_box_score(events):
    behind = box(20, 15)  # the end of Q1 is 25-20
    statuses = [end(1), end(1), end(1), FINAL]
    notifier, butler, _ = make(events, statuses, start=TIPOFF, boxes=[behind, box(), FINAL_BOX])
    await notifier.track(GAME)
    assert suffixes(butler) == ["p1", "final"]


async def test_poll_errors_back_off_and_retry(events):
    notifier, butler, _ = make(events, [playing(1), playing(1), end(1), FINAL], start=TIPOFF)
    butler.fail_next = 1  # PiButler down for the tip-off message
    await notifier.track(GAME)
    assert suffixes(butler) == ["start", "p1", "final"]


async def test_gives_up_after_max_game_length(events):
    notifier, butler, clock = make(events, [playing(2)], start=TIPOFF)
    await notifier.track(GAME)
    assert clock() >= TIPOFF + timedelta(hours=4)
    assert suffixes(butler) == ["p1"]  # first seen in Q2: no tip-off message


async def test_game_tasks_share_one_scoreboard_request(events):
    other = replace(SCHEDULED[1], tipoff=TIPOFF)
    espn = FakeEspn([[live(end(1)), live(end(1), game=other)]], [box()])
    notifier, _, _ = make(events, start=TIPOFF, espn=espn)
    await asyncio.gather(notifier.poll(GAME), notifier.poll(other))
    assert espn.board_calls == 1


# --- schedule, registration --------------------------------------------------------------------


def night_games(n: int, night: date = NIGHT, first: datetime = TIPOFF) -> list[Game]:
    games = [replace(SCHEDULED[i % len(SCHEDULED)], id=str(1000 + i)) for i in range(n)]
    return [replace(g, day=night, tipoff=first + timedelta(minutes=30 * i)) for i, g in enumerate(games)]


def days(games: list[Game]) -> dict[date, list[Game]]:
    out: dict[date, list[Game]] = {}
    for g in games:
        out.setdefault(g.day, []).append(g)
    return out


async def schedule_at(events, games, local: datetime):
    notifier, butler, _ = make(events, start=local.astimezone(UTC), espn=FakeEspn(days=days(games)))
    await notifier.refresh_schedule()
    await notifier.maybe_send_schedule()
    return butler


ATHENS_1700 = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 17:00 Athens = 10:00 Eastern


async def test_tonights_schedule_at_five_pm_athens(events):
    butler = await schedule_at(events, night_games(3), ATHENS_1700)
    msg = butler.get("schedule:2026-10-21")
    assert msg["silent"] and msg["tags"] == {"daily": ["schedule"]}
    assert "Tonight's NBA games</b> · Wed 21 Oct (US)" in msg["text"]
    assert [row[0]["follow"] for row in msg["buttons"]] == ["1000", "1001", "1002"]
    assert "{{hm:2026-10-21T23:30:00Z}}" in msg["buttons"][0][0]["label"]
    assert len(msg["topics"]) == 3


async def test_no_schedule_before_five_pm(events):
    butler = await schedule_at(events, night_games(3), ATHENS_1700 - timedelta(minutes=1))
    assert butler.keys == []


async def test_schedule_covers_the_us_night_across_athens_midnight(events):
    # 23:00 Athens: still Wednesday in the US, and tonight's games run until the Athens morning.
    butler = await schedule_at(events, night_games(3), datetime(2026, 10, 21, 20, 0, tzinfo=UTC))
    assert butler.keys == ["schedule:2026-10-21"]


async def test_crowded_night_puts_two_games_per_row(events):
    butler = await schedule_at(events, night_games(15), ATHENS_1700)
    rows = butler.get("schedule:2026-10-21")["buttons"]
    assert len(rows) == 8 and [len(r) for r in rows] == [2] * 7 + [1]
    first = rows[0][0]
    assert first["label"].endswith(f"{first_game_codes(15)}")


def first_game_codes(n: int) -> str:
    g = night_games(n)[0]
    return f"{g.away.code} @ {g.home.code}"


def test_button_rows():
    full, compact = [{"x": i} for i in range(12)], [{"c": i} for i in range(12)]
    assert button_rows(full, compact) == [[b] for b in full]
    full.append({"x": 12})
    compact.append({"c": 12})
    assert button_rows(full, compact)[-1] == [{"c": 12}]


async def test_register_builds_manifest_from_teams(events):
    notifier, butler, _ = make(events)
    notifier.espn.fail_teams = 1
    await notifier.register()
    manifest = butler.manifest
    assert manifest["name"] == "NBA"
    teams = manifest["settings"][0]
    assert teams["key"] == "teams" and teams["default"] == [] and len(teams["options"]) == 30
    assert manifest["commands"] == [{"command": "nba", "description": "Live NBA scores"}]


def test_manifest_without_callback_has_no_commands():
    assert "commands" not in build_manifest(parse_teams(load("teams.json")), "")


async def test_tick_starts_tasks_and_keeps_going_when_a_refresh_fails(events):
    games = night_games(2, first=ATHENS_1700 + timedelta(hours=9))
    espn = FakeEspn(days=days(games))
    notifier, _, clock = make(events, start=ATHENS_1700, espn=espn)
    await notifier.tick()
    assert set(notifier._tasks) == {"1000", "1001"}
    espn.fail_boards = True
    clock.t += timedelta(hours=3)
    await notifier.tick()  # logs the refresh error, keeps the known games
    assert len(notifier.games()) == 2
    for task in notifier._tasks.values():
        task.cancel()


async def test_first_refresh_failure_propagates(events):
    espn = FakeEspn(days={})
    espn.fail_boards = True
    notifier, _, _ = make(events, start=ATHENS_1700, espn=espn)
    with pytest.raises(ApiError):
        await notifier.tick()


async def test_rescheduled_game_restarts_its_waiting_tracker(events):
    game = night_games(1, first=ATHENS_1700 + timedelta(hours=9))[0]
    espn = FakeEspn(days=days([game]))
    notifier, _, clock = make(events, start=ATHENS_1700, espn=espn)
    await notifier.tick()
    first = notifier._tasks[game.id]
    moved = replace(game, tipoff=game.tipoff + timedelta(hours=1))
    espn.days = days([moved])
    clock.t += timedelta(hours=3)
    await notifier.tick()
    await asyncio.sleep(0)
    assert first.cancelled() and notifier._tracked[game.id].tipoff == moved.tipoff
    notifier._tasks[game.id].cancel()


async def test_tbd_and_cancelled_games_are_not_tracked(events):
    tbd, off = night_games(2, first=ATHENS_1700 + timedelta(hours=9))
    tbd = replace(tbd, time_valid=False)
    off = replace(off, status=Status("post", "STATUS_POSTPONED"))
    notifier, _, _ = make(events, start=ATHENS_1700, espn=FakeEspn(days=days([tbd, off])))
    await notifier.tick()
    assert notifier._tasks == {}


# --- last night's results ----------------------------------------------------------------------

ATHENS_1100 = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)  # 11:00 Athens on Thursday = 04:00 Eastern


async def results_at(events, games, at: datetime, boxes=None):
    espn = FakeEspn(days=days(games), boxes=boxes)
    notifier, butler, clock = make(events, start=at, espn=espn)
    await notifier.refresh_schedule()
    await notifier.maybe_send_results()
    return notifier, butler, clock, espn


async def test_results_at_eleven_cover_last_us_night(events):
    finished = [replace(g, status=FINAL) for g in night_games(3)]
    _, butler, _, _ = await results_at(events, finished, ATHENS_1100)
    msg = butler.get("results:2026-10-21")
    assert msg["silent"] and msg["tags"] == {"daily": ["results"]}
    assert "NBA results</b> · night of Wed 21 Oct (US)" in msg["text"]
    assert len(msg["buttons"]) == 3 and "<pre>" in msg["buttons"][0][0]["reveal"]
    assert msg["buttons"][0][0]["label"].startswith("✅ ")


async def test_no_results_before_eleven(events):
    _, butler, _, _ = await results_at(events, night_games(3), ATHENS_1100 - timedelta(minutes=1))
    assert butler.keys == []


async def test_results_wait_for_a_running_game_then_send_what_they_have(events):
    games = night_games(2)
    notifier, butler, clock, _ = await results_at(events, games, ATHENS_1100, boxes=[FINAL_BOX, box()])
    assert butler.keys == []  # second game's box score is still live: wait
    clock.t += timedelta(hours=2)
    await notifier.maybe_send_results()
    msg = butler.get("results:2026-10-21")
    assert len(msg["buttons"]) == 1 and "1 result not available yet" in msg["text"]


async def test_results_fetch_finished_box_scores_once(events):
    games = night_games(2)
    notifier, _, clock, espn = await results_at(events, games, ATHENS_1100, boxes=[FINAL_BOX, box()])
    calls = espn.summary_calls
    clock.t += timedelta(minutes=10)
    await notifier.maybe_send_results()
    assert espn.summary_calls == calls + 1  # only the unfinished game again


async def test_stale_results_are_skipped(events):
    _, butler, _, _ = await results_at(events, night_games(2), ATHENS_1100 + timedelta(hours=6))
    assert butler.keys == []


async def test_postponed_games_do_not_hold_up_results(events):
    played, off = night_games(2)
    off = replace(off, status=Status("post", "STATUS_POSTPONED"))
    _, butler, _, _ = await results_at(events, [played, off], ATHENS_1100)
    assert len(butler.get("results:2026-10-21")["buttons"]) == 1


async def test_crowded_results_use_compact_labels(events):
    _, butler, _, _ = await results_at(events, night_games(13), ATHENS_1100)
    rows = butler.get("results:2026-10-21")["buttons"]
    assert [len(r) for r in rows] == [2] * 6 + [1]
    assert not rows[0][0]["label"].startswith("✅")


# --- game days ---------------------------------------------------------------------------------


def test_us_day_and_game_night():
    # 02:30 Athens Thursday = 19:30 Eastern Wednesday.
    assert us_day(datetime(2026, 10, 21, 23, 30, tzinfo=UTC)) == date(2026, 10, 21)
    # 07:00 Athens Thursday: Wednesday night's late games may still be on, until 06:00 Eastern.
    assert game_night(datetime(2026, 10, 22, 4, 0, tzinfo=UTC)) == date(2026, 10, 21)
    assert game_night(datetime(2026, 10, 22, 10, 30, tzinfo=UTC)) == date(2026, 10, 22)


def test_quiet_hours_setting():
    s = Settings(pibutler_url="x", pibutler_api_key="k")
    assert s.quiet(time(0, 0)) and s.quiet(time(8, 59))
    assert not s.quiet(time(9, 0)) and not s.quiet(time(23, 59))
    wrapping = replace(s, quiet_start=time(23, 0), quiet_end=time(8, 0))
    assert wrapping.quiet(time(23, 30)) and wrapping.quiet(time(7, 0)) and not wrapping.quiet(time(12, 0))


# --- review regressions ------------------------------------------------------------------------


async def test_late_reminder_says_how_long_is_actually_left(events):
    notifier, butler, _ = make(events, [PRE, FINAL], start=TIPOFF - timedelta(minutes=8))
    await notifier.track(GAME)
    assert "Tip-off in 8 min" in butler.get(f":reminder:{TIPOFF:%Y-%m-%dT%H:%M:%SZ}")["text"]


async def test_final_sent_anyway_shows_the_scoreboard_result(events):
    stuck = box(90, 95, live=True)  # scoreboard final: 97-95
    notifier, butler, _ = make(events, [FINAL], start=TIPOFF + timedelta(hours=2), boxes=[stuck])
    await notifier.track(GAME)
    final = butler.get(":final")
    assert f"95{DASH}97" in final["text"] and f"95{DASH}90" not in final["text"]


async def test_slow_fetch_is_shared_even_past_its_ttl():
    from nba_notifier.cache import TtlCache

    now = [0.0]
    cache = TtlCache(10, timeout=None, clock=lambda: now[0])
    release = asyncio.Event()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await release.wait()
        return calls

    first = asyncio.create_task(cache.get("k", fetch))
    await asyncio.sleep(0)
    now[0] = 30.0  # the fetch is still running long after its TTL
    second = asyncio.create_task(cache.get("k", fetch))
    await asyncio.sleep(0)
    release.set()
    assert await first == await second == 1 and calls == 1


def test_schedule_and_follow_texts_use_the_configured_quiet_hours():
    from nba_notifier import reports
    from nba_notifier.tracker import topic

    note = reports.quiet_note(time(23, 0), time(8, 0))
    assert "Between 23:00 and 08:00 they arrive silently." in topic(GAME, note)["follow_text"]
