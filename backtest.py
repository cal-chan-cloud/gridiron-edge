"""
Score the model against a season that has already happened.

The question "do we need more metrics, or do we have too much data?" is not
answerable by argument. It is answerable by rebuilding the state as it existed
BEFORE a season started, projecting that season, and comparing to what actually
occurred — then switching each input off in turn and seeing whether the error
gets better or worse. An input that does not reduce error is not signal, it is
surface area.

Run:
    python backtest.py                 # baseline, all shipping features on
    python backtest.py --ablate        # baseline + one run per feature, toggled
    python backtest.py --season 2025

What it reconstructs for target season N:
    players      roster_N week 1                  (team as of kickoff)
    depth        depth_charts_N earliest snapshot (preseason, like ours for 2026)
    production   player_season N-1 .. N-3
    team context team_season through N-1
    injuries     injury reports N-1 .. N-5
    schedule     everything through N (byes and coaches; scores only through N-1)

and then scores against actual season-N fantasy points.

HONEST LIMITS, so the numbers are not over-read:
  * The curated coaching table is 2026-specific, so `curated_coaching` is forced
    off here and coaches come from the schedule column. This tests the coaching
    MECHANISM, not this year's hand-verified staff list.
  * ADP is not available historically, so anything ADP-derived is out of scope.
  * One held-out season is one sample. Treat a 0.01 change in correlation as
    noise; treat 0.05 as real.
"""
from __future__ import annotations

import os
import sys

# Must be set before config (and therefore the model) is imported.
TARGET = 2025
for i, a in enumerate(sys.argv):
    if a == "--season" and i + 1 < len(sys.argv):
        TARGET = int(sys.argv[i + 1])
os.environ["FF_SEASON"] = str(TARGET)
os.environ["FF_FEAT_CURATED_COACHING"] = "0"

import sqlite3  # noqa: E402
from collections import defaultdict  # noqa: E402

import config  # noqa: E402
from config import BASE_DIR, HISTORY_SEASONS, INJURY_SEASONS, NFLVERSE, SCORING_PRESETS  # noqa: E402
from data.sources import f, fetch_csv, i as _i, log, norm_name, norm_team, s  # noqa: E402
from models.database import SCHEMA  # noqa: E402

BT_DB = BASE_DIR / f"backtest_{TARGET}.db"
POS_OK = ("QB", "RB", "WR", "TE")


