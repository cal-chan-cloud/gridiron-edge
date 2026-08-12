"""
Team scoring environment for 2026, and the coaching layer that shapes it.

Three things get computed here and stored in `team_context`:

  1. PACE + PASS RATE. A team's own last three seasons, weighted for recency and
     regressed toward the league mean, give a baseline. That baseline is then
     pulled toward the 2026 play-caller's career fingerprint in proportion to how
     much the staff actually changed.

  2. COACH FINGERPRINTS. For every head coach since 2012 we look up the seasons
     they ran a team (from the schedule's coach column, which is reliable for
     completed seasons) and aggregate what their offenses actually did. No
     opinions - a coach who ran 68 plays a game at a 62% pass rate shows up that
     way. First-time coordinators inherit their coaching tree's average instead,
     with the uncertainty flagged.

  3. VACATED OPPORTUNITY. Targets and carries that belonged to 2025 players who
     are no longer on the 2026 roster. This is the cleanest available signal for
     "who has room to grow".
"""
from __future__ import annotations

from collections import defaultdict

from coaching import COACHES_2026, fix_name, play_caller, staff_changes
from config import (
    COACH_BLEND_NO_HISTORY, COACH_BLEND_WITH_HISTORY, COACH_HISTORY_START,
    HISTORY_SEASONS, LEAGUE_FALLBACK, SEASON, SEASON_WEIGHTS,
)


def _safe_div(a, b, default=0.0):
    return a / b if b else default


