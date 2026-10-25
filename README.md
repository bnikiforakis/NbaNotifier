# NbaNotifier

NBA notifications in Telegram: tonight's games, quarter reports, and full-game reports with stats,
for the games you follow.

It runs 24/7 on a Raspberry Pi and delivers messages through
[PiButler](https://github.com/bnikiforakis/PiButler), an invite-only Telegram bot gateway. It is the
NBA sibling of [EuroleagueNotifier](https://github.com/bnikiforakis/EuroleagueNotifier).

## What you get

- **Tonight's games** at 17:00 (Athens time): one message with a Follow button per game. A "night" is
  one US game day, so games tipping off after midnight in Europe are still listed.
- **Followed games:** a reminder before tip-off, the tip-off, a score card after every quarter, and
  the final, each with 📊 for the full stats. Between 00:00 and 09:00 they arrive silently.
- **Last night's results** at 11:00: one message with a 📊 box score per game.
- **Weekly team schedule** on Mondays at 13:00: one message per auto-followed team with its games
  from Monday to Sunday (US game days).
- **`/nba`**: live scores of tonight's games, on demand.

Data comes from ESPN's public NBA scoreboard and game summaries. One scoreboard request covers a
whole night, so live polling costs one request every 30 seconds whatever the number of games.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

## Run with Docker

```sh
cp .env.example .env   # then fill it in
docker compose up -d --build
docker compose logs -f
```

PiButler must be running first: its compose file creates the shared `pibutler-net` network. Add
`nba-notifier:<key>` to PiButler's `PROJECT_KEYS` and use the same key as `PIBUTLER_API_KEY` here.

## License

[MIT](LICENSE)