def _conn(path):
    c = sqlite3.connect(str(path), timeout=30.0)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def build_state(force=False) -> None:
    """Reconstruct the pre-season database for the target year."""
    if BT_DB.exists() and not force:
        log(f"backtest state {BT_DB.name} exists; reusing")
        return
    if BT_DB.exists():
        BT_DB.unlink()
    log(f"building pre-{TARGET} state ...")
    c = _conn(BT_DB)

    # --- players: the week-1 roster, i.e. who was where at kickoff.
    # NOT rosters/roster_YYYY.csv — for a completed season that file records
    # roster CHANGES (its week-1 slice is 331 rows, mostly status CUT).
    # weekly_rosters is the actual per-week snapshot.
    roster = fetch_csv(f"{NFLVERSE}/weekly_rosters/roster_weekly_{TARGET}.csv", ttl_hours=24 * 90)
    wk1 = [r for r in roster if _i(r.get("week")) == 1
           and s(r.get("status")).upper() in ("ACT", "RES", "INA", "DEV")]
    seen = set()
    rows = []
    for r in wk1:
        pos = s(r.get("position")).upper()
        pos = "RB" if pos == "FB" else pos
        if pos not in POS_OK:
            continue
        gsis = s(r.get("gsis_id"))
        if not gsis or gsis in seen:
            continue
        seen.add(gsis)
        name = s(r.get("full_name"))
        rows.append((gsis, s(r.get("sleeper_id")) or None, gsis, s(r.get("pfr_id")) or None,
                     name, norm_name(name), pos, norm_team(r.get("team")),
                     f(r.get("years_exp"), 0) or None, _i(r.get("years_exp")),
                     _i(r.get("rookie_year")) or None, _i(r.get("draft_number")) or None, 1))
    c.executemany(
        "INSERT OR REPLACE INTO players (player_id,sleeper_id,gsis_id,pfr_id,name,search_name,"
        "position,team,age,years_exp,rookie_year,draft_number,active) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    log(f"  players: {len(rows)} on week-1 rosters")

    # age at kickoff, from birth date
    for r in wk1:
        bd = s(r.get("birth_date"))
        if bd and len(bd) >= 4:
            try:
                age = TARGET - int(bd[:4])
                c.execute("UPDATE players SET age=? WHERE gsis_id=?", (age, s(r.get("gsis_id"))))
            except ValueError:
                pass

    # --- depth chart, preseason.
    #
    # TWO SCHEMAS. nflverse changed the format after 2024:
    #   2025+  daily snapshots, columns dt / pos_abb / pos_rank (~500k rows)
    #   <=2024 weekly, columns week / formation / depth_position / depth_team
    # Parsing only the new one meant FOUR of five backtest seasons silently had
    # ZERO depth-chart ranks — and the depth prior is the largest single input in
    # the model. Every ablation result measured before this was fixed was
    # computed on a testbed missing its most important feature.
    dc = fetch_csv(f"{NFLVERSE}/depth_charts/depth_charts_{TARGET}.csv", ttl_hours=24 * 90)
    pos_by = {r["gsis_id"]: r["position"] for r in c.execute(
        "SELECT gsis_id, position FROM players WHERE gsis_id IS NOT NULL")}
    best, label = {}, ""
    if dc and "pos_abb" in dc[0]:
        day = min(s(r.get("dt"))[:10] for r in dc)
        label = f"{day} snapshot"
        for r in (x for x in dc if s(x.get("dt", ""))[:10] == day):
            g = s(r.get("gsis_id"))
            if g not in pos_by:
                continue
            p, abb = pos_by[g], s(r.get("pos_abb")).upper()
            # "FB" excluded on purpose: fullbacks sit on their own ladder, so
            # accepting it hands a team's FB1 the RB1 prior.
            if p == "QB" and abb != "QB":
                continue
            if p == "RB" and abb not in ("RB", "HB", "TB"):
                continue
            if p == "WR" and abb not in ("WR", "LWR", "RWR", "SWR", "WR1", "WR2", "WR3"):
                continue
            if p == "TE" and abb not in ("TE", "TE1", "TE2"):
                continue
            rank = _i(r.get("pos_rank")) or _i(r.get("pos_slot"))
            if rank > 0 and (g not in best or rank < best[g]):
                best[g] = rank
    elif dc:
        # Legacy: week 1, offence only. depth_team is 1/2/3 and several players
        # can share a level (three WR1s), so rank sequentially within each
        # (team, position) group to approximate the newer pos_rank.
        label = "week 1 (legacy schema)"
        groups = defaultdict(list)
        for r in dc:
            if _i(r.get("week")) != 1 or s(r.get("formation")) != "Offense":
                continue
            g = s(r.get("gsis_id"))
            if g not in pos_by:
                continue
            dpos = s(r.get("depth_position")).upper()
            if dpos != pos_by[g]:
                continue            # excludes fullbacks listed under RB, as above
            groups[(s(r.get("club_code")), dpos)].append((_i(r.get("depth_team"), 9), g))
        for _key, members in groups.items():
            for rank, (_lvl, g) in enumerate(sorted(members), start=1):
                if g not in best or rank < best[g]:
                    best[g] = rank
    for g, rank in best.items():
        c.execute("UPDATE players SET depth_order=? WHERE gsis_id=?", (rank, g))
    log(f"  depth chart: {label}, {len(best)} ranks")

    # --- production history (strictly before the target season).
    # INJURY_SEASONS, not HISTORY_SEASONS: the availability model weights five
    # seasons and needs real games-played for all of them.
    from data import fetch_all as FA
    for season in INJURY_SEASONS:
        try:
            src = fetch_csv(f"{NFLVERSE}/stats_player/stats_player_reg_{season}.csv", ttl_hours=24 * 90)
        except FileNotFoundError:
            continue
        out = []
        for r in src:
            pos = FA.norm_pos(r.get("position"))
            if pos not in ("QB", "RB", "WR", "TE", "K"):
                continue
            two = f(r.get("passing_2pt_conversions")) + f(r.get("rushing_2pt_conversions")) + \
                f(r.get("receiving_2pt_conversions"))
            fum = f(r.get("rushing_fumbles_lost")) + f(r.get("receiving_fumbles_lost")) + \
                f(r.get("sack_fumbles_lost"))
            out.append((
                s(r.get("player_id")), season, norm_team(r.get("recent_team")), pos, _i(r.get("games")),
                f(r.get("completions")), f(r.get("attempts")), f(r.get("passing_yards")),
                f(r.get("passing_tds")), f(r.get("passing_interceptions")), f(r.get("sacks_suffered")),
                f(r.get("passing_epa")), f(r.get("passing_cpoe")), f(r.get("carries")),
                f(r.get("rushing_yards")), f(r.get("rushing_tds")), f(r.get("rushing_epa")),
                f(r.get("rushing_first_downs")), f(r.get("rushing_20")), f(r.get("targets")),
                f(r.get("receptions")), f(r.get("receiving_yards")), f(r.get("receiving_tds")),
                f(r.get("receiving_epa")), f(r.get("receiving_air_yards")),
                f(r.get("receiving_yards_after_catch")), f(r.get("receiving_first_downs")),
                f(r.get("receiving_20")), f(r.get("target_share")), f(r.get("air_yards_share")),
                f(r.get("wopr")), f(r.get("racr")), fum, two, f(r.get("special_teams_tds")),
                f(r.get("fantasy_points")), f(r.get("fantasy_points_ppr"))))
        c.executemany("INSERT OR REPLACE INTO player_season VALUES (" + ",".join("?" * 37) + ")", out)
    log(f"  production: seasons {HISTORY_SEASONS}")

    for season in range(2012, TARGET):
        try:
            src = fetch_csv(f"{NFLVERSE}/stats_team/stats_team_reg_{season}.csv", ttl_hours=24 * 90)
        except FileNotFoundError:
            continue
        c.executemany("INSERT OR REPLACE INTO team_season VALUES (" + ",".join("?" * 16) + ")",
                      [(norm_team(r.get("team")), season, _i(r.get("games")), f(r.get("attempts")),
                        f(r.get("completions")), f(r.get("passing_yards")), f(r.get("passing_tds")),
                        f(r.get("carries")), f(r.get("rushing_yards")), f(r.get("rushing_tds")),
                        f(r.get("targets")), f(r.get("receptions")), f(r.get("receiving_yards")),
                        f(r.get("sacks_suffered")), f(r.get("passing_epa")), f(r.get("rushing_epa")))
                       for r in src])

    # --- snaps and injuries, strictly prior seasons
    for season in INJURY_SEASONS:
        try:
            src = fetch_csv(f"{NFLVERSE}/snap_counts/snap_counts_{season}.csv", ttl_hours=24 * 90)
        except FileNotFoundError:
            continue
        agg = defaultdict(lambda: {"g": 0, "sn": 0.0, "pct": 0.0, "pos": "", "team": ""})
        for r in src:
            if s(r.get("game_type")) != "REG":
                continue
            pos = FA.norm_pos(r.get("position"))
            if pos not in POS_OK:
                continue
            k = (s(r.get("pfr_player_id")), norm_team(r.get("team")))
            a = agg[k]
            a["g"] += 1
            a["sn"] += f(r.get("offense_snaps"))
            a["pct"] += f(r.get("offense_pct"))
            a["pos"], a["team"] = pos, k[1]
        c.executemany("INSERT OR REPLACE INTO snaps VALUES (?,?,?,?,?,?,?,?)",
                      [(k[0], "", season, v["team"], v["pos"], v["g"], v["sn"],
                        v["pct"] / v["g"] if v["g"] else 0.0) for k, v in agg.items() if k[0]])

    for season in INJURY_SEASONS:
        try:
            src = fetch_csv(f"{NFLVERSE}/injuries/injuries_{season}.csv", ttl_hours=24 * 90)
        except FileNotFoundError:
            continue
        agg = defaultdict(lambda: {"out": 0, "dnp": 0, "lim": 0, "q": 0, "parts": set(), "team": ""})
        for r in src:
            g = s(r.get("gsis_id"))
            if not g:
                continue
            a = agg[g]
            a["team"] = norm_team(r.get("team"))
            st = s(r.get("report_status")).lower()
            pr = s(r.get("practice_status")).lower()
            if st in ("out", "doubtful"):
                a["out"] += 1
            elif st == "questionable":
                a["q"] += 1
            if "did not participate" in pr:
                a["dnp"] += 1
            elif "limited" in pr:
                a["lim"] += 1
            for col in ("report_primary_injury", "report_secondary_injury", "practice_primary_injury"):
                part = s(r.get(col)).lower()
                if part and "not injury related" not in part:
                    a["parts"].add(part)
        c.executemany("INSERT OR REPLACE INTO injury_history VALUES (?,?,?,?,?,?,?,?)",
                      [(g, season, v["team"], v["out"], v["dnp"], v["lim"], v["q"],
                        ",".join(sorted(v["parts"]))[:400]) for g, v in agg.items()])

    # --- NGS + advanced rushing
    try:
        ngs = fetch_csv(f"{NFLVERSE}/nextgen_stats/ngs_receiving.csv.gz", ttl_hours=24 * 90)
        c.executemany("INSERT OR REPLACE INTO ngs_rec VALUES (?,?,?,?,?,?,?,?,?)",
                      [(s(r.get("player_gsis_id")), _i(r.get("season")), f(r.get("avg_cushion")),
                        f(r.get("avg_separation")), f(r.get("avg_intended_air_yards")),
                        f(r.get("percent_share_of_intended_air_yards")), f(r.get("catch_percentage")),
                        f(r.get("avg_yac")), f(r.get("avg_yac_above_expectation")))
                       for r in ngs if _i(r.get("season")) in HISTORY_SEASONS
                       and _i(r.get("week"), -1) == 0 and s(r.get("player_gsis_id"))])
    except FileNotFoundError:
        pass
    for season in HISTORY_SEASONS:
        for path in (f"{NFLVERSE}/pfr_advstats/advstats_season_rush_{season}.csv",
                     f"{NFLVERSE}/pfr_advstats/advstats_week_rush_{season}.csv"):
            try:
                src = fetch_csv(path, ttl_hours=24 * 90)
            except FileNotFoundError:
                continue
            agg = defaultdict(lambda: {"c": 0.0, "ybc": 0.0, "yac": 0.0, "brk": 0.0})
            for r in src:
                pid = s(r.get("pfr_player_id")) or s(r.get("pfr_id"))
                if not pid:
                    continue
                a = agg[pid]
                a["c"] += f(r.get("carries"))
                a["ybc"] += f(r.get("rushing_yards_before_contact"))
                a["yac"] += f(r.get("rushing_yards_after_contact"))
                a["brk"] += f(r.get("rushing_broken_tackles"))
            c.executemany("INSERT OR REPLACE INTO adv_rush VALUES (?,?,?,?,?,?,?,?)",
                          [(k, season, v["c"], v["ybc"], v["ybc"] / v["c"] if v["c"] else 0.0,
                            v["yac"], v["yac"] / v["c"] if v["c"] else 0.0, v["brk"])
                           for k, v in agg.items() if v["c"] > 0])
            break

    # --- red-zone usage (prior seasons only)
    try:
        from data.redzone import ensure as rz_ensure
        rz_ensure(c, HISTORY_SEASONS)
    except Exception as exc:  # noqa: BLE001
        log(f"  ! redzone unavailable: {exc}")

    # --- schedule: byes and coaches for the target year; scores only from before
    games = fetch_csv(f"{NFLVERSE}/schedules/games.csv", ttl_hours=24)
    c.executemany("INSERT OR REPLACE INTO schedule VALUES (" + ",".join("?" * 15) + ")",
                  [(s(r.get("game_id")), _i(r.get("season")), _i(r.get("week")), s(r.get("gameday")),
                    norm_team(r.get("home_team")), norm_team(r.get("away_team")),
                    s(r.get("home_coach")), s(r.get("away_coach")),
                    f(r.get("spread_line"), None) if s(r.get("spread_line")) else None,
                    f(r.get("total_line"), None) if s(r.get("total_line")) else None,
                    s(r.get("roof")), s(r.get("surface")), _i(r.get("div_game")),
                    f(r.get("home_score"), None) if s(r.get("home_score")) and _i(r.get("season")) < TARGET else None,
                    f(r.get("away_score"), None) if s(r.get("away_score")) and _i(r.get("season")) < TARGET else None)
                   for r in games if 2012 <= _i(r.get("season")) <= TARGET])
    c.commit()
    c.close()
    log(f"  built {BT_DB.name}")


def actuals(scoring_key: str) -> dict:
    """Actual fantasy points scored in the target season."""
    sc = SCORING_PRESETS[scoring_key]
    src = fetch_csv(f"{NFLVERSE}/stats_player/stats_player_reg_{TARGET}.csv", ttl_hours=24 * 90)
    out = {}
    for r in src:
        fum = f(r.get("rushing_fumbles_lost")) + f(r.get("receiving_fumbles_lost")) + \
            f(r.get("sack_fumbles_lost"))
        two = f(r.get("passing_2pt_conversions")) + f(r.get("rushing_2pt_conversions")) + \
            f(r.get("receiving_2pt_conversions"))
        pts = (f(r.get("passing_yards")) * sc["pass_yd"] + f(r.get("passing_tds")) * sc["pass_td"]
               + f(r.get("passing_interceptions")) * sc["interception"]
               + f(r.get("rushing_yards")) * sc["rush_yd"] + f(r.get("rushing_tds")) * sc["rush_td"]
               + f(r.get("receiving_yards")) * sc["rec_yd"] + f(r.get("receiving_tds")) * sc["rec_td"]
               + f(r.get("receptions")) * sc["rec"] + fum * sc["fumble_lost"] + two * 2)
        out[s(r.get("player_id"))] = pts
    return out


def _spearman(pairs) -> float:
    n = len(pairs)
    if n < 3:
        return 0.0
    by_a = sorted(range(n), key=lambda i: -pairs[i][0])
    by_b = sorted(range(n), key=lambda i: -pairs[i][1])
    ra, rb = [0] * n, [0] * n
    for rank, idx in enumerate(by_a):
        ra[idx] = rank
    for rank, idx in enumerate(by_b):
        rb[idx] = rank
    d2 = sum((ra[i] - rb[i]) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n * n - 1))


