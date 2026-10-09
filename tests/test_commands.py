"""/nba callback views through aiohttp's test client, with a scripted scoreboard and a fixed clock."""

import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nba_notifier.api import ApiError, parse_scoreboard, parse_summary
from nba_notifier.commands import ACTION_RE, Reply, Scoreboard, callback_path, clock, create_app
from nba_notifier.models import Status
from nba_notifier.reports import DASH

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "secret"
AUTH = {"Authorization": f"Bearer {KEY}"}
NIGHT = date(2026, 10, 21)
NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)  # 20:00 Eastern on the 21st


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


SCHEDULED = parse_scoreboard(load("scoreboard_20261021_scheduled.json"), NIGHT)
HALF = parse_summary(load("summary_401898395_halftime.json"))
PRE = SCHEDULED[0]  # Hawks @ Magic
LIVE = replace(
    SCHEDULED[1],
    status=Status("in", "STATUS_IN_PROGRESS", 3, "5:12"),
    home_score=70,
    away_score=64,
    quarter_scores=[(30, 28), (25, 26), (15, 10)],
)
FINAL = replace(
    SCHEDULED[2],
    status=Status("post", "STATUS_FINAL", 4, "0.0", completed=True),
    home_score=101,
    away_score=110,
    quarter_scores=[(25, 30), (25, 30), (25, 25), (26, 25)],
)
POSTPONED = replace(SCHEDULED[3], status=Status("post", "STATUS_POSTPONED", 0, ""))
TONIGHT = [PRE, LIVE, FINAL, POSTPONED, *SCHEDULED[4:]]


class FakeEspn:
    def __init__(self, box=HALF):
        self.box = box
        self.summaries = 0

    async def summary(self, event_id):
        self.summaries += 1
        if isinstance(self.box, Exception):
            raise self.box
        return self.box


class FakeNotifier:
    def __init__(self, boards: dict, espn: FakeEspn | None = None, games=()):
        self.boards = boards
        self.espn = espn or FakeEspn()
        self._games = list(games)
        self.board_calls = 0

    def now(self):
        return NOW

    def games(self):
        return self._games

    async def scoreboard(self, day):
        self.board_calls += 1
        board = self.boards.get(day, [])
        if isinstance(board, Exception):
            raise board
        return board


async def post(notifier: FakeNotifier, body: dict) -> dict:
    async with TestClient(TestServer(create_app(Scoreboard(notifier), KEY))) as client:
        resp = await client.post("/pibutler", json=body, headers=AUTH)
        assert resp.status == 200
        return await resp.json()


def command() -> dict:
    return {"type": "command", "command": "nba", "args": "", "user": {"id": 1, "timezone": "Europe/Athens"}}


def action(data: str) -> dict:
    return {"type": "action", "data": data, "user": {"id": 1, "timezone": "Europe/Athens"}}


def labels(reply: dict) -> list[str]:
    return [b["label"] for row in reply["buttons"] for b in row]


def actions(reply: dict) -> list[str]:
    return [b.get("action") for row in reply["buttons"] for b in row]


# --- the callback endpoint -------------------------------------------------------------------


async def test_wrong_key_is_401():
    async with TestClient(TestServer(create_app(Scoreboard(FakeNotifier({})), KEY))) as client:
        resp = await client.post("/pibutler", json=command(), headers={"Authorization": "Bearer nope"})
        assert resp.status == 401
        resp = await client.post("/pibutler", json=command())
        assert resp.status == 401


@pytest.mark.parametrize("data", [b"{not json", b"[1, 2]"])
async def test_bad_json_is_400(data):
    async with TestClient(TestServer(create_app(Scoreboard(FakeNotifier({})), KEY))) as client:
        headers = AUTH | {"Content-Type": "application/json"}
        resp = await client.post("/pibutler", data=data, headers=headers)
        assert resp.status == 400


def test_callback_path():
    assert callback_path("http://nba-notifier:8081/pibutler") == "/pibutler"
    assert callback_path("http://nba-notifier:8081") == "/"


async def test_unknown_command_is_friendly():
    reply = await post(FakeNotifier({}), {"type": "command", "command": "standings"})
    assert "/nba" in reply["text"]


# --- list view -------------------------------------------------------------------------------


