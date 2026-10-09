"""Domain models for NBA schedules, live state and box scores."""

from dataclasses import dataclass, field
from datetime import date, datetime

REGULATION_PERIODS = 4

# ESPN status names (``status.type.name``) that mean a period has just ended.
BREAK_STATUSES = frozenset({"STATUS_END_PERIOD", "STATUS_HALFTIME"})


def period_label(period: int) -> str:
    """Human label for a 1-based period number: Q1..Q4, then OT1, OT2, ..."""
    return f"Q{period}" if period <= REGULATION_PERIODS else f"OT{period - REGULATION_PERIODS}"


def pct(made: int, attempted: int) -> float | None:
    return made / attempted * 100 if attempted else None


@dataclass(frozen=True)
class Team:
    code: str  # ESPN abbreviation, e.g. "LAL"
    name: str  # "Los Angeles Lakers"
    short_name: str  # "Lakers"


@dataclass(frozen=True)
class Status:
    """Where a game stands, from ESPN's ``status``.

    ``state`` is ``pre``, ``in`` or ``post``. ``period`` is the current (or last) period and
    ``clock`` the time left in it, e.g. ``"5:12"``.
    """

    state: str = "pre"
    name: str = "STATUS_SCHEDULED"
    period: int = 0
    clock: str = ""
    completed: bool = False

    @property
    def live(self) -> bool:
        return self.state == "in"

    @property
    def at_break(self) -> bool:
        return self.live and self.name in BREAK_STATUSES

    @property
    def cancelled(self) -> bool:
        """Over without a result: postponed, cancelled, suspended or forfeited."""
        return self.state == "post" and not self.completed

    @property
    def ended_periods(self) -> int:
        """How many periods are over: the current one counts once it hits a break or the final."""
        if self.completed or self.at_break:
            return self.period
        return max(self.period - 1, 0) if self.live else 0


@dataclass(frozen=True)
class Game:
    """One game of a US game day. ``quarter_scores`` holds per-period points as (home, away)."""

    id: str
    day: date  # the US (Eastern) game day it belongs to: one "night" of NBA games
    tipoff: datetime
    home: Team
    away: Team
    status: Status = field(default_factory=Status)
    home_score: int | None = None
    away_score: int | None = None
    quarter_scores: list[tuple[int, int]] = field(default_factory=list)
    venue: str | None = None
    time_valid: bool = True  # False while the tip-off time is still TBD

    @property
    def identifier(self) -> str:
        return self.id

    @property
    def score(self) -> tuple[int, int] | None:
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score, self.away_score

    def score_after(self, periods: int) -> tuple[int, int] | None:
        """The score at the end of ``periods``: the live score during that break, else the sum of the
        period scores (``None`` if they're incomplete)."""
        if self.status.at_break and self.status.period == periods and self.score:
            return self.score
        if len(self.quarter_scores) < periods:
            return None
        played = self.quarter_scores[:periods]
        return sum(h for h, _ in played), sum(a for _, a in played)


@dataclass(frozen=True)
class PlayerLine:
    name: str  # short form, e.g. "K. Durant"
    team_code: str
    minutes: int
    points: int
    rebounds: int
    assists: int
    steals: int
    blocks: int
    turnovers: int
    fouls: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    plus_minus: int | None = None
    starter: bool = False
    played: bool = True


@dataclass(frozen=True)
class TeamLine:
    code: str
    name: str
    points: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    rebounds: int
    off_reb: int
    def_reb: int
    assists: int
    steals: int
    blocks: int
    turnovers: int
    fouls: int

    @property
    def fg2m(self) -> int:
        return self.fgm - self.fg3m

    @property
    def fg2a(self) -> int:
        return self.fga - self.fg3a


@dataclass(frozen=True)
class BoxScore:
    home: TeamLine
    away: TeamLine
    home_players: list[PlayerLine]
    away_players: list[PlayerLine]
    quarter_scores: list[tuple[int, int]]
    live: bool = False

    @property
    def players(self) -> list[PlayerLine]:
        return self.home_players + self.away_players
