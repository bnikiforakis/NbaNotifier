"""Schedule sync, per-game tracking and notification decisions.

Same model as EuroleagueNotifier (its ADR-014/016/017), on NBA time:
- a "night" is one US game day (Eastern date), which is what ESPN's scoreboard groups by;
- everyone, silently: tonight's schedule at SCHEDULE_TIME (one Follow button per game) and last
  night's results at RESULTS_TIME (one 📊 button per game). Nothing else about unfollowed games;
- a team's followers, silently: its games of the week, Mondays at WEEKLY_TIME (``weekly``);
- followers of a game (pressed Follow, or auto-follow by favourite team): a reminder before
  tip-off, the tip-off, a score card after every quarter and the final card. These ring, except
  during the quiet hours (most NBA games are played at night in Europe).

Idle cost is near zero: the three game days around today are refreshed every couple of hours, and
each game gets a task that sleeps until its reminder / tip-off, polls while live, then exits. One
scoreboard request covers every game of a night, so the game tasks share it through a short cache.
"""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from nba_notifier import reports, weekly
from nba_notifier.api import ApiError, EspnClient
from nba_notifier.butler import ButlerClient, ButlerError
from nba_notifier.cache import TtlCache
from nba_notifier.config import Settings
from nba_notifier.models import REGULATION_PERIODS, BoxScore, Game, Team
from nba_notifier.store import EventStore

log = logging.getLogger(__name__)

US_EASTERN = ZoneInfo("America/New_York")
KINDS = [
    {"value": "schedule", "label": "Tonight's schedule"},
    {"value": "results", "label": "Last night's results"},
    {"value": "weekly", "label": "Weekly team schedule"},
]
COMMANDS = [{"command": "nba", "description": "Live NBA scores"}]
RESULTS_WINDOW = timedelta(hours=6)  # after RESULTS_TIME; later than that the summary is stale
RESULTS_PATIENCE = timedelta(hours=2)  # how long the summary waits for an unfinished or missing game
SCHEDULE_REFRESH = timedelta(hours=2)
LOOP_INTERVAL = timedelta(minutes=10)
LOOKAHEAD = timedelta(hours=26)  # start tracking (sleeping) tasks this far ahead
MAX_GAME_LENGTH = timedelta(hours=4)  # stop polling a game this long after tip-off
LIVE_LEAD = timedelta(minutes=2)  # start polling shortly before tip-off
QUARTER_TTL = timedelta(minutes=30)  # undelivered quarter reports go stale
REMINDER_RETRY = timedelta(minutes=1)
MAX_NAP = timedelta(minutes=1)  # longest single sleep while waiting for a time
CATCH_UP_LIMIT = timedelta(minutes=3)  # stop waiting for the box score to match the scoreboard
BOARD_TTL = 10.0  # seconds: game tasks polling the same night share one scoreboard request
NIGHT_CUTOFF = timedelta(hours=6)  # a night's games belong to it until 06:00 Eastern the next morning
MAX_ROWS = 12  # PiButler's button-row limit; more games than that go two per row

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def us_day(when: datetime) -> date:
    """The US (Eastern) calendar date at ``when``."""
    return when.astimezone(US_EASTERN).date()


def game_night(when: datetime) -> date:
    """The game day "in progress" at ``when``: the late games of a night end in the US early morning."""
    return us_day(when - NIGHT_CUTOFF)


def button_rows(full: list[dict], compact: list[dict]) -> list[list[dict]]:
    """One button per row, or two compact ones per row when there are more than ``MAX_ROWS``."""
    if len(full) <= MAX_ROWS:
        return [[b] for b in full]
    return [compact[i : i + 2] for i in range(0, len(compact), 2)]


def build_manifest(teams: list[Team], callback_url: str | None = None) -> dict:
    """Project manifest; with a ``callback_url`` PiButler also routes the ``COMMANDS`` to it."""
    unique = sorted({t.code: t for t in teams}.values(), key=lambda t: t.short_name)
    manifest = {
        "name": "NBA",
        "settings": [
            {
                "key": "teams",
                "label": "Auto-follow teams",
                "default": [],  # not "All": auto-following everything would follow every game
                "options": [{"value": t.code, "label": t.short_name} for t in unique],
            },
            {
                "key": "daily",
                "label": "Notifications",
                "options": KINDS,
                "default": [k["value"] for k in KINDS],
            },
        ],
    }
    if callback_url:
        manifest |= {"commands": COMMANDS, "callback_url": callback_url}
    return manifest


