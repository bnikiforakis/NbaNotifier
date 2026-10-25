"""Weekly team schedules: the week's US game days, one message per team, to its followers only."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from test_tracker import GAME, SCHEDULED, FakeButler, FakeClock, FakeEspn

from nba_notifier import weekly
from nba_notifier.api import ApiError
from nba_notifier.config import Settings
from nba_notifier.models import Status
from nba_notifier.store import EventStore
from nba_notifier.tracker import KINDS, Notifier, build_manifest

MONDAY = date(2026, 10, 26)
TEAMS = {t.code: t for g in SCHEDULED for t in (g.home, g.away)}
ORL, ATL, MIA, MIN, WSH, MIL = (TEAMS[c] for c in ("ORL", "ATL", "MIA", "MIN", "WSH", "MIL"))
POSTPONED = Status("post", "STATUS_POSTPONED", 0, "", completed=False)


def game(gid: str, day: date, tipoff: datetime, away, home, **changes):
    return replace(GAME, id=gid, day=day, tipoff=tipoff, away=away, home=home, **changes)


WEEK = {
    date(2026, 10, 25): [game("1", date(2026, 10, 25), datetime(2026, 10, 25, 23, 0, tzinfo=UTC), ORL, MIL)],
    MONDAY: [game("2", MONDAY, datetime(2026, 10, 26, 23, 30, tzinfo=UTC), ATL, ORL)],  # Tue 01:30 Athens
    date(2026, 10, 29): [game("3", date(2026, 10, 29), datetime(2026, 10, 30, 2, 30, tzinfo=UTC), ORL, MIA)],
    date(2026, 10, 30): [
        game("4", date(2026, 10, 30), datetime(2026, 10, 30, 23, 0, tzinfo=UTC), MIL, ORL, status=POSTPONED)
    ],
    date(2026, 10, 31): [  # no time yet: ESPN parks it at midnight Eastern
        game("5", date(2026, 10, 31), datetime(2026, 11, 1, 4, 0, tzinfo=UTC), ORL, WSH, time_valid=False)
    ],
    # Sunday, after the US clocks went back: 22:30 EST is 05:30 Monday in Athens, still this week.
    date(2026, 11, 1): [game("6", date(2026, 11, 1), datetime(2026, 11, 2, 3, 30, tzinfo=UTC), MIN, ORL)],
    date(2026, 11, 2): [game("7", date(2026, 11, 2), datetime(2026, 11, 3, 0, 0, tzinfo=UTC), ORL, ATL)],
}


def test_days_are_monday_to_sunday():
    assert weekly.days(MONDAY) == [MONDAY + timedelta(days=d) for d in range(7)]


def test_team_weeks_group_by_team_and_skip_cancelled():
    games = [g for d in weekly.days(MONDAY) for g in WEEK.get(d, [])]
    weeks = weekly.team_weeks(games)
    assert set(weeks) == {"ORL", "ATL", "MIA", "WSH", "MIN"}  # MIL only had the postponed game
    team, fixtures = weeks["ORL"]
    assert team == ORL
    assert [(f.game.id, f.opponent.code, f.home) for f in fixtures] == [
        ("2", "ATL", True),
        ("3", "MIA", False),
        ("5", "WSH", False),
        ("6", "MIN", True),
    ]


def test_message():
    games = [g for d in weekly.days(MONDAY) for g in WEEK.get(d, [])]
    team, fixtures = weekly.team_weeks(games)["ORL"]
    assert weekly.message(team, fixtures).splitlines() == [
        f"📅 <b>{ORL.short_name} weekly schedule</b>",
        "",
        f"{{{{time:2026-10-26T23:30:00Z}}}} · vs <b>{ATL.short_name}</b> (H)",
        f"{{{{time:2026-10-30T02:30:00Z}}}} · vs <b>{MIA.short_name}</b> (A)",
        f"Sat 31 Oct (US), time TBD · vs <b>{WSH.short_name}</b> (A)",
        f"{{{{time:2026-11-02T03:30:00Z}}}} · vs <b>{MIN.short_name}</b> (H)",
    ]


def test_weekly_is_a_notifications_option_on_by_default():
    daily = build_manifest([ORL])["settings"][1]
    assert {"value": "weekly", "label": "Weekly team schedule"} in KINDS
    assert "weekly" in daily["default"]


@pytest.fixture
async def events():
    store = await EventStore.open(":memory:")
    yield store
    await store.close()


def make(events, start: datetime):
    clock = FakeClock(start)
    butler = FakeButler()
    espn = FakeEspn(days=WEEK)
    settings = Settings(pibutler_url="http://x", pibutler_api_key="k")
    notifier = Notifier(settings, espn, butler, events, clock=clock, sleep=clock.sleep)
    return notifier, butler, clock, espn


def weekly_sent(butler: FakeButler) -> dict[str, dict]:
    return {n["key"]: n for n in butler.sent if n["key"].startswith("weekly")}


async def test_sent_on_monday_at_one_pm_to_each_teams_followers(events):
    notifier, butler, clock, _ = make(events, datetime(2026, 10, 26, 10, 50, tzinfo=UTC))  # 12:50 Athens
    await notifier.maybe_send_weekly()
    assert not weekly_sent(butler)
    clock.t = datetime(2026, 10, 26, 11, 0, tzinfo=UTC)  # 13:00 Athens, 07:00 Eastern
    await notifier.maybe_send_weekly()
    sent = weekly_sent(butler)
    assert set(sent) == {f"weekly:2026-10-26:{c}" for c in ("ORL", "ATL", "MIA", "WSH", "MIN")}
    orl = sent["weekly:2026-10-26:ORL"]
    assert orl["silent"] and orl["tags"] == {"daily": ["weekly"]}
    assert orl["audience"] == {"topic": "week:2026-10-26:ORL", "following": True}
    assert orl["topics"] == [
        {
            "id": "week:2026-10-26:ORL",
            "title": f"{ORL.short_name} weekly schedule",
            "auto_follow": {"teams": ["ORL"]},
        }
    ]
    assert orl["expires_at"] == "2026-10-26T22:00:00Z"  # end of Monday, Athens
    assert "2026-11-03" not in orl["text"] and MIL.short_name not in orl["text"]  # next week, last week
    clock.t += timedelta(minutes=10)
    await notifier.maybe_send_weekly()  # once a week
    assert len(weekly_sent(butler)) == 5


async def test_not_on_other_days(events):
    notifier, butler, _, espn = make(events, datetime(2026, 10, 27, 11, 0, tzinfo=UTC))  # Tuesday
    await notifier.maybe_send_weekly()
    assert not weekly_sent(butler) and espn.board_calls == 0


async def test_tick_sends_the_weekly_schedules(events, monkeypatch):
    notifier, _, _, _ = make(events, datetime(2026, 10, 26, 11, 0, tzinfo=UTC))
    calls = []

    async def record():
        calls.append(True)
        raise ApiError("down")  # a failure is logged; the tick goes on

    monkeypatch.setattr(notifier, "maybe_send_weekly", record)
    monkeypatch.setattr(notifier, "_should_track", lambda game, now: False)
    await notifier.tick()
    assert calls


async def test_espn_down_retries_next_tick(events):
    notifier, butler, clock, espn = make(events, datetime(2026, 10, 26, 11, 0, tzinfo=UTC))
    espn.fail_boards = True
    with pytest.raises(ApiError):
        await notifier.maybe_send_weekly()
    assert not weekly_sent(butler)
    espn.fail_boards = False
    clock.t += timedelta(minutes=10)
    await notifier.maybe_send_weekly()
    assert len(weekly_sent(butler)) == 5


async def test_an_empty_week_is_asked_again(events):
    notifier, butler, clock, espn = make(events, datetime(2026, 10, 26, 11, 0, tzinfo=UTC))
    espn.days = {}
    await notifier.maybe_send_weekly()
    assert not await events.has("weekly:2026-10-26")
    espn.days = WEEK
    clock.t += timedelta(minutes=10)
    await notifier.maybe_send_weekly()
    assert len(weekly_sent(butler)) == 5


async def test_a_rejected_team_doesnt_stop_the_others(events):
    notifier, butler, clock, _ = make(events, datetime(2026, 10, 26, 11, 0, tzinfo=UTC))
    butler.fail_next = 1  # the first team, alphabetically
    await notifier.maybe_send_weekly()
    assert len(weekly_sent(butler)) == 4 and not await events.has("weekly:2026-10-26")
    clock.t += timedelta(minutes=10)
    await notifier.maybe_send_weekly()  # only the missing one
    assert len(weekly_sent(butler)) == 5 and await events.has("weekly:2026-10-26")


async def test_a_failed_send_resumes_with_the_teams_not_yet_sent(events):
    notifier, butler, _, _ = make(events, datetime(2026, 10, 26, 11, 0, tzinfo=UTC))
    await notifier.events.add("weekly:2026-10-26:ORL")  # sent before a crash
    await notifier.maybe_send_weekly()
    assert set(weekly_sent(butler)) == {f"weekly:2026-10-26:{c}" for c in ("ATL", "MIA", "WSH", "MIN")}