def score(scoring_key="half_ppr", min_proj=40.0) -> dict:
    """Project the target season from the reconstructed state and score it."""
    from models.projection import project
    from models.context import build_team_context
    from models.injury import build_injury_profiles

    c = _conn(BT_DB)
    ctx = build_team_context(c)
    inj = build_injury_profiles(c)
    rows = project(c, scoring_key, ctx, inj)
    c.close()

    truth = actuals(scoring_key)
    # gsis id is the join; the backtest player_id IS the gsis id
    pairs, err = [], []
    for p in rows:
        if p["position"] not in POS_OK:
            continue
        if p["points"] < min_proj:
            continue
        a = truth.get(p["player_id"])
        if a is None:
            continue
        pairs.append((p["points"], a))
        err.append(abs(p["points"] - a))

    n = len(pairs)
    rho = _spearman(pairs)
    mae = sum(err) / n if n else 0.0
    # Top-36 hit rate: of the 36 we ranked highest, how many actually finished
    # in the real top 36? This is closer to "did the board help" than MAE.
    k = 36
    top_pred = {i for i, _ in sorted(enumerate(pairs), key=lambda x: -x[1][0])[:k]}
    top_act = {i for i, _ in sorted(enumerate(pairs), key=lambda x: -x[1][1])[:k]}
    hit = len(top_pred & top_act) / k if n >= k else 0.0
    return {"n": n, "spearman": rho, "mae": mae, "hit36": hit}


