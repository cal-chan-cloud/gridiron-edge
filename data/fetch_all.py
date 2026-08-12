"""
Pull every upstream source into fantasy.db.

Run:  python -m data.fetch_all           (daily task; refreshes volatile sources)
      python -m data.fetch_all --full    (also re-pulls the big historical files)

Sources, all free and keyless:
  * Sleeper       - player master, live injury designation, practice status
  * nflverse      - 2026 rosters + depth charts, 2023-25 player/team production,
                    snap counts, weekly injury reports, advanced rushing, NGS,
                    and the 2026 schedule (bye weeks + posted betting lines)
  * FantasyFootballCalculator - live ADP for PPR / half-PPR / standard
"""
from __future__ import annotations

import sys
from collections import defaultdict

from config import (
    BASE_DIR, FFC_ADP, FFC_FORMAT_FOR_SCORING, HISTORY_SEASONS, INJURY_SEASONS,
    NFLVERSE, SEASON, SLEEPER_PLAYERS,
)
from data.sources import (
    f, fetch_csv, fetch_json, i, log, norm_name, norm_team, s, today,
)
from models.database import bump_version, db, init_db, set_meta

REL_POS = {"QB", "RB", "WR", "TE", "K", "FB", "DEF", "DST"}

# Sources disagree on position labels for kickers and team defenses.
POS_FIX = {"FB": "RB", "PK": "K", "DEF": "DST", "D/ST": "DST", "DEF/ST": "DST"}


def norm_pos(pos) -> str:
    p = s(pos).upper()
    return POS_FIX.get(p, p)


# ---------------------------------------------------------------- Sleeper --
def fetch_sleeper(conn, force=True) -> int:
    """Player master + live injury designations. ~14MB, refreshed daily."""
    log("Sleeper: player master ...")
    raw = fetch_json(SLEEPER_PLAYERS, ttl_hours=8, force=force)
    rows = []
    for pid, p in raw.items():
        pos = norm_pos(p.get("position"))
        if pos not in ("QB", "RB", "WR", "TE", "K", "DST"):
            continue
        if not p.get("active"):
            continue
        team = norm_team(p.get("team"))
        name = s(p.get("full_name")) or f"{s(p.get('first_name'))} {s(p.get('last_name'))}".strip()
        if pos == "DST":
            # Team defenses join on team abbreviation, never on name: every source
            # spells them differently ("LA Rams Defense" / "Los Angeles Rams" / "LAR").
            team = team or norm_team(pid)
            if not team:
                continue
            name = f"{team} Defense"
            search = team
        else:
            if not name:
                continue
            search = norm_name(name)
        meta = p.get("metadata") or {}
        rows.append((
            pid, pid, s(p.get("gsis_id")) or None, None, s(p.get("espn_id")) or None,
            name, search, pos, team,
            f(p.get("age"), 0) or None, s(p.get("birth_date")) or None,
            i(p.get("years_exp"), 0), s(p.get("height")), s(p.get("weight")),
            s(p.get("college")), s(p.get("status")),
            s(p.get("depth_chart_position")), i(p.get("depth_chart_order"), 0) or None,
            s(p.get("injury_status")) or None, s(p.get("injury_body_part")) or None,
            s(p.get("injury_notes")) or None, s(p.get("practice_participation")) or None,
            None, i(meta.get("rookie_year"), 0) or None, None, None, None,
            i(p.get("search_rank"), 999999), 1,
        ))
    conn.execute("DELETE FROM players")
    conn.executemany(
        "INSERT OR REPLACE INTO players (player_id,sleeper_id,gsis_id,pfr_id,espn_id,"
        "name,search_name,position,team,age,birth_date,years_exp,height,weight,college,"
        "status,depth_pos,depth_order,injury_status,injury_body,injury_notes,practice,"
        "headshot,rookie_year,entry_year,draft_number,draft_club,search_rank,active) "
        "VALUES (" + ",".join("?" * 29) + ")", rows)
    log(f"  {len(rows)} active skill players")
    return len(rows)


