"""Replays a real recorded game (HOU @ DAL preseason, 2026-10-09) through the tracker.

The fake ESPN serves, at each moment of the fake clock, the scoreboard and box score the real feed
showed then. Recording started at halftime, so the tracker joins mid-game, like after a restart.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from nba_notifier.api import parse_scoreboard, parse_summary
from nba_notifier.config import Settings
from nba_notifier.reports import DASH
from nba_notifier.store import EventStore
from nba_notifier.tracker import Notifier

RECORDING = json.loads((Path(__file__).parent / "fixtures" / "replay_401898395.json").read_text())
DAY = date.fromisoformat(RECORDING["day"])


def _utc(stamp: str) -> datetime:
    """``20261009T143138Z`` -> an aware UTC datetime."""
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


STATES = [(_utc(s["at"]), parse_scoreboard({"events": [s["event"]]}, DAY)) for s in RECORDING["states"]]
SUMMARIES = [(_utc(s["at"]), parse_summary(s["summary"])) for s in RECORDING["summaries"]]


def latest(items, now):
    return [value for when, value in items if when <= now][-1]


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


class ReplayEspn:
    def __init__(self, clock):
        self.clock = clock

    async def scoreboard(self, day):
        return latest(STATES, self.clock())

    async def summary(self, event_id):
        return latest(SUMMARIES, self.clock())


class Butler:
    def __init__(self):
        self.sent = []

    async def notify(self, key, text, tags=None, expires_at=None, **extra):
        self.sent.append({"key": key, "text": text, **extra})
        return {}


async def test_recorded_game_replays_to_the_right_messages():
    clock = Clock(STATES[0][0])
    butler, events = Butler(), await EventStore.open(":memory:")
    settings = Settings(pibutler_url="x", pibutler_api_key="k", poll_seconds=30)
    notifier = Notifier(settings, ReplayEspn(clock), butler, events, clock=clock, sleep=clock.sleep)
    game = STATES[0][1][0]
    await notifier.track(game)
    await events.close()

    # Joined at halftime: no tip-off, Q2 and Q3 cards, then the final (Q4 had a winner, no Q4 card).
    assert [n["key"] for n in butler.sent] == ["401898395:p2", "401898395:p3", "401898395:final"]
    p2, p3, final = (n["text"] for n in butler.sent)
    assert f"End of Q2</b> · Rockets 65{DASH}45 Mavericks" in p2  # visitors first
    assert f"End of Q3</b> · Rockets 99{DASH}80 Mavericks" in p3
    assert f"Final</b> · <b>Rockets</b> 135{DASH}117 Mavericks" in final
    assert f"Q1 27{DASH}19 · Q2 38{DASH}26 · Q3 34{DASH}35 · Q4 36{DASH}37" in final
    # Sent at the buzzer, not minutes later (the final box score was already there).
    assert clock() - STATES[-1][0] < timedelta(minutes=1)
    # 16:31 Athens: outside the quiet hours, so the final rings.
    assert not butler.sent[-1]["silent"]