FLAGS = ["ngs_separation", "adv_rush_yac", "coach_blend", "implied_points",
         "age_curve", "availability", "td_regression", "sos", "snap_share",
         "efficiency_epa"]


def main() -> int:
    build_state(force="--rebuild" in sys.argv)
    base = score()
    print(f"\n{'=' * 74}")
    print(f"BASELINE — projecting {TARGET} from {HISTORY_SEASONS} (n={base['n']} players)")
    print(f"{'=' * 74}")
    print(f"  Spearman (rank correlation vs actual): {base['spearman']:.4f}")
    print(f"  MAE (points)                         : {base['mae']:.1f}")
    print(f"  Top-36 hit rate                      : {base['hit36'] * 100:.1f}%")

    if "--ablate" not in sys.argv:
        return 0

    print(f"\n{'=' * 74}")
    print("ABLATION — each feature toggled from its shipping default.")
    print("A feature that does not move the numbers is not signal, it is surface area.")
    print(f"{'=' * 74}")
    print(f"  {'feature':20s} {'state':>8s} {'spearman':>10s} {'delta':>9s} "
          f"{'MAE':>8s} {'delta':>8s} {'hit36':>7s}")
    results = []
    for flag in FLAGS:
        was = config.feature(flag)
        os.environ["FF_FEAT_" + flag.upper()] = "0" if was else "1"
        try:
            r = score()
        except Exception as exc:  # noqa: BLE001
            print(f"  {flag:20s} FAILED: {exc}")
            os.environ.pop("FF_FEAT_" + flag.upper(), None)
            continue
        os.environ.pop("FF_FEAT_" + flag.upper(), None)
        d_rho = r["spearman"] - base["spearman"]
        d_mae = r["mae"] - base["mae"]
        state = "OFF" if was else "ON"
        print(f"  {flag:20s} {state:>8s} {r['spearman']:10.4f} {d_rho:+9.4f} "
              f"{r['mae']:8.1f} {d_mae:+8.1f} {r['hit36'] * 100:6.1f}%")
        results.append((flag, was, d_rho, d_mae))

    print(f"\n{'=' * 74}\nREADING IT")
    print("  Feature currently ON, toggled OFF:")
    print("    delta NEGATIVE on spearman  -> the feature helps, keep it")
    print("    delta ~zero                 -> it earns nothing, consider removing")
    print("  Feature currently OFF, toggled ON:")
    print("    delta POSITIVE on spearman  -> adding it would help")
    print("  One season is one sample: <0.01 is noise, >0.05 is real.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
