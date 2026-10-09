"""Async client and pure parsers for ESPN's public NBA API.

Source: ``site.api.espn.com/apis/site/v2/sports/basketball/nba`` (no key, ``max-age`` ~5 s).
``cdn.nba.com`` answers 403 to non-browser clients, so it isn't used.

- ``scoreboard?dates=YYYYMMDD``: every game of one US (Eastern) game day, with its status,
  period, clock, score and per-period scores. One request covers a whole night, so it is both the
  schedule and the live feed. Date ranges aren't accepted; one day per request.
- ``summary?event=<id>``: box score (team and player stats) of one game, ~280 KB.
- ``teams``: the 30 teams, for the follow picker.

Scores are strings; competitors carry ``homeAway``; box-score teams are matched by abbreviation.
"""

import asyncio
import json
import logging
from datetime import date, datetime
from types import TracebackType
from typing import Any, Self

import httpx

from nba_notifier.models import BoxScore, Game, PlayerLine, Status, Team, TeamLine

API_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
USER_AGENT = "Mozilla/5.0 (compatible; nba-notifier/0.1)"
TIMEOUT = 15.0
ATTEMPTS = 3
BACKOFF = 1.0

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Raised when an endpoint keeps failing or returns something unparseable."""


def _int(value: Any) -> int:
    """Lenient int: ``""``, ``"--"`` and ``None`` are 0, ``"+15"`` is 15."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    text = str(value).strip() if value is not None else ""
    return _int(text) if text.lstrip("+-").replace(".", "", 1).isdigit() else None


def _pair(value: Any) -> tuple[int, int]:
    """``"25-47"`` -> ``(25, 47)``."""
    made, _, attempted = str(value or "").partition("-")
    return _int(made), _int(attempted)


def _team(raw: dict) -> Team:
    name = raw.get("displayName") or ""
    return Team(
        code=raw.get("abbreviation") or "",
        name=name,
        short_name=raw.get("shortDisplayName") or raw.get("name") or name,
    )


def _status(raw: dict) -> Status:
    kind = raw.get("type") or {}
    return Status(
        state=kind.get("state") or "pre",
        name=kind.get("name") or "",
        period=_int(raw.get("period")),
        clock=str(raw.get("displayClock") or ""),
        completed=bool(kind.get("completed")),
    )


def _sides(competitors: list[dict]) -> tuple[dict, dict]:
    by_side = {c.get("homeAway"): c for c in competitors}
    if "home" not in by_side or "away" not in by_side:
        raise ApiError("game without a home and an away team")
    return by_side["home"], by_side["away"]


def _linescores(home: dict, away: dict) -> list[tuple[int, int]]:
    def points(side: dict) -> list[int]:
        return [_int(p.get("value", p.get("displayValue"))) for p in side.get("linescores") or []]

    return list(zip(points(home), points(away), strict=False))


