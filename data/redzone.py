"""
Red-zone and goal-line usage, from play-by-play.

Why this exists: touchdown regression is the single most valuable input in the
model (backtesting says removing it costs 0.030 of rank correlation, more than
anything else). But the "usage-implied" side of that regression currently uses
TOTAL targets and carries, which treats every touch as equally likely to score.
It is not. Fifteen carries from the two-yard line and fifteen from midfield
imply completely different touchdown expectations, and the difference is exactly
what separates a goal-line back from a between-the-twenties back.

So this extracts, per player-season:
    rz_carries / rz_targets     inside the opponent 20
    gl_carries / gl_targets     inside the opponent 5 (where scores actually happen)
and the team totals to turn them into shares.

Play-by-play is ~19MB gzipped and ~400MB expanded per season, so it is streamed
and decompressed incrementally rather than held in memory, and only the eight
columns that matter are parsed. Results are cached in the database, so the cost
is paid once per season rather than on every model build.

Run:  python -m data.redzone            (fills the current history seasons)
      python -m data.redzone --season 2024
"""
from __future__ import annotations

import csv
import gzip
import io
import sys
from collections import defaultdict

from config import HISTORY_SEASONS, NFLVERSE
from data.sources import f, i, log, norm_team, s, session
from models.database import db, init_db

RZ_LINE = 20   # inside the opponent 20 is the red zone
GL_LINE = 5    # inside the 5 is where touchdowns are actually scored

SCHEMA = """
CREATE TABLE IF NOT EXISTS redzone (
    gsis_id TEXT, season INTEGER,
    rz_carries REAL, rz_targets REAL, gl_carries REAL, gl_targets REAL,
    rz_rush_td REAL, rz_rec_td REAL,
    PRIMARY KEY (gsis_id, season)
);
CREATE TABLE IF NOT EXISTS redzone_team (
    team TEXT, season INTEGER,
    rz_carries REAL, rz_targets REAL, gl_carries REAL, gl_targets REAL,
    PRIMARY KEY (team, season)
);
"""


def _stream_rows(url: str):
    """Yield play-by-play rows without ever holding the whole season in memory."""
    r = session().get(url, timeout=600, stream=True)
    r.raise_for_status()
    # gzip.GzipFile over the raw socket, wrapped for text, keeps peak memory in
    # the low megabytes instead of ~400MB per season.
    with gzip.GzipFile(fileobj=r.raw) as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", errors="replace", newline="")
        for row in csv.DictReader(text):
            yield row


def fetch_season(conn, season: int) -> int:
    """Extract red-zone usage for one season and store it."""
    have = conn.execute("SELECT COUNT(*) n FROM redzone WHERE season=?", (season,)).fetchone()["n"]
    if have:
        log(f"  redzone {season}: {have} players already stored")
        return have

    log(f"  redzone {season}: streaming play-by-play ...")
    player = defaultdict(lambda: {"rzc": 0.0, "rzt": 0.0, "glc": 0.0, "glt": 0.0,
                                  "rztd_r": 0.0, "rztd_p": 0.0})
    team = defaultdict(lambda: {"rzc": 0.0, "rzt": 0.0, "glc": 0.0, "glt": 0.0})
    plays = 0
    url = f"{NFLVERSE}/pbp/play_by_play_{season}.csv.gz"
    for row in _stream_rows(url):
        if row.get("season_type") != "REG":
            continue
        yl = row.get("yardline_100")
        if not yl:
            continue
        try:
            yards_out = float(yl)
        except ValueError:
            continue
        if yards_out > RZ_LINE:
            continue
        tm = norm_team(row.get("posteam"))
        if not tm:
            continue
        plays += 1
        in_gl = yards_out <= GL_LINE
        td = row.get("touchdown") == "1"

        if row.get("rush_attempt") == "1":
            rid = s(row.get("rusher_player_id"))
            team[(tm, season)]["rzc"] += 1
            if in_gl:
                team[(tm, season)]["glc"] += 1
            if rid:
                p = player[(rid, season)]
                p["rzc"] += 1
                if in_gl:
                    p["glc"] += 1
                if td and s(row.get("td_player_id")) == rid:
                    p["rztd_r"] += 1
        elif row.get("pass_attempt") == "1":
            # A target is charged wherever the ball was thrown to, caught or not.
            rid = s(row.get("receiver_player_id"))
            team[(tm, season)]["rzt"] += 1
            if in_gl:
                team[(tm, season)]["glt"] += 1
            if rid:
                p = player[(rid, season)]
                p["rzt"] += 1
                if in_gl:
                    p["glt"] += 1
                if td and s(row.get("td_player_id")) == rid:
                    p["rztd_p"] += 1

    conn.executemany(
        "INSERT OR REPLACE INTO redzone VALUES (?,?,?,?,?,?,?,?)",
        [(pid, sea, v["rzc"], v["rzt"], v["glc"], v["glt"], v["rztd_r"], v["rztd_p"])
         for (pid, sea), v in player.items()])
    conn.executemany(
        "INSERT OR REPLACE INTO redzone_team VALUES (?,?,?,?,?,?)",
        [(tm, sea, v["rzc"], v["rzt"], v["glc"], v["glt"]) for (tm, sea), v in team.items()])
    log(f"  redzone {season}: {plays} red-zone plays, {len(player)} players, {len(team)} teams")
    return len(player)


def ensure(conn, seasons=None) -> None:
    """Make sure the red-zone tables exist and cover the seasons we need."""
    conn.executescript(SCHEMA)
    for season in (seasons or HISTORY_SEASONS):
        try:
            fetch_season(conn, season)
        except Exception as exc:  # noqa: BLE001 - a missing season must not kill a build
            log(f"  ! redzone {season} unavailable: {exc}")


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    seasons = HISTORY_SEASONS
    if "--season" in argv:
        seasons = [int(argv[argv.index("--season") + 1])]
    init_db()
    with db() as conn:
        ensure(conn, seasons)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