# --------------------------------------------------------------- nflverse --
def fetch_rosters(conn, force=False) -> int:
    """2026 roster: authoritative team + the sleeper_id <-> gsis_id bridge."""
    log(f"nflverse: roster_{SEASON} ...")
    try:
        rows = fetch_csv(f"{NFLVERSE}/rosters/roster_{SEASON}.csv", ttl_hours=12, force=force)
    except FileNotFoundError:
        log(f"  ! roster_{SEASON} not published yet; skipping")
        return 0

    # Keep the latest week's row per player (rosters are published weekly).
    latest = {}
    for r in rows:
        key = s(r.get("gsis_id")) or s(r.get("sleeper_id"))
        if not key:
            continue
        wk = i(r.get("week"), 0)
        if key not in latest or wk >= latest[key][0]:
            latest[key] = (wk, r)

    n = 0
    for _key, (_wk, r) in latest.items():
        sleeper_id = s(r.get("sleeper_id"))
        gsis = s(r.get("gsis_id"))
        team = norm_team(r.get("team"))
        pos = norm_pos(r.get("position"))
        if pos not in ("QB", "RB", "WR", "TE", "K"):
            continue
        upd = {
            "gsis_id": gsis or None,
            "pfr_id": s(r.get("pfr_id")) or None,
            "team": team,
            "headshot": s(r.get("headshot_url")) or None,
            "entry_year": i(r.get("entry_year"), 0) or None,
            "rookie_year": i(r.get("rookie_year"), 0) or None,
            "draft_number": i(r.get("draft_number"), 0) or None,
            "draft_club": norm_team(r.get("draft_club")) or None,
            "status": s(r.get("status")) or None,
        }
        name = s(r.get("full_name"))
        cur = None
        if sleeper_id:
            cur = conn.execute("SELECT player_id FROM players WHERE sleeper_id=?", (sleeper_id,)).fetchone()
        if cur is None and gsis:
            cur = conn.execute("SELECT player_id FROM players WHERE gsis_id=?", (gsis,)).fetchone()
        if cur is None and name:
            # Last resort: name + position. Without this, any player whose
            # sleeper_id is blank in the nflverse roster gets inserted a SECOND
            # time under his gsis id. That was duplicating ~140 players, and a
            # duplicate is not cosmetic - it inflates the team's share
            # denominator and quietly taxes every real contributor on the roster.
            cur = conn.execute(
                "SELECT player_id FROM players WHERE search_name=? AND position=?",
                (norm_name(name), pos)).fetchone()
        if cur is None:
            # On a 2026 roster but not in Sleeper's active set (late signing, UDFA).
            if not name:
                continue
            pid = gsis or f"nfl-{s(r.get('esb_id')) or name}"
            conn.execute(
                "INSERT OR IGNORE INTO players (player_id,sleeper_id,gsis_id,name,search_name,"
                "position,team,years_exp,active,search_rank) VALUES (?,?,?,?,?,?,?,?,1,999999)",
                (pid, sleeper_id or None, gsis or None, name, norm_name(name), pos, team,
                 i(r.get("years_exp"), 0)))
            target = pid
        else:
            target = cur["player_id"]
        sets = ", ".join(f"{k}=?" for k in upd)
        conn.execute(f"UPDATE players SET {sets} WHERE player_id=?",
                     list(upd.values()) + [target])
        n += 1
    log(f"  {n} roster rows applied")
    return n


def fetch_depth_charts(conn, force=False) -> int:
    """Most recent published depth chart for 2026 (file holds daily snapshots)."""
    log(f"nflverse: depth_charts_{SEASON} ...")
    try:
        rows = fetch_csv(f"{NFLVERSE}/depth_charts/depth_charts_{SEASON}.csv",
                         ttl_hours=12, force=force)
    except FileNotFoundError:
        log("  ! no 2026 depth chart yet; skipping")
        return 0
    if not rows:
        return 0

    newest = max(s(r.get("dt")) for r in rows)
    day = newest[:10]
    snap = [r for r in rows if s(r.get("dt", ""))[:10] == day]
    log(f"  using snapshot {day} ({len(snap)} rows)")

    # gsis -> position, so we can keep offensive skill spots only.
    pos_by_gsis = {r["gsis_id"]: r["position"] for r in conn.execute(
        "SELECT gsis_id, position FROM players WHERE gsis_id IS NOT NULL")}

    best = {}
    for r in snap:
        g = s(r.get("gsis_id"))
        if not g or g not in pos_by_gsis:
            continue
        pos = pos_by_gsis[g]
        abb = s(r.get("pos_abb")).upper()
        # Offensive slots only; ignore special teams and defensive listings.
        if pos == "QB" and abb != "QB":
            continue
        if pos == "RB" and abb not in ("RB", "HB", "FB", "TB"):
            continue
        if pos == "WR" and abb not in ("WR", "LWR", "RWR", "SWR", "WR1", "WR2", "WR3"):
            continue
        if pos == "TE" and abb not in ("TE", "TE1", "TE2"):
            continue
        rank = i(r.get("pos_rank"), 0) or i(r.get("pos_slot"), 0)
        if rank <= 0:
            continue
        if g not in best or rank < best[g]:
            best[g] = rank

    for g, rank in best.items():
        conn.execute("UPDATE players SET depth_order=? WHERE gsis_id=?", (rank, g))
    log(f"  {len(best)} depth-chart ranks applied")
    return len(best)


