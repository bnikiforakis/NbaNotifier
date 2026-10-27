"""PiButler command callback: ``/nba`` as one message navigated in place (list → game → stats).

PiButler POSTs ``{"type": "command" | "action", ...}`` with the project's API key and shows the
``{"text", "buttons"}`` reply. Every view is rebuilt from the live scoreboard of the current night
plus a game's summary, through a short TTL cache: PiButler gives up after 8 s, and many users (or
one impatient one) must not multiply the requests to ESPN.

Action data stays within ``ACTION_RE``: ``l`` (list), ``g|<id>`` (game), ``s|<id>`` (stats), where
``<id>`` is ESPN's event id (digits).
"""

import asyncio
import hmac
import logging
import re
from dataclasses import dataclass, field
from html import escape
from urllib.parse import urlsplit

from aiohttp import web

from nba_notifier import reports
from nba_notifier.api import ApiError
from nba_notifier.cache import FETCH_TIMEOUT, TtlCache
from nba_notifier.models import BoxScore, Game, Status, period_label
from nba_notifier.tracker import MAX_ROWS, Notifier, button_rows, game_night

log = logging.getLogger(__name__)

ACTION_RE = re.compile(r"^[A-Za-z0-9_.|=-]{1,32}$")
CACHE_TTL = 15.0
MAX_TEXT = 4000
MAX_LABEL = 64
UNAVAILABLE = "⚠️ Live data unavailable right now."
TOO_LONG = "⚠️ That's too much to show in one message."
BACK = {"label": "⬅️ Back", "action": "l"}


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: list[list[dict]] = field(default_factory=list)

    def as_dict(self) -> dict:
        # Never cut HTML mid-tag: an over-long message is replaced, not truncated.
        text = self.text if len(self.text) <= MAX_TEXT else TOO_LONG
        return {"text": text, "buttons": self.buttons[:MAX_ROWS]}


def _fit(prefix: str, rest: str) -> str:
    """``prefix + rest`` cut to ``MAX_LABEL`` characters, shortening ``rest`` (never a placeholder)."""
    room = MAX_LABEL - len(prefix)
    return prefix + (rest if len(rest) <= room else rest[: room - 1] + "…")


def clock(status: Status) -> str:
    """``Q3 5:12``, ``End of Q1``, ``Halftime`` or ``OT1 0:45``."""
    if status.name == "STATUS_HALFTIME":
        return "Halftime"
    if status.at_break:
        return f"End of {period_label(status.period)}"
    return " ".join(filter(None, (period_label(status.period) if status.period else "", status.clock)))


def _code_score(game: Game) -> str:
    """``BOS 54-60 LAL``: visitors first, with team codes so two fit on a row."""
    away, home = game.away_score or 0, game.home_score or 0
    return f"{game.away.code} {away}{reports.DASH}{home} {game.home.code}"


