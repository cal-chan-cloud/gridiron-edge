"""
Freshness gate for the daily task.

A fetcher can exit 0 having written nothing - a stale cache, an upstream 404
swallowed by a fallback, an empty ADP payload. If the scheduled task only checks
exit codes it will report green for weeks while the site quietly serves frozen
data. That failure mode has already happened twice on this machine, so success is
defined here as DATA THAT LANDED, not as a process that finished.

Exit 0 only when all of the following hold:
  * the fetch stamped today's date
  * ADP exists for every format and was drawn from a recent window
  * the player pool and 2026 schedule are populated
  * projections actually build and produce sane top-end numbers
"""
from __future__ import annotations

import sys
from datetime import date, datetime

from config import SEASON
from data.sources import log
from models.database import db, get_meta

MAX_ADP_WINDOW_AGE_DAYS = 4


def main() -> int:
    problems, notes = [], []
    with db() as conn:
        stamp = get_meta(conn, "last_fetch")
        today = date.today().isoformat()
        if stamp != today:
            problems.append(f"last_fetch is {stamp!r}, expected {today}")
        else:
            notes.append(f"fetch stamped {stamp}")

        n_players = conn.execute(
            "SELECT COUNT(*) n FROM players WHERE team IS NOT NULL AND team!=''").fetchone()["n"]
        if n_players < 900:
            problems.append(f"only {n_players} players on 2026 rosters (expected 900+)")
        else:
            notes.append(f"{n_players} rostered players")

        n_games = conn.execute(
            "SELECT COUNT(*) n FROM schedule WHERE season=?", (SEASON,)).fetchone()["n"]
        if n_games < 272:
            problems.append(f"{SEASON} schedule has {n_games} games (expected 272)")
        else:
            notes.append(f"{n_games} scheduled games")

        for fmt in ("ppr", "half-ppr", "standard"):
            row = conn.execute(
                "SELECT COUNT(*) n, MAX(window_end) we, MAX(total_drafts) td "
                "FROM adp WHERE fmt=?", (fmt,)).fetchone()
            if not row or row["n"] < 100:
                problems.append(f"ADP[{fmt}] has {row['n'] if row else 0} rows (expected 100+)")
                continue
            we = row["we"]
            try:
                age = (date.today() - datetime.strptime(we, "%Y-%m-%d").date()).days
            except (TypeError, ValueError):
                problems.append(f"ADP[{fmt}] window_end unparseable: {we!r}")
                continue
            if age > MAX_ADP_WINDOW_AGE_DAYS:
                problems.append(f"ADP[{fmt}] window ends {we} ({age}d stale)")
            else:
                notes.append(f"ADP[{fmt}] {row['n']} players / {row['td']} drafts through {we}")

        # A daily ADP snapshot must exist for the movers view to ever work.
        snap = conn.execute(
            "SELECT COUNT(*) n FROM adp_history WHERE date=?", (today,)).fetchone()["n"]
        if snap < 100:
            problems.append(f"only {snap} ADP history rows stamped today")

    # The real test: does the model still build end to end?
    try:
        from models.projection import project
        from models.valuation import compute_values
        from config import DEFAULT_LEAGUE
        with db() as conn:
            rows = compute_values(project(conn, DEFAULT_LEAGUE["scoring"]), DEFAULT_LEAGUE)
        if len(rows) < 700:
            problems.append(f"projections produced only {len(rows)} players")
        else:
            top = max(r["points"] for r in rows)
            if not (180 <= top <= 600):
                problems.append(f"top projection {top:.0f} is outside a believable range")
            else:
                notes.append(f"model builds: {len(rows)} players, top {top:.0f} pts")
    except Exception as exc:  # noqa: BLE001 - any failure here is a failed run
        problems.append(f"projection build failed: {exc}")

    for n in notes:
        log(f"  ok   {n}")
    for p in problems:
        log(f"  FAIL {p}")
    if problems:
        log(f"FRESHNESS CHECK FAILED ({len(problems)} problem(s))")
        return 1
    log("Freshness check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
