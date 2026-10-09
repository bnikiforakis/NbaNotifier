"""Telegram-HTML message builders for schedules, quarter and final reports.

Times are emitted as ``{{time:<ISO Z>}}`` / ``{{hm:<ISO Z>}}`` placeholders that PiButler renders in
each user's timezone. Tables use ``<pre>`` and stay within ~32 characters for phone screens.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from html import escape

from nba_notifier.models import (
    REGULATION_PERIODS,
    BoxScore,
    Game,
    PlayerLine,
    TeamLine,
    pct,
    period_label,
)

DASH = "\N{EN DASH}"


def iso_z(dt: datetime) -> str:
    """UTC ISO timestamp with a ``Z`` suffix, as PiButler placeholders and expiry fields expect."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def game_url(game: Game) -> str:
    """ESPN's game page (the link ESPN's own scoreboard gives for every game)."""
    return f"https://www.espn.com/nba/game/_/gameId/{game.id}"


def _names(game: Game) -> tuple[str, str]:
    return escape(game.home.short_name), escape(game.away.short_name)


def ot_suffix(periods: int) -> str:
    """``" (OT)"``, ``" (2OT)"``, ... for a game of ``periods`` periods; empty in regulation."""
    extra = periods - REGULATION_PERIODS
    if extra <= 0:
        return ""
    return " (OT)" if extra == 1 else f" ({extra}OT)"


def _box(label: str, rows: list[str]) -> str:
    """A monospace table. Telegram shows ``label`` in the block's header (instead of "Copy code")."""
    return f'<pre><code class="language-{label}">' + "\n".join(rows) + "</code></pre>"


def _team_header(game: Game) -> str:
    """Column headers with the teams' names (at most 13 characters, so columns stay apart)."""
    away, home = (escape(t.short_name[:13]) for t in (game.away, game.home))
    return f"{'':<3}{away:>14}{home:>14}"


def _period_table(game: Game, scores: list[tuple[int, int]]) -> str:
    # One row per period (overtimes just add rows), same 31-character layout as the team table.
    rows = [_team_header(game)]
    rows += [f"{period_label(i):<3}{a:>14}{h:>14}" for i, (h, a) in enumerate(scores, 1)]
    rows.append(f"{'T':<3}{sum(a for _, a in scores):>14}{sum(h for h, _ in scores):>14}")
    return _box("Score by quarter", rows)


def shooting(made: int, attempted: int) -> str:
    """``7/9 (77.8%)``: made/attempted and the percentage (``0/0 (-)`` when nothing was tried)."""
    value = pct(made, attempted)
    return f"{made}/{attempted} ({'-' if value is None else f'{value:.1f}%'})"


StatRow = tuple[str, Callable[[TeamLine], str]]

QUARTER_STATS: list[StatRow] = [
    ("FG", lambda t: shooting(t.fgm, t.fga)),
    ("2P", lambda t: shooting(t.fg2m, t.fg2a)),
    ("3P", lambda t: shooting(t.fg3m, t.fg3a)),
    ("FT", lambda t: shooting(t.ftm, t.fta)),
    ("REB", lambda t: str(t.rebounds)),
    ("AST", lambda t: str(t.assists)),
    ("TO", lambda t: str(t.turnovers)),
]
FINAL_STATS: list[StatRow] = [
    *QUARTER_STATS[:6],
    ("STL", lambda t: str(t.steals)),
    ("BLK", lambda t: str(t.blocks)),
    QUARTER_STATS[6],
]


def _team_table(game: Game, box: BoxScore, stats: list[StatRow]) -> str:
    # 3 + 14 + 14 = 31 characters: fits a phone screen with "35/70 (50.0%)" per team.
    rows = [_team_header(game)]
    rows += [f"{label:<3}{fmt(box.away):>14}{fmt(box.home):>14}" for label, fmt in stats]
    return _box("Team stats", rows)


def periods_line(scores: list[tuple[int, int]], name: Callable[[int], str] = period_label) -> str:
    """``Q1 22-18 · Q2 15-17``: away first, like every score here (with en dashes)."""
    return " · ".join(f"{name(i)} {a}{DASH}{h}" for i, (h, a) in enumerate(scores, 1))