def topic(game: Game, quiet: str = "") -> dict:
    name = reports.title(game)
    what = (
        "You'll get a reminder before tip-off, a score card after every quarter and the final result, "
        "each with 📊 for the full stats."
    )
    return {
        "id": game.identifier,
        "title": name,
        "auto_follow": {"teams": [game.home.code, game.away.code]},
        "follow_text": (
            f"⭐ You're following <b>{escape(name)}</b> ({{{{hm:{reports.iso_z(game.tipoff)}}}}}).\n"
            + " ".join(part for part in (what, quiet) if part)
        ),
        "unfollow_text": f"Unfollowed {name}",
    }


def follow_button(game: Game) -> dict:
    return {
        "label": "☆ Follow this game",
        "label_active": "⭐ Following · tap to unfollow",
        "follow": game.identifier,
    }


def stats_button(full_report: str) -> dict:
    """Reveals the full tables on demand, keeping the notification itself compact."""
    return {"label": "📊 Stats", "reveal": full_report}


def game_button(game: Game) -> dict:
    return {"label": "🔗 Game page", "url": reports.game_url(game)}


def followers(game: Game) -> dict:
    return {"topic": game.identifier, "following": True}


class Notifier:
    def __init__(
        self,
        settings: Settings,
        espn: EspnClient,
        butler: ButlerClient,
        events: EventStore,
        clock: Clock = _utcnow,
        sleep: Sleep = asyncio.sleep,
        on_tick: Callable[[], None] | None = None,
    ):
        self.settings = settings
        self.espn = espn
        self.butler = butler
        self.events = events
        self.now = clock
        self.sleep = sleep
        self.on_tick = on_tick
        self.tz = ZoneInfo(settings.timezone)
        self.boards = TtlCache(BOARD_TTL, timeout=None, clock=lambda: self.now().timestamp())
        self._tasks: dict[str, asyncio.Task] = {}
        self._tracked: dict[str, Game] = {}  # the version of each game its task was started with
        self._games: dict[str, Game] = {}
        self._schedule_at: datetime | None = None
        self._lagging: dict[str, datetime] = {}  # message key -> when its box score was first behind
        self._result_boxes: dict[str, dict[str, BoxScore]] = {}  # results key -> finished box scores

    # --- main loop ---------------------------------------------------------------------------

    async def run(self) -> None:
        await self.register()
        while True:
            try:
                await self.tick()
                self._beat()
            except Exception:
                log.exception("main loop error")
            await self.sleep(LOOP_INTERVAL.total_seconds())

    def games(self) -> list[Game]:
        """The loaded schedule (read-only snapshot)."""
        return list(self._games.values())

    def _beat(self) -> None:
        if self.on_tick:
            self.on_tick()

    async def register(self) -> None:
        """Register the manifest, retrying until both ESPN and PiButler are reachable."""
        delay = 5.0
        while True:
            self._beat()  # alive while waiting, not only once ticking
            try:
                teams = await self.espn.teams()
                await self.butler.register(build_manifest(teams, self.settings.callback_url))
                log.info("registered manifest with %d teams", len(teams))
                return
            except (ApiError, ButlerError) as exc:
                log.warning("registration failed (%s), retrying in %.0fs", exc, delay)
                await self.sleep(delay)
                delay = min(delay * 2, 300)

    async def tick(self) -> None:
        """Refresh the schedule, send the daily messages and (re)start game tasks.

        A failing refresh or daily message doesn't stop tracking the games already known; only
        before any schedule has loaded does the error propagate.
        """
        now = self.now()
        if self._schedule_at is None or now - self._schedule_at >= SCHEDULE_REFRESH:
            try:
                await self.refresh_schedule()
                self._schedule_at = now
            except Exception:
                if self._schedule_at is None:
                    raise
                log.exception("schedule refresh failed, keeping the previous one")
        try:
            await self.maybe_send_schedule()
        except Exception:
            log.exception("tonight's schedule failed")
        try:
            await self.maybe_send_results()
        except Exception:
            log.exception("last night's results failed")
        for gid, game in self._games.items():
            running = gid in self._tasks and not self._tasks[gid].done()
            if running and self._tracked[gid].tipoff != game.tipoff and not await self._started(gid):
                log.info(
                    "%s: tip-off moved %s → %s, restarting",
                    gid,
                    reports.iso_z(self._tracked[gid].tipoff),
                    reports.iso_z(game.tipoff),
                )
                self._tasks[gid].cancel()
                running = False
            if not running and self._should_track(game, now) and not await self.final_sent(gid):
                log.info(
                    "%s: tracking %s @ %s (tip-off %s)",
                    gid,
                    game.away.code,
                    game.home.code,
                    reports.iso_z(game.tipoff),
                )
                self._tasks[gid] = asyncio.create_task(self.track(game), name=gid)
                self._tracked[gid] = game
        try:  # last: a slow ESPN mustn't hold up the game tasks
            await self.maybe_send_weekly()
        except Exception:
            log.exception("weekly schedules failed")

    async def refresh_schedule(self) -> None:
        """Load last night, today and tomorrow (US game days): results, schedule and lookahead."""
        today = us_day(self.now())
        days = [today + timedelta(days=d) for d in (-1, 0, 1)]
        boards = await asyncio.gather(*(self.espn.scoreboard(d) for d in days))
        self._games = {g.identifier: g for board in boards for g in board}
        log.info("schedule refreshed: %d games", len(self._games))

    def _should_track(self, game: Game, now: datetime) -> bool:
        if not game.time_valid or game.status.cancelled:
            return False
        return now - MAX_GAME_LENGTH < game.tipoff < now + LOOKAHEAD

    async def _started(self, gid: str) -> bool:
        """True once anything live has been sent; a schedule change then no longer matters."""
        return await self.events.has(f"{gid}:start") or await self.events.last_period(gid) > 0

    async def final_sent(self, gid: str) -> bool:
        """True once the game's final message has gone out (the game is over)."""
        return await self.events.has(f"{gid}:final")

    def topic(self, game: Game) -> dict:
        return topic(game, reports.quiet_note(self.settings.quiet_start, self.settings.quiet_end))

    def _quiet(self) -> bool:
        """Whether followers' messages go out silently now (the quiet hours, in ``TIMEZONE``)."""
        return self.settings.quiet(self.now().astimezone(self.tz).time())

    async def scoreboard(self, day: date) -> list[Game]:
        """The live scoreboard of a game day, shared by every caller for ``BOARD_TTL`` seconds."""
        return await self.boards.get(day, lambda: self.espn.scoreboard(day))

    # --- tonight's schedule ------------------------------------------------------------------

    async def maybe_send_schedule(self) -> None:
        now = self.now()
        if now.astimezone(self.tz).time() < self.settings.schedule_time:
            return
        night = us_day(now)
        upcoming = sorted(
            (g for g in self._games.values() if g.day == night and g.tipoff > now and not g.status.cancelled),
            key=lambda g: (g.tipoff, g.id),
        )
        if not upcoming:
            return  # no games tonight, or all already started
        key = f"schedule:{night.isoformat()}"
        if await self.events.has(key):
            return

        def button(game: Game, compact: bool) -> dict:
            return {
                "label": reports.schedule_label(game, False, compact),
                "label_active": reports.schedule_label(game, True, compact),
                "follow": game.identifier,
            }

        await self._send(
            key,
            reports.daily_schedule(
                night,
                self.settings.results_time,
                reports.quiet_note(self.settings.quiet_start, self.settings.quiet_end),
            ),
            tags={"daily": ["schedule"]},
            topics=[self.topic(g) for g in upcoming],
            silent=True,
            buttons=button_rows([button(g, False) for g in upcoming], [button(g, True) for g in upcoming]),
        )

    # --- last night's results ----------------------------------------------------------------

    async def maybe_send_results(self) -> None:
        """At RESULTS_TIME, one silent message with every result of last night.

        Waits (up to RESULTS_PATIENCE) for games that are still running or whose box score is
        missing, and gives up once the summary would be stale (RESULTS_WINDOW).
        """
        local_now = self.now().astimezone(self.tz)
        due = datetime.combine(local_now.date(), self.settings.results_time, self.tz)
        if not due <= local_now < due + RESULTS_WINDOW:
            return
        night = us_day(due) - timedelta(days=1)
        key = f"results:{night.isoformat()}"
        played = sorted(
            (g for g in self._games.values() if g.day == night and not g.status.cancelled),
            key=lambda g: (g.tipoff, g.id),
        )
        if not played or await self.events.has(key):
            return
        boxes = self._result_boxes.setdefault(key, {})
        missing = [g for g in played if g.identifier not in boxes]
        fetched = await asyncio.gather(*(self.espn.summary(g.id) for g in missing), return_exceptions=True)
        for game, box in zip(missing, fetched, strict=True):
            over = await self.final_sent(game.identifier)  # the tracker saw the final buzzer
            if isinstance(box, BoxScore) and (not box.live or over):
                boxes[game.identifier] = box  # finished games don't change: fetch once
        pending = [g for g in played if g.identifier not in boxes]
        if pending and local_now < due + RESULTS_PATIENCE:
            log.info("%s: waiting for %s", key, ", ".join(g.identifier for g in pending))
            return
        if not boxes:
            return
        finished = [(g, boxes[g.identifier]) for g in played if g.identifier in boxes]
        reveals = [reports.final_report(g, b) for g, b in finished]

        def buttons(compact: bool) -> list[dict]:
            return [
                {"label": reports.result_label(g, b, compact), "reveal": reveal}
                for (g, b), reveal in zip(finished, reveals, strict=True)
            ]

        await self._send(
            key,
            reports.daily_results(night, missing=len(pending)),
            tags={"daily": ["results"]},
            buttons=button_rows(buttons(False), buttons(True)),
            silent=True,
        )
        self._result_boxes.pop(key, None)

    # --- weekly team schedules (ADR-N4) -------------------------------------------------------

    async def maybe_send_weekly(self) -> None:
        """Mondays from WEEKLY_TIME: one silent message per team with its games of the week, to
        the team's followers. Retried every tick until every team is sent (ESPN down or empty, a
        rejected send); stale after Monday."""
        local_now = self.now().astimezone(self.tz)
        if local_now.weekday() != 0 or local_now.time() < self.settings.weekly_time:
            return
        monday = local_now.date()  # the week's first US game day: Monday's games start this evening
        if await self.events.has(f"weekly:{monday.isoformat()}"):
            return
        boards = await asyncio.gather(*(self.espn.scoreboard(d) for d in weekly.days(monday)))
        weeks = weekly.team_weeks(g for board in boards for g in board)
        if not weeks:
            return  # an empty week is more likely a bad answer than no games at all: ask again
        ends = datetime.combine(monday + timedelta(days=1), time(0), self.tz)
        failed = []
        for code, (team, fixtures) in sorted(weeks.items(), key=lambda item: item[1][0].short_name):
            key = f"weekly:{monday.isoformat()}:{code}"
            if await self.events.has(key):
                continue
            week_topic = weekly.topic(team, monday)
            try:
                await self._send(
                    key,
                    weekly.message(team, fixtures),
                    tags={"daily": ["weekly"]},
                    expires_at=reports.iso_z(ends),
                    topics=[week_topic],
                    audience={"topic": week_topic["id"], "following": True},
                    silent=True,
                )
            except ButlerError as exc:  # the other teams still go out
                failed.append(code)
                log.warning("%s failed: %s", key, exc)
        if failed:
            return
        await self.events.add(f"weekly:{monday.isoformat()}")
        log.info("weekly schedules sent for %d teams", len(weeks))

    # --- per-game tracking -------------------------------------------------------------------

    async def track(self, game: Game) -> None:
        gid = game.identifier
        try:
            await self._reminder(game)
            await self._sleep_until(game.tipoff - LIVE_LEAD)
            log.info("%s: polling live", gid)
            while self.now() < game.tipoff + MAX_GAME_LENGTH:
                try:
                    if await self.poll(game):
                        log.info("%s: done", gid)
                        return
                except (ApiError, ButlerError) as exc:
                    log.warning("%s: poll failed: %s", gid, exc)
                await self.sleep(self.settings.poll_seconds)
            log.warning("%s: gave up, no final after %s", gid, MAX_GAME_LENGTH)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: tracker crashed", gid)

    async def _reminder(self, game: Game) -> None:
        minutes = self.settings.reminder_minutes
        await self._sleep_until(game.tipoff - timedelta(minutes=minutes))
        key = f"{game.identifier}:reminder:{reports.iso_z(game.tipoff)}"  # a rescheduled game gets a new one
        while self.now() < game.tipoff and not await self.events.has(key):
            left = min(minutes, math.ceil((game.tipoff - self.now()).total_seconds() / 60))
            try:
                await self._send(
                    key,
                    reports.reminder(game, left),
                    expires_at=reports.iso_z(game.tipoff),
                    topics=[self.topic(game)],
                    audience=followers(game),
                    buttons=[[follow_button(game)]],
                    silent=self._quiet(),
                )
            except ButlerError as exc:
                log.warning("%s: reminder failed, retrying: %s", game.identifier, exc)
                left = (game.tipoff - self.now()).total_seconds()
                await self.sleep(max(0.0, min(REMINDER_RETRY.total_seconds(), left)))

    async def poll(self, game: Game) -> bool:
        """One live poll. Returns True once the game is over (or called off) and its final is out."""
        gid = game.identifier
        live = next((g for g in await self.scoreboard(game.day) if g.id == game.id), None)
        if live is None:
            log.warning("%s: not on the %s scoreboard any more", gid, game.day)
            return False
        status = live.status
        if status.cancelled:
            log.info("%s: %s, stopping", gid, status.name)
            return True
        if status.completed:
            return await self._finals(live)
        # Tip-off only while the first quarter is on: a tracker (re)started mid-game skips it.
        if status.live and status.ended_periods == 0 and not await self.events.has(f"{gid}:start"):
            await self._send(
                f"{gid}:start",
                reports.tipoff(live),
                expires_at=reports.iso_z(self.now() + QUARTER_TTL),
                topics=[self.topic(live)],
                audience=followers(live),  # followers only
                buttons=[[follow_button(live)]],
                silent=self._quiet(),
            )
        period = status.ended_periods
        score = live.score_after(period)
        # Only the latest ended period: after a restart we don't replay old quarters.
        if period > await self.events.last_period(gid) and _another_period_follows(period, score):
            key = f"{gid}:p{period}"
            box = await self.espn.summary(game.id)
            if box is None or not self._caught_up(key, _box_reached(box, score)):
                return False
            await self._send(
                key,
                reports.quarter_card(live, box, period, score),
                expires_at=reports.iso_z(self.now() + QUARTER_TTL),
                topics=[self.topic(live)],
                audience=followers(live),
                buttons=[
                    [stats_button(reports.quarter_report(live, box, period, score))],
                    [follow_button(live)],
                ],
                silent=self._quiet(),
            )
        return False

    async def _finals(self, game: Game) -> bool:
        gid = game.identifier
        if await self.final_sent(gid):
            return True
        box = await self.espn.summary(game.id)
        if box is None:
            return False
        final = not box.live and (game.score is None or (box.home.points, box.away.points) == game.score)
        if not self._caught_up(f"{gid}:final", final):
            return False
        if game.score and (box.home.points, box.away.points) != game.score:
            # Sent anyway after a lagging box score: the result itself comes from the scoreboard.
            home, away = game.score
            box = replace(box, home=replace(box.home, points=home), away=replace(box.away, points=away))
            box = replace(box, quarter_scores=game.quarter_scores or box.quarter_scores)
        await self._send(
            f"{gid}:final",
            reports.final_card(game, box),
            topics=[self.topic(game)],
            audience=followers(game),
            buttons=[[stats_button(reports.final_report(game, box)), game_button(game)]],
            silent=self._quiet(),
        )
        return True

    def _caught_up(self, key: str, caught_up: bool) -> bool:
        """Whether to send ``key`` now: once its box score has caught up, or after ``CATCH_UP_LIMIT``.

        The scoreboard and the summary are cached independently, so the box score can lag by a poll
        or two; the limit keeps a broken feed from blocking the message forever.
        """
        if caught_up:
            self._lagging.pop(key, None)
            return True
        since = self._lagging.setdefault(key, self.now())
        if self.now() - since < CATCH_UP_LIMIT:
            log.info("%s: box score behind the scoreboard, waiting", key)
            return False
        log.warning("%s: box score still behind after %s, sending anyway", key, CATCH_UP_LIMIT)
        return True

    async def _send(
        self, key: str, text: str, tags: dict | None = None, expires_at: str | None = None, **extra
    ) -> None:
        await self.butler.notify(key, text, tags, expires_at, **extra)
        await self.events.add(key)

    async def _sleep_until(self, when: datetime) -> None:
        # Short naps against the wall clock: asyncio timers don't advance while the host sleeps
        # (e.g. a laptop lid), so one long sleep could wake up far too late.
        while (delay := (when - self.now()).total_seconds()) > 0:
            await self.sleep(min(delay, MAX_NAP.total_seconds()))


def _another_period_follows(ended: int, score: tuple[int, int] | None) -> bool:
    """False when the ended period may be the last one, so the final report covers it instead.

    From Q4 on, a period is followed by overtime only if it ends tied; otherwise the final is
    imminent. An unknown score keeps the report.
    """
    if ended < REGULATION_PERIODS or score is None:
        return True
    home, away = score
    return home == away


def _box_reached(box: BoxScore, score: tuple[int, int] | None) -> bool:
    """Box score totals are at least the scoreboard score (stats are game-to-date)."""
    return score is None or (box.home.points >= score[0] and box.away.points >= score[1])