def parse_teams(payload: Any) -> list[Team]:
    try:
        entries = payload["sports"][0]["leagues"][0]["teams"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError("unexpected teams payload") from exc
    return [_team(e["team"]) for e in entries if isinstance(e, dict) and isinstance(e.get("team"), dict)]


def parse_scoreboard(payload: Any, day: date) -> list[Game]:
    """Games of the US game day ``day`` (the date the scoreboard was asked for).

    A malformed event (e.g. a placeholder without both teams) is skipped, not fatal for the night.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ApiError("unexpected scoreboard payload")
    games = []
    for event in payload["events"]:
        try:
            games.append(_game(event, day))
        except (ApiError, KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            log.warning("skipping malformed event %s: %r", (event or {}).get("id"), exc)
    return games


def _game(event: dict, day: date) -> Game:
    comp = event["competitions"][0]
    home, away = _sides(comp["competitors"])
    status = _status(event.get("status") or comp.get("status") or {})
    started = status.state != "pre"
    return Game(
        id=str(event["id"]),
        day=day,
        tipoff=datetime.fromisoformat(event["date"].replace("Z", "+00:00")),
        home=_team(home["team"]),
        away=_team(away["team"]),
        status=status,
        home_score=_int(home.get("score")) if started else None,
        away_score=_int(away.get("score")) if started else None,
        quarter_scores=_linescores(home, away),
        venue=(comp.get("venue") or {}).get("fullName"),
        time_valid=comp.get("timeValid", True) is not False,
    )


def _stats(raw: dict) -> dict[str, str]:
    return {s.get("name"): s.get("displayValue") for s in raw.get("statistics") or []}


def _team_line(team: Team, points: int, raw: dict) -> TeamLine:
    stats = _stats(raw)
    fgm, fga = _pair(stats.get("fieldGoalsMade-fieldGoalsAttempted"))
    fg3m, fg3a = _pair(stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
    ftm, fta = _pair(stats.get("freeThrowsMade-freeThrowsAttempted"))
    return TeamLine(
        code=team.code,
        name=team.name,
        points=points,
        fgm=fgm,
        fga=fga,
        fg3m=fg3m,
        fg3a=fg3a,
        ftm=ftm,
        fta=fta,
        rebounds=_int(stats.get("totalRebounds")),
        off_reb=_int(stats.get("offensiveRebounds")),
        def_reb=_int(stats.get("defensiveRebounds")),
        assists=_int(stats.get("assists")),
        steals=_int(stats.get("steals")),
        blocks=_int(stats.get("blocks")),
        turnovers=_int(stats.get("totalTurnovers", stats.get("turnovers"))),
        fouls=_int(stats.get("fouls")),
    )


def _players(raw: dict) -> list[PlayerLine]:
    code = (raw.get("team") or {}).get("abbreviation") or ""
    lines = []
    for group in raw.get("statistics") or []:
        keys = group.get("keys") or []
        for entry in group.get("athletes") or []:
            athlete = entry.get("athlete") or {}
            values = dict(zip(keys, entry.get("stats") or [], strict=False))
            fgm, fga = _pair(values.get("fieldGoalsMade-fieldGoalsAttempted"))
            fg3m, fg3a = _pair(values.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
            ftm, fta = _pair(values.get("freeThrowsMade-freeThrowsAttempted"))
            lines.append(
                PlayerLine(
                    name=athlete.get("shortName") or athlete.get("displayName") or "?",
                    team_code=code,
                    minutes=_int(values.get("minutes")),
                    points=_int(values.get("points")),
                    rebounds=_int(values.get("rebounds")),
                    assists=_int(values.get("assists")),
                    steals=_int(values.get("steals")),
                    blocks=_int(values.get("blocks")),
                    turnovers=_int(values.get("turnovers")),
                    fouls=_int(values.get("fouls")),
                    fgm=fgm,
                    fga=fga,
                    fg3m=fg3m,
                    fg3a=fg3a,
                    ftm=ftm,
                    fta=fta,
                    plus_minus=_optional_int(values.get("plusMinus")),
                    starter=bool(entry.get("starter")),
                    played=not entry.get("didNotPlay") and bool(values),
                )
            )
    return lines


def parse_summary(payload: Any) -> BoxScore | None:
    """Box score from a game summary; ``None`` before tip-off (no stats yet)."""
    try:
        return _summary(payload)
    except ApiError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise ApiError(f"unexpected summary payload: {exc!r}") from exc


def _summary(payload: Any) -> BoxScore | None:
    try:
        comp = payload["header"]["competitions"][0]
        home, away = _sides(comp["competitors"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError("unexpected summary payload") from exc
    status = _status(comp.get("status") or {})
    box = payload.get("boxscore") or {}
    teams = {(t.get("team") or {}).get("abbreviation"): t for t in box.get("teams") or []}
    players = {(p.get("team") or {}).get("abbreviation"): p for p in box.get("players") or []}
    home_team, away_team = _team(home["team"]), _team(away["team"])
    if status.state == "pre" or not players:
        return None
    return BoxScore(
        home=_team_line(home_team, _int(home.get("score")), teams.get(home_team.code, {})),
        away=_team_line(away_team, _int(away.get("score")), teams.get(away_team.code, {})),
        home_players=_players(players.get(home_team.code, {})),
        away_players=_players(players.get(away_team.code, {})),
        quarter_scores=_linescores(home, away),
        live=not status.completed,
    )


class EspnClient:
    """Async ESPN NBA client; use as ``async with EspnClient() as client:``."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, backoff: float = BACKOFF):
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=TIMEOUT,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._backoff = backoff

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET and decode JSON, retrying network errors and 5xx."""
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
            try:
                response = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                last = exc
                continue
            except httpx.HTTPError as exc:  # e.g. a broken encoding or a redirect loop
                raise ApiError(f"GET {path} failed: {exc}") from exc
            if response.status_code >= 500:
                last = ApiError(f"{response.status_code} from {response.url}")
                continue
            if response.status_code >= 400:
                raise ApiError(f"{response.status_code} from {response.url}")
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise ApiError(f"invalid JSON from {response.url}") from exc
        raise ApiError(f"GET {path} failed after {ATTEMPTS} attempts: {last}") from last

    async def scoreboard(self, day: date) -> list[Game]:
        return parse_scoreboard(await self._get("/scoreboard", {"dates": f"{day:%Y%m%d}"}), day)

    async def summary(self, event_id: str) -> BoxScore | None:
        return parse_summary(await self._get("/summary", {"event": event_id}))

    async def teams(self) -> list[Team]:
        return parse_teams(await self._get("/teams"))