# ------------------------------------------------------------ coach layer --
def build_coach_profiles(conn) -> dict:
    """Aggregate every head coach's offensive output across the seasons they ran a team."""
    # (season, team) -> coach, from completed seasons only.
    coach_of = {}
    for r in conn.execute(
        "SELECT season, home_team, away_team, home_coach, away_coach FROM schedule "
        "WHERE season >= ? AND season < ? AND home_score IS NOT NULL",
        (COACH_HISTORY_START, SEASON),
    ):
        if r["home_coach"]:
            coach_of[(r["season"], r["home_team"])] = fix_name(r["home_coach"])
        if r["away_coach"]:
            coach_of[(r["season"], r["away_team"])] = fix_name(r["away_coach"])

    # Actual points scored per team-season, from final scores.
    pts = defaultdict(lambda: [0.0, 0])
    for r in conn.execute(
        "SELECT season, home_team, away_team, home_score, away_score FROM schedule "
        "WHERE season >= ? AND home_score IS NOT NULL", (COACH_HISTORY_START,)
    ):
        pts[(r["season"], r["home_team"])][0] += r["home_score"] or 0
        pts[(r["season"], r["home_team"])][1] += 1
        pts[(r["season"], r["away_team"])][0] += r["away_score"] or 0
        pts[(r["season"], r["away_team"])][1] += 1

    agg = defaultdict(lambda: {
        "seasons": 0, "team_seasons": [], "plays": 0.0, "dropbacks": 0.0,
        "rush": 0.0, "games": 0.0, "points": 0.0, "pgames": 0.0,
        "ptd": 0.0, "rtd": 0.0,
    })
    for r in conn.execute("SELECT * FROM team_season"):
        key = (r["season"], r["team"])
        coach = coach_of.get(key)
        if not coach:
            continue
        g = r["games"] or 0
        if g <= 0:
            continue
        dropbacks = (r["attempts"] or 0) + (r["sacks"] or 0)
        rush = r["carries"] or 0
        a = agg[coach]
        a["seasons"] += 1
        a["team_seasons"].append(f"{r['season']} {r['team']}")
        a["plays"] += dropbacks + rush
        a["dropbacks"] += dropbacks
        a["rush"] += rush
        a["games"] += g
        a["ptd"] += r["passing_tds"] or 0
        a["rtd"] += r["rushing_tds"] or 0
        p, pg = pts.get(key, (0.0, 0))
        a["points"] += p
        a["pgames"] += pg

    out = {}
    rows = []
    for coach, a in agg.items():
        g = a["games"]
        if g < 8:  # less than half a season as a head coach: not a signal
            continue
        prof = {
            "coach": coach,
            "seasons": a["seasons"],
            "team_seasons": ", ".join(sorted(a["team_seasons"])[-6:]),
            "plays_per_game": _safe_div(a["plays"], g),
            "pass_rate": _safe_div(a["dropbacks"], a["plays"]),
            "points_per_game": _safe_div(a["points"], a["pgames"]),
            "pass_att_pg": _safe_div(a["dropbacks"], g),
            "rush_att_pg": _safe_div(a["rush"], g),
            "pass_td_rate": _safe_div(a["ptd"], a["dropbacks"]),
            "rush_td_rate": _safe_div(a["rtd"], a["rush"]),
        }
        out[coach] = prof
        rows.append(tuple(prof[k] for k in (
            "coach", "seasons", "team_seasons", "plays_per_game", "pass_rate",
            "points_per_game", "pass_att_pg", "rush_att_pg", "pass_td_rate", "rush_td_rate")))

    conn.execute("DELETE FROM coach_profile")
    conn.executemany("INSERT OR REPLACE INTO coach_profile VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return out


def _tree_average(profiles: dict, tree: str) -> dict | None:
    """Average fingerprint of every 2026 coach in a given offensive lineage.

    Used when a play-caller has never called plays: inheriting the tree they came
    up in is a far better prior than league average, and it is still derived
    from real data rather than assertion.
    """
    members = [profiles[c["hc"]] for t, c in COACHES_2026.items()
               if c["tree"] == tree and c["hc"] in profiles]
    if not members:
        return None
    keys = ("plays_per_game", "pass_rate", "points_per_game")
    return {k: sum(m[k] for m in members) / len(members) for k in keys}


# ------------------------------------------------------------ team layer --
def _team_baselines(conn) -> dict:
    """Recency-weighted, sample-aware pace and pass rate for each team."""
    by_team = defaultdict(dict)
    for r in conn.execute("SELECT * FROM team_season WHERE season IN (?,?,?)", tuple(HISTORY_SEASONS)):
        g = r["games"] or 0
        if g <= 0:
            continue
        dropbacks = (r["attempts"] or 0) + (r["sacks"] or 0)
        plays = dropbacks + (r["carries"] or 0)
        by_team[r["team"]][r["season"]] = {
            "plays_pg": plays / g,
            "pass_rate": _safe_div(dropbacks, plays, LEAGUE_FALLBACK["pass_rate"]),
        }

    # League means, computed from the data rather than hard-coded.
    latest = [v[HISTORY_SEASONS[0]] for v in by_team.values() if HISTORY_SEASONS[0] in v]
    lg_plays = sum(x["plays_pg"] for x in latest) / len(latest) if latest else LEAGUE_FALLBACK["plays_per_game"]
    lg_pass = sum(x["pass_rate"] for x in latest) / len(latest) if latest else LEAGUE_FALLBACK["pass_rate"]

    out = {}
    for team, seasons in by_team.items():
        num_p = num_r = wsum = 0.0
        for w, season in zip(SEASON_WEIGHTS, HISTORY_SEASONS):
            if season in seasons:
                num_p += w * seasons[season]["plays_pg"]
                num_r += w * seasons[season]["pass_rate"]
                wsum += w
        if wsum <= 0:
            continue
        plays, prate = num_p / wsum, num_r / wsum
        # Regress toward the league mean; team pace is only ~60% year-over-year stable.
        out[team] = {
            "plays_pg": 0.72 * plays + 0.28 * lg_plays,
            "pass_rate": 0.70 * prate + 0.30 * lg_pass,
        }
    return out, lg_plays, lg_pass


def _implied_points(conn) -> dict:
    """Market-implied points per game for 2026, from posted totals and spreads.

    Only early-season games have lines up in August, so this covers ~3 games per
    team. It is used as a light-touch adjustment, never as the primary driver,
    and the sample size is surfaced in the UI so it can be judged.
    """
    acc = defaultdict(list)
    for r in conn.execute(
        "SELECT home_team, away_team, spread_line, total_line FROM schedule "
        "WHERE season=? AND total_line IS NOT NULL AND spread_line IS NOT NULL", (SEASON,)
    ):
        tot, spread = r["total_line"], r["spread_line"]
        # nflverse spread_line is from the home team's perspective (positive = home favoured).
        acc[r["home_team"]].append(tot / 2.0 + spread / 2.0)
        acc[r["away_team"]].append(tot / 2.0 - spread / 2.0)
    return {t: (sum(v) / len(v), len(v)) for t, v in acc.items() if v}


def _bye_weeks(conn) -> dict:
    played = defaultdict(set)
    for r in conn.execute("SELECT week, home_team, away_team FROM schedule WHERE season=?", (SEASON,)):
        played[r["home_team"]].add(r["week"])
        played[r["away_team"]].add(r["week"])
    out = {}
    for team, weeks in played.items():
        missing = [w for w in range(1, 19) if w not in weeks]
        out[team] = missing[0] if missing else None
    return out


def _vacated(conn) -> dict:
    """2025 targets and carries that have LEFT each team.

    The question is not "is this player retired" but "is he still here". A back
    who moved from Atlanta to San Francisco vacates Atlanta's carries just as
    completely as one who retired, and those carries are the single best signal
    for which backfield has room for someone to break out. Checking only whether
    a player is on SOME 2026 roster misses every trade and free-agent move, which
    is most of them - it reported vacated carries as 0% for all 32 teams.
    """
    team_now = {r["gsis_id"]: r["team"] for r in conn.execute(
        "SELECT gsis_id, team FROM players "
        "WHERE gsis_id IS NOT NULL AND team IS NOT NULL AND team!=''")}
    out = defaultdict(lambda: {"tgt": 0.0, "car": 0.0, "tgt_tot": 0.0, "car_tot": 0.0})
    for r in conn.execute(
        "SELECT gsis_id, team, targets, carries FROM player_season WHERE season=?",
        (HISTORY_SEASONS[0],)
    ):
        t = out[r["team"]]
        t["tgt_tot"] += r["targets"] or 0
        t["car_tot"] += r["carries"] or 0
        if team_now.get(r["gsis_id"]) != r["team"]:
            t["tgt"] += r["targets"] or 0
            t["car"] += r["carries"] or 0
    return out


def _sos(conn) -> dict:
    """Strength of schedule: mean 2025 points allowed by each 2026 opponent.

    Expressed relative to league average, so 1.00 is neutral, >1 is an easier
    slate for the offense.
    """
    allowed = defaultdict(lambda: [0.0, 0])
    prev = HISTORY_SEASONS[0]
    for r in conn.execute(
        "SELECT home_team, away_team, home_score, away_score FROM schedule "
        "WHERE season=? AND home_score IS NOT NULL", (prev,)
    ):
        allowed[r["home_team"]][0] += r["away_score"] or 0
        allowed[r["home_team"]][1] += 1
        allowed[r["away_team"]][0] += r["home_score"] or 0
        allowed[r["away_team"]][1] += 1
    pa = {t: v[0] / v[1] for t, v in allowed.items() if v[1]}
    if not pa:
        return {}
    lg = sum(pa.values()) / len(pa)

    opp = defaultdict(list)
    for r in conn.execute("SELECT home_team, away_team FROM schedule WHERE season=?", (SEASON,)):
        opp[r["home_team"]].append(r["away_team"])
        opp[r["away_team"]].append(r["home_team"])
    out = {}
    for team, opps in opp.items():
        vals = [pa[o] for o in opps if o in pa]
        out[team] = (sum(vals) / len(vals)) / lg if vals else 1.0
    return out


def _scored_last_season(conn) -> dict:
    """Actual points per game scored by each team in the most recent season."""
    acc = defaultdict(lambda: [0.0, 0])
    for r in conn.execute(
        "SELECT home_team, away_team, home_score, away_score FROM schedule "
        "WHERE season=? AND home_score IS NOT NULL", (HISTORY_SEASONS[0],)
    ):
        acc[r["home_team"]][0] += r["home_score"] or 0
        acc[r["home_team"]][1] += 1
        acc[r["away_team"]][0] += r["away_score"] or 0
        acc[r["away_team"]][1] += 1
    return {t: v[0] / v[1] for t, v in acc.items() if v[1]}


def build_team_context(conn) -> dict:
    """Compute and persist the 2026 environment for all 32 teams."""
    profiles = build_coach_profiles(conn)
    baselines, lg_plays, lg_pass = _team_baselines(conn)
    implied = _implied_points(conn)
    byes = _bye_weeks(conn)
    vac = _vacated(conn)
    sos = _sos(conn)
    scored = _scored_last_season(conn)

    # League-average implied points, computed once rather than per team.
    lg_ipg = (sum(v[0] for v in implied.values()) / len(implied)) if implied else None

    rows, out = [], {}
    for team, staff in COACHES_2026.items():
        base = baselines.get(team, {"plays_pg": lg_plays, "pass_rate": lg_pass})
        plays, prate = base["plays_pg"], base["pass_rate"]

        chg = staff_changes(team)
        caller = play_caller(team)
        note_bits = []
        if chg["caller_changed"]:
            prof = profiles.get(caller)
            if prof and prof["seasons"] >= 1:
                blend = COACH_BLEND_WITH_HISTORY * chg["severity"]
                plays = (1 - blend) * plays + blend * prof["plays_per_game"]
                prate = (1 - blend) * prate + blend * prof["pass_rate"]
                note_bits.append(
                    f"{caller} takes over play-calling; blended {int(blend * 100)}% toward his "
                    f"career {prof['pass_rate'] * 100:.0f}% pass rate / "
                    f"{prof['plays_per_game']:.0f} plays a game over {prof['seasons']} season(s)")
            else:
                tree = _tree_average(profiles, staff["tree"])
                blend = COACH_BLEND_NO_HISTORY * chg["severity"]
                if tree:
                    plays = (1 - blend) * plays + blend * tree["plays_per_game"]
                    prate = (1 - blend) * prate + blend * tree["pass_rate"]
                    note_bits.append(
                        f"{caller} has no play-calling record; leaning {int(blend * 100)}% on the "
                        f"{staff['tree']} tree average. Wider range of outcomes than usual")
                else:
                    note_bits.append(f"{caller} is an unknown quantity as a play-caller")
        elif chg["hc_changed"] or chg["oc_changed"]:
            note_bits.append(
                f"{caller} still calls the plays, so the offense's identity carries over; "
                f"the staff change around him is not a scheme reset")
        else:
            note_bits.append(f"{caller} returns as play-caller; continuity from 2025")

        ipg, igames = implied.get(team, (None, 0))
        if ipg is not None and igames >= 2 and lg_ipg:
            # Light touch: the market only has ~3 games priced this early.
            adj = 1.0 + 0.10 * ((ipg / lg_ipg) - 1.0)
            plays *= min(max(adj, 0.94), 1.06)

        # Expected scoring: last season's actual, nudged by the (thin) market
        # sample. Only ~3 games per team are priced this early in August, so the
        # market gets a minority weight rather than driving the number.
        last_ppg = scored.get(team)
        if last_ppg is not None and ipg is not None and igames >= 2:
            exp_ppg = 0.68 * last_ppg + 0.32 * ipg
        else:
            exp_ppg = last_ppg if last_ppg is not None else (ipg or 22.5)

        v = vac[team]
        rows.append((
            team, SEASON, plays, prate, plays * prate, plays * (1 - prate),
            ipg, igames, byes.get(team),
            v["tgt"], v["car"],
            ". ".join(note_bits) + ".", caller, int(chg["caller_changed"]),
            chg["severity"], sos.get(team, 1.0),
        ))
        out[team] = {
            "plays_pg": plays, "pass_rate": prate,
            "pass_att_pg": plays * prate, "rush_att_pg": plays * (1 - prate),
            "implied_ppg": ipg, "implied_games": igames,
            "last_ppg": last_ppg, "exp_ppg": exp_ppg, "bye": byes.get(team),
            "vacated_targets": v["tgt"], "vacated_carries": v["car"],
            "vacated_target_pct": _safe_div(v["tgt"], v["tgt_tot"]),
            "vacated_carry_pct": _safe_div(v["car"], v["car_tot"]),
            "caller": caller, "caller_changed": chg["caller_changed"],
            "note": ". ".join(note_bits) + ".", "sos": sos.get(team, 1.0),
        }

    conn.execute("DELETE FROM team_context")
    conn.executemany(
        "INSERT OR REPLACE INTO team_context VALUES (" + ",".join("?" * 16) + ")", rows)
    return out
