"""Settings loaded from environment variables (see .env.example)."""

import os
from dataclasses import dataclass
from datetime import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    pibutler_url: str
    pibutler_api_key: str
    project_id: str = "nba-notifier"
    reminder_minutes: int = 30
    poll_seconds: int = 30
    timezone: str = "Europe/Athens"  # the daily messages and quiet hours follow this clock
    schedule_time: time = time(17, 0)  # tonight's games (the US game day starting this evening)
    results_time: time = time(11, 0)  # last night's results
    weekly_time: time = time(13, 0)  # Mondays: each followed team's games of the week (ADR-N4)
    quiet_start: time = time(0, 0)  # followers' messages are silent from here...
    quiet_end: time = time(9, 0)  # ...until here
    db_path: str = "/data/nba_notifier.db"
    log_level: str = "INFO"
    callback_url: str = "http://nba-notifier:8081/pibutler"  # empty: no /nba command

    @property
    def callback_port(self) -> int:
        """The port PiButler calls is the one we listen on, so it comes from the URL itself."""
        return urlsplit(self.callback_url).port or 80

    def quiet(self, local: time) -> bool:
        """Whether ``local`` (in ``timezone``) falls in the quiet hours; the window may wrap midnight."""
        start, end = self.quiet_start, self.quiet_end
        return start <= local < end if start <= end else local >= start or local < end


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    missing = [k for k in ("PIBUTLER_URL", "PIBUTLER_API_KEY") if not env.get(k)]
    if missing:
        raise SystemExit(f"Missing required settings: {', '.join(missing)}")
    settings = Settings(
        pibutler_url=env["PIBUTLER_URL"].rstrip("/"),
        pibutler_api_key=env["PIBUTLER_API_KEY"],
        project_id=env.get("PIBUTLER_PROJECT_ID", "nba-notifier"),
        reminder_minutes=int(env.get("REMINDER_MINUTES", "30")),
        poll_seconds=int(env.get("POLL_SECONDS", "30")),
        timezone=env.get("TIMEZONE", "Europe/Athens"),
        schedule_time=time.fromisoformat(env.get("SCHEDULE_TIME", "17:00")),
        results_time=time.fromisoformat(env.get("RESULTS_TIME", "11:00")),
        weekly_time=time.fromisoformat(env.get("WEEKLY_TIME", "13:00")),
        quiet_start=time.fromisoformat(env.get("QUIET_START", "00:00")),
        quiet_end=time.fromisoformat(env.get("QUIET_END", "09:00")),
        db_path=env.get("DB_PATH", "/data/nba_notifier.db"),
        log_level=env.get("LOG_LEVEL", "INFO"),
        callback_url=env.get("CALLBACK_URL", "http://nba-notifier:8081/pibutler"),
    )
    ZoneInfo(settings.timezone)  # fail fast on a typo
    return settings