def _ranked(players: list[PlayerLine], limit: int = 3) -> list[PlayerLine]:
    return sorted(players, key=lambda p: (-p.points, -p.rebounds - p.assists))[:limit]


def _top_scorers(game: Game, box: BoxScore, limit: int = 3) -> list[str]:
    names = {t.code: t.short_name for t in (game.home, game.away)}
    return [
        f"{i}. {escape(p.name)} ({escape(names.get(p.team_code, p.team_code))}) <b>{p.points}</b>"
        for i, p in enumerate(_ranked(box.players, limit), start=1)
    ]


def _top_performers(players: list[PlayerLine], limit: int = 3) -> list[str]:
    return [
        f"• {escape(p.name)}: {p.points} PTS, {p.rebounds} REB, {p.assists} AST"
        for p in _ranked(players, limit)
    ]


def plain_score(game: Game, home_score: int, away_score: int) -> str:
    """Plain-text score, visitors first, e.g. ``Celtics 108-112 Lakers`` (en dash)."""
    return f"{game.away.short_name} {away_score}{DASH}{home_score} {game.home.short_name}"


def box_sections(game: Game, box: BoxScore, scores: list[tuple[int, int]], stats: list[StatRow]) -> list[str]:
    """Period table, top scorers and team stats, as message lines."""
    return [
        _period_table(game, scores),
        "🔥 <b>Top scorers</b>",
        *_top_scorers(game, box),
        "",
        _team_table(game, box, stats),  # labelled "Team stats" by its box
    ]


def score_line(
    game: Game, home_score: int, away_score: int, bold_winner: bool = False, bold_both: bool = False
) -> str:
    home, away = _names(game)
    if bold_both:
        home, away = f"<b>{home}</b>", f"<b>{away}</b>"
    elif bold_winner and home_score != away_score:
        if home_score > away_score:
            home = f"<b>{home}</b>"
        else:
            away = f"<b>{away}</b>"
    return f"{away} {away_score}{DASH}{home_score} {home}"


def _period_score(
    box: BoxScore, period: int, score: tuple[int, int] | None
) -> tuple[list[tuple[int, int]], int, int]:
    scores = box.quarter_scores[:period]
    home_score, away_score = box.home.points, box.away.points
    if score is not None:
        home_score, away_score = score
    elif len(scores) == period:
        home_score, away_score = sum(h for h, _ in scores), sum(a for _, a in scores)
    return scores, home_score, away_score


def _period_head(game: Game, period: int, home_score: int, away_score: int) -> str:
    return f"🏀 <b>End of {period_label(period)}</b> · {score_line(game, home_score, away_score)}"


def quarter_card(game: Game, box: BoxScore, period: int, score: tuple[int, int] | None = None) -> str:
    """Compact end-of-period message for followers; the tables sit behind a Stats button."""
    scores, home_score, away_score = _period_score(box, period, score)
    head = _period_head(game, period, home_score, away_score)
    return f"{head}\n{periods_line(scores)}" if scores else head


def final_card(game: Game, box: BoxScore) -> str:
    """Compact final for followers: result and score by period; the tables sit behind Stats."""
    head = final_score(game, box)
    return f"{head}\n{periods_line(box.quarter_scores)}" if box.quarter_scores else head


def quarter_report(game: Game, box: BoxScore, period: int, score: tuple[int, int] | None = None) -> str:
    """Report sent when ``period`` (1-based; 5+ are overtimes) has ended.

    The headline is ``score`` (the scoreboard score at the end of the period) when given, else the
    sum of the first ``period`` period scores; team stats are game-to-date.
    """
    scores, home_score, away_score = _period_score(box, period, score)
    lines = [
        _period_head(game, period, home_score, away_score),
        "",
        *box_sections(game, box, scores, QUARTER_STATS),
    ]
    return "\n".join(lines)