class Scoreboard:
    """Builds the ``/nba`` views from the live scoreboard of the current night."""

    def __init__(self, notifier: Notifier, cache_ttl: float = CACHE_TTL):
        self.notifier = notifier
        self.espn = notifier.espn
        self.cache = TtlCache(cache_ttl)

    # --- entry point -------------------------------------------------------------------------

    async def reply(self, body: dict) -> Reply:
        kind = body.get("type")
        try:
            if kind == "command" and body.get("command") == "nba":
                return await self.list_view()
            if kind == "action":
                return await self.action(str(body.get("data") or ""))
        except Exception:
            log.exception("nba view failed")
            return Reply(UNAVAILABLE, [[BACK]])
        return Reply("🤷 Unknown command. Try /nba for tonight's games.")

    async def action(self, data: str) -> Reply:
        view, _, ref = data.partition("|")
        if data == "l":
            return await self.list_view()
        if view not in ("g", "s") or not ref.isdigit():
            return Reply("🤷 That button has expired. Try /nba again.")
        if view == "g":
            game = await self._find(ref)
            return await self.game_view(game) if game else _gone()
        # Board and box score together, so one tap stays well inside PiButler's 8 s.
        game, box = await asyncio.gather(self._find(ref), self._summary(ref))
        return await self.stats_view(game, box) if game else _gone()

    # --- views -------------------------------------------------------------------------------

    async def list_view(self) -> Reply:
        night = game_night(self.notifier.now())
        try:
            games = sorted(await self._board(night), key=lambda g: (g.tipoff, g.id))
        except (ApiError, TimeoutError) as exc:
            log.warning("scoreboard unavailable: %s", exc)
            return Reply(f"🏀 <b>NBA tonight</b>\n\n{UNAVAILABLE}")
        head = f"🏀 <b>NBA</b> · night of {night:%a} {night.day} {night:%b} (US)"
        if not games:
            return Reply(f"{head}\n\nNo games tonight.")
        rows = button_rows(
            [{"label": self._label(g, False), "action": f"g|{g.id}"} for g in games],
            [{"label": self._label(g, True), "action": f"g|{g.id}"} for g in games],
        )
        return Reply(f"{head}\n\nTap a game for the live score.", rows)

    async def game_view(self, game: Game) -> Reply:
        refresh = {"label": "🔄 Refresh", "action": f"g|{game.id}"}
        stats = {"label": "📊 Stats", "action": f"s|{game.id}"}
        status = game.status
        if status.state == "pre":
            when = f"{{{{time:{reports.iso_z(game.tipoff)}}}}}" if game.time_valid else "time TBD"
            lines = [f"⏳ {reports.matchup(game)}", f"Tip-off {when}"]
            if game.venue:
                lines.append(f"📍 {escape(game.venue)}")
            return Reply("\n".join(lines), [[refresh], [BACK]])
        if status.cancelled:
            return Reply(f"🚫 {reports.matchup(game)}\n\nNot played ({_status_text(status)}).", [[BACK]])
        home, away = game.score or (0, 0)
        if status.live:
            lines = [f"🔴 {reports.score_line(game, home, away, bold_both=True)}", clock(status)]
        else:
            score = reports.score_line(game, home, away, bold_winner=True)
            lines = [f"🏁 <b>Final</b>{reports.ot_suffix(len(game.quarter_scores))} · {score}"]
        if game.quarter_scores:
            lines += ["", reports.periods_line(game.quarter_scores)]
        return Reply("\n".join(lines), [[stats, refresh], [BACK]])

    async def stats_view(self, game: Game, box: BoxScore | None) -> Reply:
        buttons = [
            [{"label": "🔄 Refresh", "action": f"s|{game.id}"}, {"label": "⬅️ Game", "action": f"g|{game.id}"}]
        ]
        if game.status.state == "pre" or game.status.cancelled:
            return Reply(f"📊 {reports.matchup(game)}\n\nNo stats: the game hasn't started.", buttons)
        if box is None:
            return Reply(f"📊 {reports.matchup(game)}\n\n{UNAVAILABLE}", buttons)
        home, away = game.score or (box.home.points, box.away.points)
        score = escape(reports.plain_score(game, home, away))
        if game.status.live:
            head = f"📊 <b>Live stats</b> · {clock(game.status)} · {score}"
            sections = reports.box_sections(game, box, box.quarter_scores, reports.QUARTER_STATS)
        else:
            head = f"📊 <b>Final</b>{reports.ot_suffix(len(box.quarter_scores))} · {score}"
            sections = reports.box_sections(game, box, box.quarter_scores, reports.FINAL_STATS)
        return Reply("\n".join([head, "", *sections]), buttons)

    # --- helpers -----------------------------------------------------------------------------

    def _label(self, game: Game, compact: bool) -> str:
        status = game.status
        names = f"{game.away.code} @ {game.home.code}" if compact else reports.title(game)
        if status.live:
            score = _code_score(game) if compact else reports.plain_score(game, *game.score)
            return _fit("🔴 ", score if compact else f"{score} · {clock(status)}")
        if status.completed:
            return _fit("", reports.result_score(game, *game.score, compact))
        if status.cancelled:
            return _fit("🚫 ", names)
        when = f"{{{{hm:{reports.iso_z(game.tipoff)}}}}} " if game.time_valid else "TBD "
        return _fit("⏳ " + when, names)

    async def _board(self, night) -> list[Game]:
        """The tracker's shared scoreboard cache (no second cache layer), bounded for PiButler."""
        return await asyncio.wait_for(self.notifier.scoreboard(night), FETCH_TIMEOUT)

    async def _summary(self, event_id: str) -> BoxScore | None:
        try:
            return await self.cache.get(("summary", event_id), lambda: self.espn.summary(event_id))
        except (ApiError, TimeoutError) as exc:
            log.warning("%s: summary unavailable: %s", event_id, exc)
            return None

    async def _find(self, event_id: str) -> Game | None:
        """The live version of a game, from the scoreboard of the day the schedule puts it on
        (tonight's if the schedule doesn't know it). One board fetch per tap."""
        known = next((g for g in self.notifier.games() if g.id == event_id), None)
        night = known.day if known else game_night(self.notifier.now())
        return next((g for g in await self._board(night) if g.id == event_id), None)


def _gone() -> Reply:
    return Reply("That game isn't on the scoreboard any more.", [[BACK]])


def _status_text(status: Status) -> str:
    return status.name.removeprefix("STATUS_").replace("_", " ").lower()


def create_app(scoreboard: Scoreboard, api_key: str, path: str = "/pibutler") -> web.Application:
    """aiohttp app answering PiButler's command/action callbacks on ``POST <path>``."""
    expected = f"Bearer {api_key}".encode()

    async def handle(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "expected a JSON object"}, status=400)
        reply = await scoreboard.reply(body)
        return web.json_response(reply.as_dict())

    app = web.Application()
    app.router.add_post(path, handle)
    return app


def callback_path(url: str) -> str:
    """The path part of ``CALLBACK_URL`` that the server listens on."""
    return urlsplit(url).path or "/"