async def test_list_shows_tonights_games_one_per_row():
    reply = await post(FakeNotifier({NIGHT: TONIGHT}), command())
    assert reply["text"].startswith("🏀 <b>NBA</b> · night of Wed 21 Oct (US)")
    assert all(len(row) == 1 for row in reply["buttons"])
    assert len(reply["buttons"]) == 11
    names = labels(reply)
    assert names[0] == "⏳ {{hm:2026-10-21T23:00:00Z}} Hawks @ Magic"
    live = next(n for n in names if n.startswith("🔴"))
    assert live == f"🔴 {LIVE.away.short_name} 64{DASH}70 {LIVE.home.short_name} · Q3 5:12"
    assert f"✅ {FINAL.away.short_name} 110{DASH}101 {FINAL.home.short_name}" in names
    assert any(n.startswith("🚫 ") for n in names)
    assert all(a.startswith("g|") and ACTION_RE.match(a) for a in actions(reply))
    assert all(len(n) <= 64 for n in names)


async def test_list_with_more_than_twelve_games_goes_two_per_row():
    many = [replace(SCHEDULED[i % 11], id=str(500 + i)) for i in range(15)]
    many[0] = replace(LIVE, id="500")
    reply = await post(FakeNotifier({NIGHT: many}), command())
    assert len(reply["buttons"]) == 8
    assert all(len(row) == 2 for row in reply["buttons"][:-1])
    assert len(reply["buttons"][-1]) == 1
    names = labels(reply)
    assert len(names) == 15
    assert f"🔴 {LIVE.away.code} 64{DASH}70 {LIVE.home.code}" in names
    assert any(n.startswith("⏳ ") and n.endswith(f"{PRE.away.code}{DASH}{PRE.home.code}") for n in names)


async def test_list_without_games():
    reply = await post(FakeNotifier({}), command())
    assert reply["text"].endswith("No games tonight.")
    assert reply["buttons"] == []


async def test_list_when_espn_is_down():
    reply = await post(FakeNotifier({NIGHT: ApiError("down")}), command())
    assert "unavailable" in reply["text"]


async def test_list_shows_last_night_until_six_eastern():
    # 09:00 UTC = 05:00 Eastern on the 22nd: still the night of the 21st.
    notifier = FakeNotifier({NIGHT: TONIGHT})
    notifier.now = lambda: datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    assert "night of Wed 21 Oct" in (await post(notifier, command()))["text"]
    notifier.now = lambda: datetime(2026, 10, 22, 9, 0, tzinfo=UTC) + timedelta(hours=2)
    assert "No games tonight" in (await post(notifier, command()))["text"]


# --- game view -------------------------------------------------------------------------------


async def test_game_view_before_tipoff():
    reply = await post(FakeNotifier({NIGHT: TONIGHT}), action(f"g|{PRE.id}"))
    assert reply["text"].split("\n") == [
        "⏳ <b>Hawks</b> @ <b>Magic</b>",
        "Tip-off {{time:2026-10-21T23:00:00Z}}",
        "📍 Kia Center",
    ]
    assert actions(reply) == [f"g|{PRE.id}", "l"]


async def test_game_view_tbd():
    board = [replace(PRE, time_valid=False)]
    reply = await post(FakeNotifier({NIGHT: board}), action(f"g|{PRE.id}"))
    assert "Tip-off time TBD" in reply["text"]


async def test_game_view_live():
    reply = await post(FakeNotifier({NIGHT: TONIGHT}), action(f"g|{LIVE.id}"))
    lines = reply["text"].split("\n")
    away, home = LIVE.away.short_name, LIVE.home.short_name
    assert lines[0] == f"🔴 <b>{away}</b> 64{DASH}70 <b>{home}</b>"
    assert lines[1] == "Q3 5:12"
    assert lines[3] == f"Q1 28{DASH}30 · Q2 26{DASH}25 · Q3 10{DASH}15"
    assert actions(reply) == [f"s|{LIVE.id}", f"g|{LIVE.id}", "l"]


async def test_game_view_final():
    reply = await post(FakeNotifier({NIGHT: TONIGHT}), action(f"g|{FINAL.id}"))
    away, home = FINAL.away.short_name, FINAL.home.short_name
    assert reply["text"].startswith(f"✅ <b>Final</b> · <b>{away}</b> 110{DASH}101 {home}")


async def test_game_view_final_in_overtime():
    ot = replace(FINAL, quarter_scores=[*FINAL.quarter_scores, (10, 5)])
    reply = await post(FakeNotifier({NIGHT: [ot]}), action(f"g|{FINAL.id}"))
    assert reply["text"].startswith("✅ <b>Final</b> (OT)")
    assert "OT1" in reply["text"]


