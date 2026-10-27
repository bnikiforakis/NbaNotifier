"""Message formatting: visitors first everywhere, phone-width tables, NBA stats."""

import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path

from nba_notifier import reports
from nba_notifier.api import parse_summary
from nba_notifier.models import Game, Team
from nba_notifier.reports import DASH

FIXTURES = Path(__file__).parent / "fixtures"
HOU = Team("HOU", "Houston Rockets", "Rockets")
DAL = Team("DAL", "Dallas Mavericks", "Mavericks")
TIPOFF = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
GAME = Game(id="401898395", day=date(2026, 10, 9), tipoff=TIPOFF, home=DAL, away=HOU, venue="Venetian Arena")
HALF = parse_summary(json.loads((FIXTURES / "summary_401898395_halftime.json").read_text()))
FINAL = replace(HALF, live=False, quarter_scores=[(19, 27), (26, 38), (30, 20), (25, 25)])
FINAL = replace(FINAL, home=replace(FINAL.home, points=100), away=replace(FINAL.away, points=110))


def table_rows(text: str) -> list[str]:
    return [row for block in re.findall(r"<code[^>]*>(.*?)</code>", text, re.S) for row in block.split("\n")]


def test_iso_z():
    assert reports.iso_z(TIPOFF) == "2026-10-09T12:00:00Z"


def test_game_url():
    assert reports.game_url(GAME) == "https://www.espn.com/nba/game/_/gameId/401898395"


def test_ot_suffix():
    assert reports.ot_suffix(4) == ""
    assert reports.ot_suffix(5) == " (OT)"
    assert reports.ot_suffix(7) == " (3OT)"


def test_scores_list_the_visitors_first():
    assert reports.plain_score(GAME, 45, 65) == f"Rockets 65{DASH}45 Mavericks"
    assert reports.score_line(GAME, 45, 65) == f"Rockets 65{DASH}45 Mavericks"
    assert reports.score_line(GAME, 45, 65, bold_winner=True) == f"<b>Rockets</b> 65{DASH}45 Mavericks"
    assert reports.score_line(GAME, 70, 65, bold_winner=True) == f"Rockets 65{DASH}70 <b>Mavericks</b>"
    assert reports.score_line(GAME, 1, 1, bold_both=True) == f"<b>Rockets</b> 1{DASH}1 <b>Mavericks</b>"
    assert reports.matchup(GAME) == "<b>Rockets</b> @ <b>Mavericks</b>"
    assert reports.title(GAME) == "Rockets @ Mavericks"
    assert reports.periods_line([(19, 27), (26, 38)]) == f"Q1 27{DASH}19 · Q2 38{DASH}26"


def test_period_names_continue_into_overtime():
    line = reports.periods_line([(1, 2)] * 6)
    assert line.split(" · ")[4].startswith("OT1") and line.split(" · ")[5].startswith("OT2")


def test_quarter_card_and_report():
    card = reports.quarter_card(GAME, HALF, 2, (45, 65))
    assert card.startswith(f"🏀 <b>End of Q2</b> · Rockets 65{DASH}45 Mavericks")
    assert card.endswith(f"Q1 27{DASH}19 · Q2 38{DASH}26")
    report = reports.quarter_report(GAME, HALF, 2, (45, 65))
    assert "🔥 <b>Top scorers</b>" in report
    rows = table_rows(report)
    assert rows[0].index("Rockets") < rows[0].index("Mavericks")
    assert any(r.startswith("Q1") and r.split()[1:] == ["27", "19"] for r in rows)
    assert any(r.startswith("T ") and r.split()[1:] == ["65", "45"] for r in rows)
    assert any(r.startswith("FG ") and r.split()[1] == "25/47" for r in rows)
    assert all(len(r) <= 31 for r in rows)


def test_quarter_card_without_score_sums_the_periods():
    assert f"Rockets 27{DASH}19 Mavericks" in reports.quarter_card(GAME, HALF, 1)