def fetch_player_season(conn, force=False) -> int:
    log("nflverse: player season stats ...")
    total = 0
    for season in HISTORY_SEASONS:
        try:
            rows = fetch_csv(f"{NFLVERSE}/stats_player/stats_player_reg_{season}.csv",
                             ttl_hours=24 * 30, force=force)
        except FileNotFoundError:
            log(f"  ! stats_player_reg_{season} missing")
            continue
        out = []
        for r in rows:
            pos = norm_pos(r.get("position"))
            if pos not in ("QB", "RB", "WR", "TE", "K"):
                continue
            two = f(r.get("passing_2pt_conversions")) + f(r.get("rushing_2pt_conversions")) + \
                f(r.get("receiving_2pt_conversions"))
            # OFFENSIVE fumbles only. `fumbles_lost_total` also counts fumbles on
            # kick and punt returns, which are not offensive touches: charging
            # them against a player's fumble-per-touch rate penalises exactly the
            # young, athletic receivers who double as returners (DJ Moore,
            # Tetairoa McMillan, Josh Downs all lost phantom points this way).
            # Verified: using these three columns reproduces nflverse's own
            # fantasy_points_ppr for 2020/2020 player-seasons, exactly.
            fumbles_lost = f(r.get("rushing_fumbles_lost")) + \
                f(r.get("receiving_fumbles_lost")) + f(r.get("sack_fumbles_lost"))
            out.append((
                s(r.get("player_id")), season, norm_team(r.get("recent_team")),
                pos, i(r.get("games")),
                f(r.get("completions")), f(r.get("attempts")), f(r.get("passing_yards")),
                f(r.get("passing_tds")), f(r.get("passing_interceptions")),
                f(r.get("sacks_suffered")), f(r.get("passing_epa")), f(r.get("passing_cpoe")),
                f(r.get("carries")), f(r.get("rushing_yards")), f(r.get("rushing_tds")),
                f(r.get("rushing_epa")), f(r.get("rushing_first_downs")), f(r.get("rushing_20")),
                f(r.get("targets")), f(r.get("receptions")), f(r.get("receiving_yards")),
                f(r.get("receiving_tds")), f(r.get("receiving_epa")),
                f(r.get("receiving_air_yards")), f(r.get("receiving_yards_after_catch")),
                f(r.get("receiving_first_downs")), f(r.get("receiving_20")),
                f(r.get("target_share")), f(r.get("air_yards_share")), f(r.get("wopr")),
                f(r.get("racr")), fumbles_lost, two, f(r.get("special_teams_tds")),
                f(r.get("fantasy_points")), f(r.get("fantasy_points_ppr")),
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO player_season VALUES (" + ",".join("?" * 37) + ")", out)
        total += len(out)
        log(f"  {season}: {len(out)} players")
    return total


def fetch_team_season(conn, force=False) -> int:
    log("nflverse: team season stats ...")
    total = 0
    # Deeper history here: coach career profiles need many seasons.
    seasons = sorted(set(HISTORY_SEASONS) | set(range(2012, SEASON)))
    for season in seasons:
        try:
            rows = fetch_csv(f"{NFLVERSE}/stats_team/stats_team_reg_{season}.csv",
                             ttl_hours=24 * 60, force=force)
        except FileNotFoundError:
            continue
        out = [(
            norm_team(r.get("team")), season, i(r.get("games")),
            f(r.get("attempts")), f(r.get("completions")), f(r.get("passing_yards")),
            f(r.get("passing_tds")), f(r.get("carries")), f(r.get("rushing_yards")),
            f(r.get("rushing_tds")), f(r.get("targets")), f(r.get("receptions")),
            f(r.get("receiving_yards")), f(r.get("sacks_suffered")),
            f(r.get("passing_epa")), f(r.get("rushing_epa")),
        ) for r in rows]
        conn.executemany(
            "INSERT OR REPLACE INTO team_season VALUES (" + ",".join("?" * 16) + ")", out)
        total += len(out)
    log(f"  {total} team-seasons")
    return total


def fetch_snaps(conn, force=False) -> int:
    log("nflverse: snap counts ...")
    total = 0
    for season in HISTORY_SEASONS:
        try:
            rows = fetch_csv(f"{NFLVERSE}/snap_counts/snap_counts_{season}.csv",
                             ttl_hours=24 * 30, force=force)
        except FileNotFoundError:
            continue
        agg = defaultdict(lambda: {"g": 0, "sn": 0.0, "pct": 0.0, "player": "", "pos": "", "team": ""})
        for r in rows:
            if s(r.get("game_type")) != "REG":
                continue
            pos = norm_pos(r.get("position"))
            if pos not in ("QB", "RB", "WR", "TE", "K"):
                continue
            key = (s(r.get("pfr_player_id")), norm_team(r.get("team")))
            a = agg[key]
            a["g"] += 1
            a["sn"] += f(r.get("offense_snaps"))
            a["pct"] += f(r.get("offense_pct"))
            a["player"] = s(r.get("player"))
            a["pos"] = pos
            a["team"] = key[1]
        out = [(k[0], v["player"], season, v["team"], v["pos"], v["g"], v["sn"],
                v["pct"] / v["g"] if v["g"] else 0.0) for k, v in agg.items() if k[0]]
        conn.executemany("INSERT OR REPLACE INTO snaps VALUES (?,?,?,?,?,?,?,?)", out)
        total += len(out)
    log(f"  {total} player-season snap rows")
    return total


def fetch_injuries(conn, force=False) -> int:
    """Collapse weekly injury reports into a per-player-season durability record."""
    log("nflverse: injury reports ...")
    total = 0
    for season in INJURY_SEASONS:
        try:
            rows = fetch_csv(f"{NFLVERSE}/injuries/injuries_{season}.csv",
                             ttl_hours=24 * 30, force=force)
        except FileNotFoundError:
            continue
        agg = defaultdict(lambda: {"out": 0, "dnp": 0, "lim": 0, "q": 0,
                                   "parts": set(), "team": ""})
        for r in rows:
            g = s(r.get("gsis_id"))
            if not g:
                continue
            a = agg[g]
            a["team"] = norm_team(r.get("team"))
            status = s(r.get("report_status")).lower()
            practice = s(r.get("practice_status")).lower()
            if status == "out":
                a["out"] += 1
            elif status == "doubtful":
                a["out"] += 1
            elif status == "questionable":
                a["q"] += 1
            if "did not participate" in practice:
                a["dnp"] += 1
            elif "limited" in practice:
                a["lim"] += 1
            for col in ("report_primary_injury", "report_secondary_injury",
                        "practice_primary_injury"):
                part = s(r.get(col)).lower()
                if part and "not injury related" not in part:
                    a["parts"].add(part)
        out = [(g, season, v["team"], v["out"], v["dnp"], v["lim"], v["q"],
                ",".join(sorted(v["parts"]))[:400]) for g, v in agg.items()]
        conn.executemany("INSERT OR REPLACE INTO injury_history VALUES (?,?,?,?,?,?,?,?)", out)
        total += len(out)
    log(f"  {total} player-season injury records")
    return total


def fetch_adv_rush(conn, force=False) -> int:
    log("nflverse: advanced rushing ...")
    total = 0
    for season in HISTORY_SEASONS:
        rows = None
        for path in (f"{NFLVERSE}/pfr_advstats/advstats_season_rush_{season}.csv",
                     f"{NFLVERSE}/pfr_advstats/advstats_week_rush_{season}.csv"):
            try:
                rows = fetch_csv(path, ttl_hours=24 * 30, force=force)
                weekly = "week" in path
                break
            except FileNotFoundError:
                continue
        if not rows:
            continue
        agg = defaultdict(lambda: {"c": 0.0, "ybc": 0.0, "yac": 0.0, "brk": 0.0})
        for r in rows:
            pid = s(r.get("pfr_player_id")) or s(r.get("pfr_id"))
            if not pid:
                continue
            a = agg[pid]
            a["c"] += f(r.get("carries"))
            a["ybc"] += f(r.get("rushing_yards_before_contact"))
            a["yac"] += f(r.get("rushing_yards_after_contact"))
            a["brk"] += f(r.get("rushing_broken_tackles"))
        out = [(k, season, v["c"], v["ybc"], v["ybc"] / v["c"] if v["c"] else 0.0,
                v["yac"], v["yac"] / v["c"] if v["c"] else 0.0, v["brk"])
               for k, v in agg.items() if v["c"] > 0]
        conn.executemany("INSERT OR REPLACE INTO adv_rush VALUES (?,?,?,?,?,?,?,?)", out)
        total += len(out)
    log(f"  {total} advanced-rushing rows")
    return total


def fetch_ngs(conn, force=False) -> int:
    """Next Gen receiving: separation, cushion, air-yards share, YAC over expected."""
    log("nflverse: next gen stats (receiving) ...")
    try:
        rows = fetch_csv(f"{NFLVERSE}/nextgen_stats/ngs_receiving.csv.gz",
                         ttl_hours=24 * 14, force=force)
    except FileNotFoundError:
        log("  ! NGS unavailable")
        return 0
    out = []
    for r in rows:
        season = i(r.get("season"))
        if season not in HISTORY_SEASONS:
            continue
        # week == 0 rows are the season aggregates.
        if i(r.get("week"), -1) != 0:
            continue
        g = s(r.get("player_gsis_id"))
        if not g:
            continue
        out.append((g, season, f(r.get("avg_cushion")), f(r.get("avg_separation")),
                    f(r.get("avg_intended_air_yards")),
                    f(r.get("percent_share_of_intended_air_yards")),
                    f(r.get("catch_percentage")), f(r.get("avg_yac")),
                    f(r.get("avg_yac_above_expectation"))))
    conn.executemany("INSERT OR REPLACE INTO ngs_rec VALUES (?,?,?,?,?,?,?,?,?)", out)
    log(f"  {len(out)} NGS season rows")
    return len(out)


def fetch_schedule(conn, force=False) -> int:
    """Full schedule history: bye weeks, 2026 betting lines, and coach-by-season."""
    log("nflverse: schedules ...")
    rows = fetch_csv(f"{NFLVERSE}/schedules/games.csv", ttl_hours=12, force=force)
    out = [(
        s(r.get("game_id")), i(r.get("season")), i(r.get("week")), s(r.get("gameday")),
        norm_team(r.get("home_team")), norm_team(r.get("away_team")),
        s(r.get("home_coach")), s(r.get("away_coach")),
        f(r.get("spread_line"), None) if s(r.get("spread_line")) else None,
        f(r.get("total_line"), None) if s(r.get("total_line")) else None,
        s(r.get("roof")), s(r.get("surface")), i(r.get("div_game")),
        f(r.get("home_score"), None) if s(r.get("home_score")) else None,
        f(r.get("away_score"), None) if s(r.get("away_score")) else None,
    ) for r in rows if i(r.get("season")) >= 2012]
    conn.executemany(
        "INSERT OR REPLACE INTO schedule VALUES (" + ",".join("?" * 15) + ")", out)
    n26 = sum(1 for r in out if r[1] == SEASON)
    log(f"  {len(out)} games ({n26} in {SEASON})")
    return len(out)


# -------------------------------------------------------------------- ADP --
ADP_SNAPSHOT = "data/adp_history.csv"
ADP_SNAPSHOT_DAYS = 10


def save_adp_snapshot(conn) -> int:
    """Persist recent ADP snapshots to a small text file.

    Everything else in the database is re-downloaded from upstream on every run,
    so the ONLY state that has to survive between CI runs is the day-over-day ADP
    history the "movers" view is built from. Committing the whole database would
    mean ~2.7MB of churning binary per day (about a gigabyte a year, and useless
    diffs); this is a few hundred KB of text that git stores well.

    Pruned to the window the movers query actually reads, so it stops growing.
    """
    import csv as _csv

    path = BASE_DIR / ADP_SNAPSHOT
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM adp_history ORDER BY date DESC LIMIT ?",
        (ADP_SNAPSHOT_DAYS,))]
    if not dates:
        return 0
    rows = conn.execute(
        "SELECT fmt, search_name, date, adp FROM adp_history WHERE date >= ? "
        "ORDER BY date, fmt, search_name", (min(dates),)).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["fmt", "search_name", "date", "adp"])
        for r in rows:
            w.writerow([r["fmt"], r["search_name"], r["date"], r["adp"]])
    log(f"  ADP history snapshot: {len(rows)} rows across {len(dates)} day(s)")
    return len(rows)


