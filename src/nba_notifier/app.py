"""Entry point: wire config, storage and clients, then run the notifier and its command server."""

import asyncio
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from nba_notifier.api import EspnClient
from nba_notifier.butler import ButlerClient
from nba_notifier.commands import Scoreboard, callback_path, create_app
from nba_notifier.config import Settings, load_settings
from nba_notifier.store import EventStore
from nba_notifier.tracker import Notifier

log = logging.getLogger("nba_notifier")


async def run() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    events = await EventStore.open(settings.db_path)
    async with (
        EspnClient() as espn,
        ButlerClient(settings.pibutler_url, settings.pibutler_api_key, settings.project_id) as butler,
    ):
        # Docker's healthcheck looks at this file's age (see Dockerfile).
        heartbeat = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat"))
        notifier = Notifier(settings, espn, butler, events, on_tick=heartbeat.touch)
        runner: web.AppRunner | None = None
        try:
            if settings.callback_url:
                path = callback_path(settings.callback_url)
                runner = web.AppRunner(create_app(Scoreboard(notifier), settings.pibutler_api_key, path))
                await runner.setup()
                # Bound inside the container only: compose doesn't publish it, PiButler's network reaches it.
                await web.TCPSite(runner, "0.0.0.0", settings.callback_port).start()
            await _serve(notifier, settings)
        finally:
            if runner is not None:
                await runner.cleanup()
            for task in asyncio.all_tasks() - {asyncio.current_task()}:
                task.cancel()
            await events.close()


async def _serve(notifier: Notifier, settings: Settings) -> None:
    """Run the notifier until SIGINT/SIGTERM."""
    main = asyncio.create_task(notifier.run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main.cancel)
    log.info(
        "started: schedule %s, results %s, quiet %s-%s (%s), commands %s",
        f"{settings.schedule_time:%H:%M}",
        f"{settings.results_time:%H:%M}",
        f"{settings.quiet_start:%H:%M}",
        f"{settings.quiet_end:%H:%M}",
        settings.timezone,
        f"on port {settings.callback_port}" if settings.callback_url else "off",
    )
    try:
        await main
    except asyncio.CancelledError:
        log.info("stopping")


def main() -> None:
    asyncio.run(run())
