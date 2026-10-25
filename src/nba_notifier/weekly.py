"""Weekly team schedules: one silent message per followed team, Monday afternoon (ADR-N4).

The port of EuroleagueNotifier's (its ADR-025), on NBA time: the week is the seven US game days
Monday to Sunday, so Sunday's late games (after 05:00 in Europe) are in it and Monday's haven't
started when it's sent. Each message lists the team's games in the recipient's time:

    📅 Celtics weekly schedule

    Tue 27 Oct, 02:00 · vs Knicks (A)
    Fri 30 Oct, 02:30 · vs Heat (H)

It goes to everyone auto-following the team, through an audience topic per team and week.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape

from nba_notifier.models import Game, Team
from nba_notifier.reports import iso_z, short_day


@dataclass(frozen=True)
class Fixture:
    game: Game
    home: bool  # the team plays at home

    @property
    def opponent(self) -> Team:
        return self.game.away if self.home else self.game.home


def days(monday: date) -> list[date]:
    """The week's US game days, Monday to Sunday."""
    return [monday + timedelta(days=d) for d in range(7)]


def team_weeks(games: Iterable[Game]) -> dict[str, tuple[Team, list[Fixture]]]:
    """Per team code: the team and its fixtures among ``games`` (cancelled ones left out)."""
    weeks: dict[str, tuple[Team, list[Fixture]]] = {}
    for game in games:
        if game.status.cancelled:
            continue
        for team, home in ((game.home, True), (game.away, False)):
            weeks.setdefault(team.code, (team, []))[1].append(Fixture(game, home))
    for _, fixtures in weeks.values():
        fixtures.sort(key=lambda f: (f.game.day, f.game.tipoff, f.game.id))
    return weeks


def _when(game: Game) -> str:
    """The recipient's local day and time, or the US day while ESPN has no time yet."""
    if game.time_valid:
        return f"{{{{time:{iso_z(game.tipoff)}}}}}"
    return f"{short_day(game.day)} (US), time TBD"


def message(team: Team, fixtures: list[Fixture]) -> str:
    lines = [f"📅 <b>{escape(team.short_name)} weekly schedule</b>", ""]
    for f in fixtures:
        where = "H" if f.home else "A"
        lines.append(f"{_when(f.game)} · vs <b>{escape(f.opponent.short_name)}</b> ({where})")
    return "\n".join(lines)


def topic(team: Team, monday: date) -> dict:
    """The week's audience: everyone auto-following the team."""
    return {
        "id": f"week:{monday.isoformat()}:{team.code}",
        "title": f"{team.short_name} weekly schedule",
        "auto_follow": {"teams": [team.code]},
    }