def final_report(game: Game, box: BoxScore) -> str:
    """Full-game summary: result, period scores, team stats and top performers by points."""
    score = score_line(game, box.home.points, box.away.points, bold_winner=True)
    home, away = _names(game)
    lines = [
        f"🏁 <b>FINAL</b>{ot_suffix(len(box.quarter_scores))} · {score}",
        "",
        _period_table(game, box.quarter_scores),
        _team_table(game, box, FINAL_STATS),
        f"⭐ <b>{away}</b>",
        *_top_performers(box.away_players),
        "",
        f"⭐ <b>{home}</b>",
        *_top_performers(box.home_players),
    ]
    return "\n".join(lines)


def matchup(game: Game) -> str:
    """``<b>Away</b> @ <b>Home</b>``: the NBA lists the visitors first."""
    home, away = _names(game)
    return f"<b>{away}</b> @ <b>{home}</b>"


def reminder(game: Game, minutes: int) -> str:
    """``minutes`` is the time actually left, which is less than usual when the reminder runs late."""
    parts = [
        f"⏰ Tip-off in {max(minutes, 1)} min: {matchup(game)}",
        f"{{{{hm:{iso_z(game.tipoff)}}}}}",
    ]
    if game.venue:
        parts.append(escape(game.venue))
    return " · ".join(parts)


def title(game: Game) -> str:
    """Plain-text game name for button labels and topic titles (not HTML)."""
    return f"{game.away.short_name} @ {game.home.short_name}"


def tipoff(game: Game) -> str:
    return f"🏀 <b>Tip-off!</b> {matchup(game)}"


def final_score(game: Game, box: BoxScore) -> str:
    """One-line result: ``🏁 Final · Celtics 108-112 Lakers`` (winner in bold)."""
    score = score_line(game, box.home.points, box.away.points, bold_winner=True)
    return f"🏁 <b>Final</b>{ot_suffix(len(box.quarter_scores))} · {score}"


def _night(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def quiet_note(start: time, end: time) -> str:
    """``Between 00:00 and 09:00 they arrive silently.``: the quiet hours, for message texts."""
    return f"Between {start:%H:%M} and {end:%H:%M} they arrive silently."


def daily_schedule(day: date, results_time: time | None = None, quiet: str = "") -> str:
    """Header of tonight's schedule (the US game day ``day``). The games are its Follow buttons."""
    intro = (
        "Tap a game to follow it: a reminder before tip-off and a score card after every quarter, "
        "with 📊 for the full stats."
    )
    results = f"All results arrive at {results_time:%H:%M}." if results_time else ""
    body = " ".join(part for part in (intro, quiet, results) if part)
    return f"📅 <b>Tonight's NBA games</b> · {_night(day)} (US)\n\n{body}"


def schedule_label(game: Game, following: bool, compact: bool = False) -> str:
    """``☆ {{hm}} Celtics @ Lakers``, or ``☆ {{hm}} BOS-LAL`` for two buttons per row."""
    star = "⭐" if following else "☆"
    when = f"{{{{hm:{iso_z(game.tipoff)}}}}}" if game.time_valid else "TBD"
    if compact:
        return f"{star} {when} {game.away.code}{DASH}{game.home.code}"
    return f"{star} {when} {game.away.short_name} @ {game.home.short_name}"


def daily_results(day: date, missing: int = 0) -> str:
    """Header of the results of the US game day ``day``. The games are its 📊 buttons."""
    text = f"🏁 <b>NBA results</b> · night of {_night(day)} (US)\n\nTap a game for its box score."
    if missing:
        text += f"\n\n⚠️ {missing} result{'s' if missing > 1 else ''} not available yet: see /nba."
    return text


def result_label(game: Game, box: BoxScore, compact: bool = False) -> str:
    """Button label such as ``✅ Celtics 108-112 Lakers (OT)``, or ``BOS 108-112 LAL`` compact."""
    ot = ot_suffix(len(box.quarter_scores))
    if compact:
        return f"{game.away.code} {box.away.points}{DASH}{box.home.points} {game.home.code}{ot}"
    return f"✅ {plain_score(game, box.home.points, box.away.points)}{ot}"