async def test_game_view_cancelled():
    reply = await post(FakeNotifier({NIGHT: TONIGHT}), action(f"g|{POSTPONED.id}"))
    assert reply["text"].startswith("🚫 ")
    assert "Not played (postponed)" in reply["text"]
    assert actions(reply) == ["l"]


async def test_game_from_another_day_is_found_through_the_schedule():
    other = replace(FINAL, id="999", day=date(2026, 10, 20))
    notifier = FakeNotifier({NIGHT: TONIGHT, other.day: [other]}, games=[other])
    reply = await post(notifier, action("g|999"))
    assert reply["text"].startswith("✅ <b>Final</b>")


async def test_game_no_longer_on_the_scoreboard():
    reply = await post(FakeNotifier({NIGHT: TONIGHT}), action("g|123"))
    assert "isn't on the scoreboard" in reply["text"]
    assert actions(reply) == ["l"]


@pytest.mark.parametrize("data", ["", "x", "g|", "g|abc", "s|1|2", "z|401", "l|1"])
async def test_garbage_actions_have_expired(data):
    reply = await post(FakeNotifier({NIGHT: TONIGHT}), action(data))
    assert "expired" in reply["text"]


async def test_back_returns_to_the_list():
    reply = await post(FakeNotifier({NIGHT: TONIGHT}), action("l"))
    assert reply["text"].startswith("🏀 <b>NBA</b>")


# --- stats view ------------------------------------------------------------------------------


async def test_stats_live():
    espn = FakeEspn()
    reply = await post(FakeNotifier({NIGHT: TONIGHT}, espn), action(f"s|{LIVE.id}"))
    assert reply["text"].startswith("📊 <b>Live stats</b> · Q3 5:12 · ")
    assert "Top scorers" in reply["text"] and "Team stats" in reply["text"]
    assert "BLK" not in reply["text"]
    assert actions(reply) == [f"s|{LIVE.id}", f"g|{LIVE.id}"]
    assert espn.summaries == 1


async def test_stats_final():
    box = replace(parse_summary(load("summary_401898395_halftime.json")), live=False)
    reply = await post(FakeNotifier({NIGHT: TONIGHT}, FakeEspn(box)), action(f"s|{FINAL.id}"))
    assert reply["text"].startswith("📊 <b>Final</b> · ")
    assert "BLK" in reply["text"] and "PIR" not in reply["text"]


async def test_stats_before_tipoff():
    reply = await post(FakeNotifier({NIGHT: TONIGHT}, FakeEspn()), action(f"s|{PRE.id}"))
    assert "hasn't started" in reply["text"]


@pytest.mark.parametrize("box", [None, ApiError("down")])
async def test_stats_unavailable(box):
    reply = await post(FakeNotifier({NIGHT: TONIGHT}, FakeEspn(box)), action(f"s|{LIVE.id}"))
    assert "unavailable" in reply["text"]


async def test_every_tap_reads_the_trackers_shared_scoreboard():
    # No second cache layer: the tracker's short-lived cache is the only one, so Refresh is fresh.
    notifier = FakeNotifier({NIGHT: TONIGHT})
    scoreboard = Scoreboard(notifier)
    await scoreboard.reply(command())
    await scoreboard.reply(action(f"g|{LIVE.id}"))
    assert notifier.board_calls == 2


async def test_long_text_is_replaced_not_cut():
    out = Reply("x" * 5000, [[{"label": "a", "action": "l"}]] * 20).as_dict()
    assert out["text"].startswith("⚠️") and len(out["buttons"]) == 12


# --- clock -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (Status("in", "STATUS_HALFTIME", 2, "0.0"), "Halftime"),
        (Status("in", "STATUS_END_PERIOD", 1, "0.0"), "End of Q1"),
        (Status("in", "STATUS_IN_PROGRESS", 3, "5:12"), "Q3 5:12"),
        (Status("in", "STATUS_IN_PROGRESS", 5, "0:45"), "OT1 0:45"),
        (Status("in", "STATUS_END_PERIOD", 5, "0.0"), "End of OT1"),
    ],
)
def test_clock(status, text):
    assert clock(status) == text


def test_action_data_fits_telegram():
    assert all(ACTION_RE.match(f"{v}|{g.id}") for v in "gs" for g in SCHEDULED)
    assert not re.search(r"\s", "".join(g.id for g in SCHEDULED))