def load_adp_snapshot(conn) -> int:
    """Restore the committed ADP history into a freshly built database."""
    path = BASE_DIR / ADP_SNAPSHOT
    if not path.exists():
        return 0
    import csv as _csv

    with open(path, encoding="utf-8", newline="") as fh:
        rows = [(r["fmt"], r["search_name"], r["date"], f(r["adp"]))
                for r in _csv.DictReader(fh)]
    if rows:
        conn.executemany("INSERT OR IGNORE INTO adp_history VALUES (?,?,?,?)", rows)
        log(f"  restored {len(rows)} ADP history rows from {ADP_SNAPSHOT}")
    return len(rows)


def fetch_adp(conn, teams=12, force=True) -> int:
    """Live ADP from FantasyFootballCalculator, for each scoring format."""
    log("FFC: average draft position ...")
    stamp = today()
    total = 0
    for fmt in sorted(set(FFC_FORMAT_FOR_SCORING.values())):
        url = FFC_ADP.format(fmt=fmt, teams=teams, year=SEASON)
        try:
            payload = fetch_json(url, ttl_hours=6, force=force)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! {fmt} failed: {exc}")
            continue
        meta = payload.get("meta") or {}
        players = payload.get("players") or []
        if not players:
            log(f"  ! {fmt}: no players returned")
            continue
        rows, hist = [], []
        for p in players:
            name = s(p.get("name"))
            if not name:
                continue
            pos = norm_pos(p.get("position"))
            team = norm_team(p.get("team"))
            # Team defenses key on team abbreviation (see fetch_sleeper).
            nm = team if pos == "DST" else norm_name(name)
            rows.append((
                fmt, name, nm, pos, team,
                f(p.get("adp")), s(p.get("adp_formatted")), f(p.get("stdev")),
                f(p.get("high")), f(p.get("low")), i(p.get("times_drafted")),
                i(p.get("bye")), s(p.get("player_id")),
                i(meta.get("total_drafts")), s(meta.get("start_date")), s(meta.get("end_date")),
            ))
            hist.append((fmt, nm, stamp, f(p.get("adp"))))
        conn.execute("DELETE FROM adp WHERE fmt=?", (fmt,))
        conn.executemany("INSERT OR REPLACE INTO adp VALUES (" + ",".join("?" * 16) + ")", rows)
        conn.executemany("INSERT OR REPLACE INTO adp_history VALUES (?,?,?,?)", hist)
        set_meta(conn, f"adp_drafts_{fmt}", meta.get("total_drafts", 0))
        log(f"  {fmt}: {len(rows)} players from {meta.get('total_drafts')} drafts "
            f"({meta.get('start_date')} .. {meta.get('end_date')})")
        total += len(rows)
    return total


# ------------------------------------------------------------------- main --
def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    full = "--full" in argv

    init_db()
    log("=" * 62)
    log(f"Gridiron Edge fetch starting (full={full})")
    with db() as conn:
        load_adp_snapshot(conn)
        n_players = fetch_sleeper(conn, force=True)
        fetch_rosters(conn, force=full)
        fetch_depth_charts(conn, force=full)
        fetch_schedule(conn, force=True)
        fetch_player_season(conn, force=full)
        fetch_team_season(conn, force=full)
        fetch_snaps(conn, force=full)
        fetch_injuries(conn, force=full)
        fetch_adv_rush(conn, force=full)
        fetch_ngs(conn, force=full)
        n_adp = fetch_adp(conn, force=True)
        save_adp_snapshot(conn)

        set_meta(conn, "last_fetch", today())
        set_meta(conn, "last_fetch_players", n_players)
        set_meta(conn, "last_fetch_adp", n_adp)
        bump_version(conn)

    log("Fetch complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
