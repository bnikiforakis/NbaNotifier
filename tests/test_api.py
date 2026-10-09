"""Parsers against real ESPN payloads, and the HTTP client against a mock transport."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from nba_notifier.api import ApiError, EspnClient, parse_scoreboard, parse_summary, parse_teams
from nba_notifier.models import Status

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


FINALS = parse_scoreboard(load("scoreboard_20261008_final.json"), date(2026, 10, 8))
SCHEDULED = parse_scoreboard(load("scoreboard_20261021_scheduled.json"), date(2026, 10, 21))
HALFTIME = load("summary_401898395_halftime.json")


# --- scoreboard ------------------------------------------------------------------------------


def test_final_games_parse_scores_and_periods():
    assert len(FINALS) == 6
    game = FINALS[0]
    assert game.id == "401898392"
    assert game.day == date(2026, 10, 8)
    assert game.tipoff == datetime(2026, 10, 8, 23, 0, tzinfo=UTC)
    assert (game.home.code, game.away.code) == ("CLE", "BOS")
    assert game.home.name == "Cleveland Cavaliers"
    assert game.home.short_name == "Cavaliers"
    assert game.score == (113, 124)
    assert game.quarter_scores == [(29, 32), (33, 39), (34, 28), (17, 25)]
    assert game.venue == "Rocket Arena"
    assert game.time_valid
    assert game.status.completed and game.status.state == "post"
    assert game.status.ended_periods == 4
    assert not game.status.live and not game.status.cancelled


def test_home_and_away_come_from_home_away_not_order():
    payload = load("scoreboard_20261008_final.json")
    competitors = payload["events"][0]["competitions"][0]["competitors"]
    competitors.reverse()
    game = parse_scoreboard(payload, date(2026, 10, 8))[0]
    assert (game.home.code, game.away.code) == ("CLE", "BOS")
    assert game.score == (113, 124)


def test_scheduled_games_have_no_score_yet():
    assert len(SCHEDULED) == 11
    game = SCHEDULED[0]
    assert (game.away.code, game.home.code) == ("ATL", "ORL")
    assert game.home_score is None and game.away_score is None
    assert game.score is None
    assert game.status.state == "pre"
    assert game.status.ended_periods == 0
    assert game.venue == "Kia Center"


def test_time_tbd_is_parsed():
    payload = load("scoreboard_20261021_scheduled.json")
    payload["events"][0]["competitions"][0]["timeValid"] = False
    assert not parse_scoreboard(payload, date(2026, 10, 21))[0].time_valid


def test_bad_scoreboard_payload_is_an_api_error():
    with pytest.raises(ApiError):
        parse_scoreboard({"nope": 1}, date(2026, 10, 8))


def test_malformed_events_are_skipped_not_fatal():
    payload = load("scoreboard_20261008_final.json")
    payload["events"][0]["competitions"][0]["competitors"][0]["homeAway"] = "away"
    del payload["events"][1]["date"]
    games = parse_scoreboard(payload, date(2026, 10, 8))
    assert len(games) == 4 and payload["events"][0]["id"] not in {g.id for g in games}


# --- status ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "ended", "at_break", "cancelled"),
    [
        (Status(), 0, False, False),
        (Status("in", "STATUS_IN_PROGRESS", 1, "8:00"), 0, False, False),
        (Status("in", "STATUS_IN_PROGRESS", 3, "11:32"), 2, False, False),
        (Status("in", "STATUS_END_PERIOD", 1, "0.0"), 1, True, False),
        (Status("in", "STATUS_HALFTIME", 2, "0.0"), 2, True, False),
        (Status("in", "STATUS_END_PERIOD", 4, "0.0"), 4, True, False),
        (Status("post", "STATUS_FINAL", 5, "0.0", completed=True), 5, False, False),
        (Status("post", "STATUS_POSTPONED", 0, ""), 0, False, True),
    ],
)
def test_status_progress(status, ended, at_break, cancelled):
    assert status.ended_periods == ended
    assert status.at_break == at_break
    assert status.cancelled == cancelled


def test_score_after_uses_live_score_during_the_break():
    game = replace(
        FINALS[0],
        status=Status("in", "STATUS_HALFTIME", 2, "0.0"),
        home_score=60,
        away_score=70,
        quarter_scores=[(29, 32), (30, 37)],
    )
    assert game.score_after(2) == (60, 70)
    assert game.score_after(1) == (29, 32)


def test_score_after_sums_periods_when_seen_late():
    game = replace(
        FINALS[0],
        status=Status("in", "STATUS_IN_PROGRESS", 3, "10:47"),
        home_score=50,
        away_score=66,
        quarter_scores=[(19, 27), (26, 38), (5, 1)],
    )
    assert game.score_after(2) == (45, 65)
    assert game.score_after(4) is None


# --- summary ---------------------------------------------------------------------------------


def test_halftime_summary_box_score():
    box = parse_summary(HALFTIME)
    assert box is not None and box.live
    assert (box.home.code, box.away.code) == ("DAL", "HOU")
    assert (box.home.points, box.away.points) == (45, 65)
    assert box.quarter_scores == [(19, 27), (26, 38)]
    hou = box.away
    assert (hou.fgm, hou.fga, hou.fg3m, hou.fg3a, hou.ftm, hou.fta) == (25, 47, 7, 15, 8, 13)
    assert (hou.fg2m, hou.fg2a) == (18, 32)
    assert (hou.rebounds, hou.assists, hou.steals) == (28, 18, 6)
    assert (hou.blocks, hou.turnovers, hou.fouls) == (3, 10, 10)
    assert hou.name == "Houston Rockets"


def test_halftime_summary_players():
    box = parse_summary(HALFTIME)
    durant = box.away_players[0]
    assert durant.name == "K. Durant"
    assert durant.team_code == "HOU"
    assert durant.starter and durant.played
    assert (durant.minutes, durant.points, durant.fgm, durant.fga, durant.fta) == (17, 8, 4, 9, 1)
    assert durant.plus_minus == 15
    assert box.home_players[0].plus_minus == -12
    dnp = {p.name for p in box.away_players if not p.played}
    assert "C. Capela" in dnp and "K. Durant" not in dnp
    assert all(p.team_code == "DAL" for p in box.home_players)
    assert box.players == box.home_players + box.away_players


def test_final_summary_is_not_live():
    payload = json.loads(json.dumps(HALFTIME))
    status = payload["header"]["competitions"][0]["status"]["type"]
    status.update(name="STATUS_FINAL", state="post", completed=True)
    assert parse_summary(payload).live is False


def test_pre_game_summary_is_none():
    payload = json.loads(json.dumps(HALFTIME))
    payload["header"]["competitions"][0]["status"]["type"].update(name="STATUS_SCHEDULED", state="pre")
    assert parse_summary(payload) is None
    no_players = json.loads(json.dumps(HALFTIME))
    no_players["boxscore"].pop("players")
    assert parse_summary(no_players) is None


def test_bad_summary_is_an_api_error():
    with pytest.raises(ApiError):
        parse_summary({"header": {}})


# --- teams -----------------------------------------------------------------------------------


def test_teams():
    teams = parse_teams(load("teams.json"))
    assert len(teams) == 30
    assert len({t.code for t in teams}) == 30
    atl = teams[0]
    assert (atl.code, atl.name, atl.short_name) == ("ATL", "Atlanta Hawks", "Hawks")
    with pytest.raises(ApiError):
        parse_teams({})


# --- client ----------------------------------------------------------------------------------


def client(handler) -> EspnClient:
    return EspnClient(transport=httpx.MockTransport(handler), backoff=0)


async def test_scoreboard_asks_for_the_game_day():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=load("scoreboard_20261008_final.json"))

    async with client(handler) as espn:
        games = await espn.scoreboard(date(2026, 10, 8))
    assert len(games) == 6 and games[0].day == date(2026, 10, 8)
    assert seen[0].path.endswith("/basketball/nba/scoreboard")
    assert seen[0].params["dates"] == "20261008"


async def test_summary_asks_for_the_event():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=HALFTIME)

    async with client(handler) as espn:
        box = await espn.summary("401898395")
    assert box.away.code == "HOU"
    assert seen[0].path.endswith("/summary")
    assert seen[0].params["event"] == "401898395"


async def test_teams_endpoint():
    async with client(lambda r: httpx.Response(200, json=load("teams.json"))) as espn:
        assert len(await espn.teams()) == 30


async def test_retries_server_errors_and_transport_errors():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=load("teams.json"))

    async with client(handler) as espn:
        assert len(await espn.teams()) == 30
    assert len(calls) == 3


async def test_gives_up_after_three_attempts():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    async with client(handler) as espn:
        with pytest.raises(ApiError, match="3 attempts"):
            await espn.teams()
    assert len(calls) == 3


async def test_client_errors_are_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"code": 400})

    async with client(handler) as espn:
        with pytest.raises(ApiError, match="400"):
            await espn.scoreboard(date(2026, 10, 8))
    assert len(calls) == 1


async def test_invalid_json_is_an_api_error():
    async with client(lambda r: httpx.Response(200, content=b"<html>")) as espn:
        with pytest.raises(ApiError, match="invalid JSON"):
            await espn.teams()