def test_final_report():
    text = reports.final_report(GAME, FINAL)
    assert text.startswith(f"🏁 <b>FINAL</b> · <b>Rockets</b> 110{DASH}100 Mavericks")
    rows = table_rows(text)
    labels = [r.split()[0] for r in rows if r.strip()]
    assert "BLK" in labels and "STL" in labels and "PIR" not in text
    assert all(len(r) <= 31 for r in rows)
    assert text.index("⭐ <b>Rockets</b>") < text.index("⭐ <b>Mavericks</b>")
    assert re.search(r"• [^:]+: \d+ PTS, \d+ REB, \d+ AST", text)


def test_final_report_in_overtime():
    ot = replace(FINAL, quarter_scores=[*FINAL.quarter_scores, (5, 3)])
    assert reports.final_report(GAME, ot).startswith("🏁 <b>FINAL</b> (OT)")
    assert reports.final_score(GAME, ot).startswith("🏁 <b>Final</b> (OT) · <b>Rockets</b>")


def test_final_card():
    card = reports.final_card(GAME, FINAL)
    head, periods = card.split("\n")
    assert head == f"🏁 <b>Final</b> · <b>Rockets</b> 110{DASH}100 Mavericks"
    assert periods.startswith(f"Q1 27{DASH}19")


def test_top_scorers_show_team_names():
    text = reports.quarter_report(GAME, HALF, 2, (45, 65))
    assert re.search(r"1\. .+ \((Rockets|Mavericks)\) <b>\d+</b>", text)


def test_reminder_and_tipoff():
    text = reports.reminder(GAME, 30)
    assert text == (
        "⏰ Tip-off in 30 min: <b>Rockets</b> @ <b>Mavericks</b>"
        " · {{hm:2026-10-09T12:00:00Z}} · Venetian Arena"
    )
    assert reports.tipoff(GAME) == "🏀 <b>Tip-off!</b> <b>Rockets</b> @ <b>Mavericks</b>"


def test_schedule_labels():
    assert reports.schedule_label(GAME, False) == "☆ {{hm:2026-10-09T12:00:00Z}} Rockets @ Mavericks"
    compact = reports.schedule_label(GAME, True, compact=True)
    assert compact == "⭐ {{hm:2026-10-09T12:00:00Z}} HOU @ DAL"
    assert reports.schedule_label(replace(GAME, time_valid=False), False) == "☆ TBD Rockets @ Mavericks"


def test_daily_schedule():
    quiet = reports.quiet_note(time(0, 0), time(9, 0))
    text = reports.daily_schedule(date(2026, 10, 21), time(11, 0), quiet)
    assert text.startswith("📅 <b>Tonight's NBA games</b> · Wed 21 Oct (US)")
    assert text.endswith("Between 00:00 and 09:00 they arrive silently. All results arrive at 11:00.")
    assert "  " not in text
    assert "All results" not in reports.daily_schedule(date(2026, 10, 21))


def test_daily_results():
    text = reports.daily_results(date(2026, 10, 21))
    assert text.startswith("🏁 <b>NBA results</b> · night of Wed 21 Oct (US)")
    assert "⚠️" not in text
    assert "⚠️ 1 result not available yet: see /nba." in reports.daily_results(date(2026, 10, 21), 1)
    assert "2 results not" in reports.daily_results(date(2026, 10, 21), 2)


def test_result_labels():
    assert reports.result_label(GAME, FINAL) == f"🏆 Rockets 110{DASH}100 Mavericks"
    ot = replace(FINAL, quarter_scores=[*FINAL.quarter_scores, (5, 3)])
    assert reports.result_label(GAME, ot, compact=True) == f"🏆 HOU 110{DASH}100 DAL (OT)"


def test_trophy_goes_on_the_winners_side():
    assert reports.result_score(GAME, 112, 108) == f"Rockets 108{DASH}112 Mavericks 🏆"  # home wins
    assert reports.result_score(GAME, 108, 112, compact=True) == f"🏆 HOU 112{DASH}108 DAL"
    assert reports.result_score(GAME, 90, 90) == f"Rockets 90{DASH}90 Mavericks"


def test_names_are_escaped():
    odd = replace(GAME, away=Team("X", "A&B", "A&B"))
    assert "A&amp;B" in reports.matchup(odd)
    assert "A&amp;B" in reports.final_report(odd, FINAL)
