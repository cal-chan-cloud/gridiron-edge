"""
The projection engine.

The chain, in order, for every player on a 2026 roster:

  OPPORTUNITY  Historical share of the team's targets and carries, weighted for
               recency and sample size, shrunk toward a depth-chart prior, and
               discounted if the player changed teams. Rookies start from the
               depth prior scaled by draft capital.

  NORMALISE    Shares are rescaled so each team's pass catchers sum to ~1.0 and
               its ball carriers sum to 1.0. This is what makes vacated volume
               redistribute automatically: when a team loses its WR1, everyone
               still on the roster is scaled up to fill the gap. It also stops
               the common failure where a team's projected targets exceed the
               targets it will actually throw.

  VOLUME       Share x the team's projected pass/rush attempts, which already
               carry the coaching and pace adjustments from models/context.py.

  EFFICIENCY   Yards per target/carry/attempt, shrunk toward positional means by
               opportunity count, nudged by Next Gen separation and yards after
               contact where we have them.

  TOUCHDOWNS   Regressed hardest of anything. A player's own TD rate is blended
               with the rate their usage implies, because touchdown rate is the
               noisiest input in fantasy and the biggest driver of both busts
               and breakouts.

  AVAILABILITY Multiplied by projected games from models/injury.py, with an
               efficiency discount for players returning from structural injury.

Every player also gets a floor, a ceiling, a confidence score and a plain-English
note, so a number can always be interrogated rather than taken on faith.
"""
from __future__ import annotations

from collections import defaultdict

from config import (
    AGE_CURVES, DEPTH_PRIOR_CARRY_SHARE, DEPTH_PRIOR_TARGET_SHARE, GAMES,
    HISTORY_SEASONS, NEW_TEAM_SAMPLE_DISCOUNT, NORMALISE_SCALE_BOUNDS,
    ROOKIE_CAPITAL_BOOST, SCORING_PRESETS, SEASON_WEIGHTS, SHRINK,
    ATTEMPTS_PER_DROPBACK, TARGETS_PER_ATTEMPT, TD_PERSONAL_WEIGHT,
    POSITION_RELIABILITY,
)
from models.context import build_team_context
from models.injury import build_injury_profiles

# Cold-start fallbacks only. The real shrink targets are measured from the
# loaded seasons by _pos_priors() below, so the model recalibrates itself as
# the league changes instead of drifting against numbers typed in once. The
# hand-written values were consistently high (RB yards per target 6.3 against a
# measured 5.79; quarterback yards per carry 5.4 against 4.40), which biased
# every low-sample player upward.
POS_YPT = {"WR": 8.1, "TE": 7.6, "RB": 6.3, "QB": 6.0}
POS_YPC = {"RB": 4.35, "QB": 5.4, "WR": 6.8, "TE": 3.0}
POS_CATCH = {"WR": 0.645, "TE": 0.700, "RB": 0.760, "QB": 0.6}
POS_FUM = {"QB": 0.0060, "RB": 0.0046, "WR": 0.0054, "TE": 0.0047}
QB_YPA = 7.05


def _pos_priors(conn) -> dict:
    """Measure league-wide efficiency by position over the loaded seasons."""
    acc = defaultdict(lambda: defaultdict(float))
    for r in conn.execute(
        "SELECT position, targets, receptions, receiving_yards, carries, "
        "rushing_yards, attempts, passing_yards, fumbles_lost FROM player_season "
        "WHERE season IN (?,?,?)", tuple(HISTORY_SEASONS)
    ):
        a = acc[r["position"]]
        a["tgt"] += r["targets"] or 0
        a["rec"] += r["receptions"] or 0
        a["recyd"] += r["receiving_yards"] or 0
        a["car"] += r["carries"] or 0
        a["ruyd"] += r["rushing_yards"] or 0
        a["att"] += r["attempts"] or 0
        a["pyd"] += r["passing_yards"] or 0
        a["fum"] += r["fumbles_lost"] or 0

    def pick(pos, num, den, fallback, floor_n):
        a = acc.get(pos)
        if not a or a[den] < floor_n:
            return fallback
        return a[num] / a[den]

    out = {"ypt": {}, "ypc": {}, "catch": {}, "fum": {}, "ypa": QB_YPA}
    for pos in ("QB", "RB", "WR", "TE"):
        out["ypt"][pos] = pick(pos, "recyd", "tgt", POS_YPT.get(pos, 7.0), 400)
        out["ypc"][pos] = pick(pos, "ruyd", "car", POS_YPC.get(pos, 4.2), 400)
        out["catch"][pos] = pick(pos, "rec", "tgt", POS_CATCH.get(pos, 0.65), 400)
        a = acc.get(pos)
        opp = (a["tgt"] + a["car"] + a["att"]) if a else 0
        out["fum"][pos] = (a["fum"] / opp) if opp > 2000 else POS_FUM.get(pos, 0.005)
    qb = acc.get("QB")
    if qb and qb["att"] > 2000:
        out["ypa"] = qb["pyd"] / qb["att"]
    return out


def _w_depth(pos: str, table: dict, depth: int) -> float:
    """Prior share for a player at a given depth-chart rank.

    Past the end of the table the prior decays geometrically rather than sitting
    at a flat fraction of the last entry. In August a roster carries ~90 players,
    so a team can list nine receivers; giving the ninth a meaningful prior
    inflates the team's share total and, after normalisation, quietly taxes the
    actual starters. Decay makes deep camp bodies round to nothing.
    """
    arr = table.get(pos)
    if not arr:
        return 0.002
    idx = max(1, depth or len(arr) + 2) - 1
    if idx < len(arr):
        return arr[idx]
    return arr[-1] * (0.45 ** (idx - len(arr) + 1))


def _rookie_boost(draft_number) -> float:
    n = draft_number or 999
    for cutoff, mult in ROOKIE_CAPITAL_BOOST:
        if n <= cutoff:
            return mult
    return 0.8


def _age_mult(pos: str, age) -> float:
    """Age adjustment applied to per-game EFFICIENCY only.

    The curves in config describe total production decline, and total production
    is efficiency x volume x games. Availability already falls with age in the
    injury model, and role loss shows up in the depth chart, so charging the full
    curve against efficiency as well would bill an ageing player three times for
    the same effect. A 29-year-old back was landing at ~0.69 of his prime purely
    through compounding. The curve is damped to the share of the decline that is
    genuinely per-touch.
    """
    curve = AGE_CURVES.get(pos)
    if not curve or not age:
        return 1.0
    a = int(round(age))
    lo, hi = min(curve), max(curve)
    raw = curve.get(max(lo, min(a, hi)), 1.0)
    return 1.0 - (1.0 - raw) * 0.55


def _shrink(obs, n, prior, k):
    return (n * obs + k * prior) / (n + k) if (n + k) > 0 else prior


# --------------------------------------------------------------- history ---
def _load_history(conn) -> dict:
    """Per-player, per-season usage and efficiency, team-relative."""
    team_tot = {}
    for r in conn.execute("SELECT * FROM team_season WHERE season IN (?,?,?)",
                          tuple(HISTORY_SEASONS)):
        dropbacks = (r["attempts"] or 0) + (r["sacks"] or 0)
        team_tot[(r["team"], r["season"])] = {
            "targets": max(r["targets"] or 0, 1.0),
            "carries": max(r["carries"] or 0, 1.0),
            "attempts": max(r["attempts"] or 0, 1.0),
            "dropbacks": max(dropbacks, 1.0),
            "pass_td": r["passing_tds"] or 0,
            "rush_td": r["rushing_tds"] or 0,
            "games": max(r["games"] or 17, 1),
        }

    hist = defaultdict(dict)
    for r in conn.execute("SELECT * FROM player_season"):
        tt = team_tot.get((r["team"], r["season"]))
        if not tt:
            continue
        g = r["games"] or 0
        if g <= 0:
            continue
        tgt, car, att = r["targets"] or 0, r["carries"] or 0, r["attempts"] or 0
        hist[r["gsis_id"]][r["season"]] = {
            "games": g, "team": r["team"], "pos": r["position"],
            "targets": tgt, "carries": car, "attempts": att,
            "tgt_share": tgt / tt["targets"],
            "carry_share": car / tt["carries"],
            "qb_share": ((att + (r["sacks"] or 0)) / tt["dropbacks"]),
            "air_share": r["air_yards_share"] or 0.0,
            "ypt": (r["receiving_yards"] or 0) / tgt if tgt else 0.0,
            "ypc": (r["rushing_yards"] or 0) / car if car else 0.0,
            "ypa": (r["passing_yards"] or 0) / att if att else 0.0,
            "catch": (r["receptions"] or 0) / tgt if tgt else 0.0,
            "rec_td_rate": (r["receiving_tds"] or 0) / tgt if tgt else 0.0,
            "rush_td_rate": (r["rushing_tds"] or 0) / car if car else 0.0,
            "pass_td_rate": (r["passing_tds"] or 0) / att if att else 0.0,
            "int_rate": (r["interceptions"] or 0) / att if att else 0.0,
            "rec_td_share": (r["receiving_tds"] or 0) / max(tt["pass_td"], 1.0),
            "rush_td_share": (r["rushing_tds"] or 0) / max(tt["rush_td"], 1.0),
            "fum_rate": (r["fumbles_lost"] or 0) / max(tgt + car + att, 1.0),
            # Per game, like every other key here: _weighted() sample-weights
            # its inputs, which only means anything for rates. This was the
            # single season TOTAL in a table of rates, so a player who scored
            # a conversion in a half season was credited with half the rate.
            "two_pt": (r["two_pt"] or 0) / g,
        }
    return hist, team_tot


def _team_td_rates(conn, team_tot) -> dict:
    """Recency-weighted pass/rush touchdown rates per team, regressed to league."""
    acc = defaultdict(lambda: {"ptd": 0.0, "att": 0.0, "rtd": 0.0, "car": 0.0, "w": 0.0})
    for (team, season), tt in team_tot.items():
        try:
            w = SEASON_WEIGHTS[HISTORY_SEASONS.index(season)]
        except ValueError:
            continue
        a = acc[team]
        a["ptd"] += w * tt["pass_td"]
        a["att"] += w * tt["attempts"]
        a["rtd"] += w * tt["rush_td"]
        a["car"] += w * tt["carries"]
        a["w"] += w
    lg_p = sum(a["ptd"] for a in acc.values()) / max(sum(a["att"] for a in acc.values()), 1)
    lg_r = sum(a["rtd"] for a in acc.values()) / max(sum(a["car"] for a in acc.values()), 1)
    out = {}
    for team, a in acc.items():
        p = a["ptd"] / a["att"] if a["att"] else lg_p
        r = a["rtd"] / a["car"] if a["car"] else lg_r
        # Team TD rate is only moderately sticky year to year.
        out[team] = {"pass_td_rate": 0.55 * p + 0.45 * lg_p,
                     "rush_td_rate": 0.55 * r + 0.45 * lg_r}
    out["_league"] = {"pass_td_rate": lg_p, "rush_td_rate": lg_r}
    return out


def _weighted(seasons: dict, key: str, cap_games: float = 13.0):
    """Recency- and sample-weighted mean of a per-season stat, plus effective n."""
    num = den = ngames = 0.0
    for w, season in zip(SEASON_WEIGHTS, HISTORY_SEASONS):
        row = seasons.get(season)
        if not row:
            continue
        sample = min(row["games"] / cap_games, 1.0)
        weight = w * sample
        num += weight * row[key]
        den += weight
        ngames += w * row["games"]
    return (num / den if den else None), ngames


# ------------------------------------------------------------ projection ---
def project(conn, scoring_key: str = "half_ppr", team_ctx=None, injuries=None) -> list:
    scoring = SCORING_PRESETS[scoring_key]
    ctx = team_ctx or build_team_context(conn)
    inj = injuries if injuries is not None else build_injury_profiles(conn)
    hist, team_tot = _load_history(conn)
    td_rates = _team_td_rates(conn, team_tot)
    pri = _pos_priors(conn)

    ngs = {r["gsis_id"]: r for r in conn.execute(
        "SELECT * FROM ngs_rec WHERE season=?", (HISTORY_SEASONS[0],))}
    advr = {}
    for r in conn.execute("SELECT * FROM adv_rush WHERE season=?", (HISTORY_SEASONS[0],)):
        advr[r["pfr_id"]] = r

    players = conn.execute(
        "SELECT * FROM players WHERE team IS NOT NULL AND team!='' "
        "AND position IN ('QB','RB','WR','TE','K','DST')"
    ).fetchall()

    # ---------------------------------------------------- stage 1: shares --
    prelim = []
    for p in players:
        pos, team = p["position"], p["team"]
        if team not in ctx:
            continue
        seasons = hist.get(p["gsis_id"], {}) if p["gsis_id"] else {}
        is_rookie = not seasons and (p["years_exp"] or 0) <= 1
        # An unlisted player with a production record is a real contributor whose
        # depth chart simply has not published; an unlisted player with no record
        # is a camp body and should be priced as one.
        if p["depth_order"]:
            depth = p["depth_order"]
        elif pos == "QB":
            depth = 1 if seasons else 3
        elif seasons:
            depth = 3
        else:
            depth = len(DEPTH_PRIOR_TARGET_SHARE.get(pos, [0])) + 2

        # Did they move? Their old role transfers only partially.
        last_team = None
        for season in HISTORY_SEASONS:
            if season in seasons:
                last_team = seasons[season]["team"]
                break
        moved = bool(last_team and last_team != team)

        tgt_prior = _w_depth(pos, DEPTH_PRIOR_TARGET_SHARE, depth)
        car_prior = _w_depth(pos, DEPTH_PRIOR_CARRY_SHARE, depth)
        if is_rookie:
            boost = _rookie_boost(p["draft_number"])
            tgt_prior *= boost
            car_prior *= boost

        obs_tgt, n_tgt = _weighted(seasons, "tgt_share")
        obs_car, n_car = _weighted(seasons, "carry_share")
        if moved:
            n_tgt *= NEW_TEAM_SAMPLE_DISCOUNT
            n_car *= NEW_TEAM_SAMPLE_DISCOUNT

        tgt_share = _shrink(obs_tgt if obs_tgt is not None else tgt_prior, n_tgt,
                            tgt_prior, SHRINK["target_share_games"])
        car_share = _shrink(obs_car if obs_car is not None else car_prior, n_car,
                            car_prior, SHRINK["carry_share_games"])

        # Quarterbacks: the starter takes nearly all the dropbacks.
        qb_share = 0.0
        if pos == "QB":
            obs_qb, n_qb = _weighted(seasons, "qb_share")
            prior_qb = 0.90 if depth <= 1 else 0.10 if depth == 2 else 0.02
            # Draft capital overrides an August depth chart for quarterbacks.
            # ROOKIE_CAPITAL_BOOST is applied to target and carry shares but was
            # never applied here, so a first-overall pick listed behind a 38-year-
            # old veteran projected as a permanent backup. High picks start, and
            # usually within the season - a floor on the prior reflects the
            # probability of a mid-season takeover without asserting a week.
            if is_rookie and depth > 1:
                cap = p["draft_number"] or 999
                floor_share = 0.45 if cap <= 15 else 0.28 if cap <= 45 else 0.0
                prior_qb = max(prior_qb, floor_share)
            qb_share = _shrink(obs_qb if obs_qb is not None else prior_qb,
                               n_qb * (0.5 if moved else 1.0), prior_qb, 8.0)

        prelim.append({
            "p": p, "pos": pos, "team": team, "seasons": seasons, "depth": depth,
            "rookie": is_rookie, "moved": moved, "last_team": last_team,
            "tgt_share_raw": tgt_share, "car_share_raw": car_share,
            "qb_share_raw": qb_share, "n_tgt": n_tgt, "n_car": n_car,
            "games": inj.get(p["player_id"], {}).get("games", GAMES * 0.87),
        })

    # ------------------------------------------- stage 2: normalise shares --
    # A team can only throw so many passes and hand off so many times, so every
    # team's shares have to add up. Two things have to be right here.
    #
    # WHAT adds up: a share is a share OF THE GAMES A PLAYER ACTUALLY PLAYS. The
    # constraint is therefore sum(share x games/17) = 1, not sum(share) = 1. The
    # difference is not pedantic - normalising raw shares deletes the targets and
    # carries belonging to every game anyone misses, which drained ~14% of the
    # league's opportunity into thin air and flattened the entire player pool.
    # Handled this way, a back who misses three games has those carries handed
    # to his backup instead of erased.
    #
    # WHO absorbs the correction: scaling everyone by the same factor is the
    # obvious move and it is wrong. It taxes a proven 28%-target-share tight end
    # exactly as hard as the eighth receiver on a 90-man August roster, purely
    # because his team signed depth. The correction is instead allocated in
    # proportion to each player's UNCERTAINTY: we know a lot about the
    # incumbent's role and little about the journeyman's, so the journeyman
    # gives up the share.
    for kind, raw_key, norm_key, total, n_key in (
        ("rec", "tgt_share_raw", "tgt_share", 0.98, "n_tgt"),
        ("rush", "car_share_raw", "car_share", 1.00, "n_car"),
        ("qb", "qb_share_raw", "qb_share", 1.00, "n_tgt"),
    ):
        by_team = defaultdict(list)
        for e in prelim:
            if kind == "rec" and e["pos"] not in ("RB", "WR", "TE"):
                continue
            if kind == "rush" and e["pos"] not in ("QB", "RB", "WR", "TE"):
                continue
            if kind == "qb" and e["pos"] != "QB":
                continue
            by_team[e["team"]].append(e)

        for team, group in by_team.items():
            # Availability weight: the fraction of the season each player is on
            # the field. Shares are pooled in these units so the team total is
            # conserved across everyone's missed time.
            av = [min(max(e["games"] / GAMES, 0.05), 1.0) for e in group]
            ssum = sum(e[raw_key] * a for e, a in zip(group, av))
            if ssum <= 0:
                for e in group:
                    e[norm_key] = 0.0
                continue

            excess = ssum - total
            if excess > 0:
                # Weight by contributed share x uncertainty. Uncertainty falls as
                # a player accumulates real games in the role.
                weights = [e[raw_key] * a / (1.0 + (e.get(n_key, 0.0) / 8.0))
                           for e, a in zip(group, av)]
                wsum = sum(weights)
                if wsum > 0:
                    for e, a, w in zip(group, av, weights):
                        cut = excess * (w / wsum) / a
                        # Never strip more than 45% of a player's own projection;
                        # beyond that the correction has stopped being a
                        # correction and started being a different model.
                        e[norm_key] = max(e[raw_key] - cut, e[raw_key] * 0.55)
                else:
                    for e in group:
                        e[norm_key] = e[raw_key]
            else:
                for e in group:
                    e[norm_key] = e[raw_key]

            # Final exact rescale so the team's pool balances, bounded so a
            # missing depth chart cannot invent a WR1 out of a camp body.
            resid = sum(e[norm_key] * a for e, a in zip(group, av))
            if resid > 0:
                lo, hi = NORMALISE_SCALE_BOUNDS
                k = min(max(total / resid, lo), hi)
                for e in group:
                    e[norm_key] *= k

    # ------------------------------------------------- stage 3: box score --
    out = []
    for e in prelim:
        p, pos, team = e["p"], e["pos"], e["team"]
        c = ctx[team]
        seasons = e["seasons"]
        prof = inj.get(p["player_id"], {})
        games = prof.get("games", GAMES * 0.85)
        eff_mult = prof.get("eff_mult", 1.0)
        age_mult = _age_mult(pos, p["age"])
        tt = td_rates.get(team, td_rates["_league"])

        dropbacks_pg = c["pass_att_pg"]
        team_targets_pg = dropbacks_pg * ATTEMPTS_PER_DROPBACK * TARGETS_PER_ATTEMPT
        team_carries_pg = c["rush_att_pg"]

        notes = []
        pass_att = pass_yd = pass_td = ints = 0.0
        carries = rush_yd = rush_td = 0.0
        targets = receptions = rec_yd = rec_td = 0.0

        if pos in ("QB", "RB", "WR", "TE"):
            # ---- receiving
            if pos in ("RB", "WR", "TE"):
                targets = team_targets_pg * e["tgt_share"] * games
                obs_ypt, _ = _weighted(seasons, "ypt")
                n_tgt_opp = sum(seasons[s]["targets"] for s in seasons)
                base_ypt = pri["ypt"].get(pos, POS_YPT.get(pos, 7.0))
                ypt = _shrink(obs_ypt if obs_ypt is not None else base_ypt,
                              n_tgt_opp, base_ypt, SHRINK["ypt_targets"])
                # Next Gen separation/YAC-over-expected is a real, if small, edge.
                g = ngs.get(p["gsis_id"])
                if g and g["avg_separation"]:
                    sep_adj = 1.0 + max(-0.06, min(0.06, (g["avg_separation"] - 2.85) * 0.035))
                    yac_adj = 1.0 + max(-0.05, min(0.05, (g["yac_above_exp"] or 0) * 0.045))
                    ypt *= sep_adj * yac_adj
                    if g["avg_separation"] >= 3.3:
                        notes.append(f"Elite separation ({g['avg_separation']:.2f} yd)")
                obs_catch, _ = _weighted(seasons, "catch")
                base_catch = pri["catch"].get(pos, POS_CATCH.get(pos, 0.65))
                catch = _shrink(obs_catch if obs_catch is not None else base_catch,
                                n_tgt_opp, base_catch, SHRINK["ypt_targets"])
                receptions = targets * catch
                rec_yd = targets * ypt * eff_mult * age_mult

                # Receiving TDs: usage-implied vs. personal rate.
                obs_air, _ = _weighted(seasons, "air_share")
                air_share = obs_air if obs_air is not None else e["tgt_share"]
                team_pass_td = dropbacks_pg * games * tt["pass_td_rate"]
                implied_td = team_pass_td * (0.55 * e["tgt_share"] + 0.45 * air_share)
                obs_rate, _ = _weighted(seasons, "rec_td_rate")
                personal_td = targets * (obs_rate if obs_rate is not None else 0.0)
                w = TD_PERSONAL_WEIGHT[pos] * min(n_tgt_opp / SHRINK["td_rate_opp"], 1.0)
                rec_td = (w * personal_td + (1 - w) * implied_td) * age_mult

            # ---- rushing
            if pos in ("QB", "RB", "WR", "TE"):
                carries = team_carries_pg * e["car_share"] * games
                obs_ypc, _ = _weighted(seasons, "ypc")
                n_car_opp = sum(seasons[s]["carries"] for s in seasons)
                base_ypc = pri["ypc"].get(pos, POS_YPC.get(pos, 4.2))
                ypc = _shrink(obs_ypc if obs_ypc is not None else base_ypc,
                              n_car_opp, base_ypc, SHRINK["ypc_carries"])
                a = advr.get(p["pfr_id"])
                if a and a["carries"] and a["carries"] > 60 and a["yac_att"]:
                    # Yards after contact is far more repeatable than raw YPC,
                    # which is heavily blocking-dependent.
                    ypc *= 1.0 + max(-0.05, min(0.06, (a["yac_att"] - 2.9) * 0.05))
                    if a["yac_att"] >= 3.4:
                        notes.append(f"Creates yardage after contact ({a['yac_att']:.1f}/att)")
                rush_yd = carries * ypc * eff_mult * age_mult

                team_rush_td = team_carries_pg * games * tt["rush_td_rate"]
                obs_tds, _ = _weighted(seasons, "rush_td_share")
                td_share = obs_tds if obs_tds is not None else e["car_share"]
                implied = team_rush_td * (0.62 * e["car_share"] + 0.38 * td_share)
                obs_rate, _ = _weighted(seasons, "rush_td_rate")
                personal = carries * (obs_rate if obs_rate is not None else 0.0)
                w = TD_PERSONAL_WEIGHT.get(pos, 0.3) * min(n_car_opp / SHRINK["td_rate_opp"], 1.0)
                rush_td = w * personal + (1 - w) * implied

            # ---- passing
            if pos == "QB":
                pass_att = dropbacks_pg * e["qb_share"] * games * ATTEMPTS_PER_DROPBACK
                obs_ypa, _ = _weighted(seasons, "ypa")
                n_att = sum(seasons[s]["attempts"] for s in seasons)
                base_ypa = pri["ypa"]
                ypa = _shrink(obs_ypa if obs_ypa is not None else base_ypa, n_att,
                              base_ypa, SHRINK["ypa_attempts"])
                pass_yd = pass_att * ypa * age_mult
                obs_ptd, _ = _weighted(seasons, "pass_td_rate")
                ptd_rate = _shrink(obs_ptd if obs_ptd is not None else tt["pass_td_rate"],
                                   n_att, tt["pass_td_rate"], 240.0)
                pass_td = pass_att * ptd_rate
                obs_int, _ = _weighted(seasons, "int_rate")
                ints = pass_att * _shrink(obs_int if obs_int is not None else 0.025,
                                          n_att, 0.025, 260.0)

        elif pos == "K":
            # Kicker scoring tracks team scoring closely and nothing else does.
            ppg = c.get("exp_ppg") or 22.5
            pts_pg = 8.2 * (ppg / 22.5)
            return_row = _finish(p, pos, team, c, prof, games, pts_pg * games,
                                 notes + ["Kicker output tracks team scoring; "
                                          "inherently high variance"], e, scoring_key)
            out.append(return_row)
            continue

        elif pos == "DST":
            pa = _dst_points_allowed(conn, team)
            # Anchored so a league-average defense (~21 points allowed) lands
            # near 8 a game, which is where real DST scoring actually sits.
            pts_pg = min(max(8.2 - 0.30 * (pa - 21.0), 3.0), 13.0)
            return_row = _finish(p, pos, team, c, prof, GAMES, pts_pg * GAMES,
                                 notes + [f"Based on {HISTORY_SEASONS[0]} points allowed "
                                          f"({pa:.1f}/g); defenses are volatile year to year"],
                                 e, scoring_key)
            out.append(return_row)
            continue

        # ---- fantasy points
        # Fumbles: shrink the player's own rate toward the positional rate by
        # opportunity count. The previous flat 0.55 multiplier was an
        # unexplained haircut that under-counted fumbles for genuinely careless
        # ball carriers and invented them for players who have never fumbled.
        opp = targets + carries + pass_att
        obs_fum, _ = _weighted(seasons, "fum_rate")
        base_fum = pri["fum"].get(pos, 0.005)
        n_opp = sum(seasons[s2]["targets"] + seasons[s2]["carries"] + seasons[s2]["attempts"]
                    for s2 in seasons)
        fum_rate = _shrink(obs_fum if obs_fum is not None else base_fum,
                           n_opp, base_fum, 240.0)
        fum = fum_rate * opp

        # Two-point conversions: small, but the scoring config has always defined
        # them and the model was silently ignoring it. Scaled by projected games
        # so a part-season player is not credited with a full year of them.
        two_pt_pg, _ = _weighted(seasons, "two_pt")
        two_pt = (two_pt_pg or 0.0) * games * scoring.get("rush_2pt", 2.0)

        pts = (
            pass_yd * scoring["pass_yd"] + pass_td * scoring["pass_td"]
            + ints * scoring["interception"]
            + rush_yd * scoring["rush_yd"] + rush_td * scoring["rush_td"]
            + rec_yd * scoring["rec_yd"] + rec_td * scoring["rec_td"]
            + receptions * (scoring["rec"] + (scoring["te_premium"] if pos == "TE" else 0.0))
            + fum * scoring["fumble_lost"]
            + two_pt
        )

        row = _finish(p, pos, team, c, prof, games, pts, notes, e, scoring_key)
        row.update({
            "pass_att": pass_att, "pass_yd": pass_yd, "pass_td": pass_td, "ints": ints,
            "carries": carries, "rush_yd": rush_yd, "rush_td": rush_td,
            "targets": targets, "receptions": receptions, "rec_yd": rec_yd, "rec_td": rec_td,
            "tgt_share": e.get("tgt_share", 0.0), "car_share": e.get("car_share", 0.0),
        })
        out.append(row)

    _apply_reliability(out)
    return out


def _apply_reliability(rows: list) -> None:
    """Shrink positions whose year-to-year signal is weak toward their own mean.

    Kicker and defense projections separate the field far more confidently than
    the evidence supports. Left alone, that false confidence hands them large
    VORP and floats them into the first half of the draft board, which is exactly
    the mistake the site exists to prevent. Shrinking collapses the spread
    without pretending they all score the same.
    """
    by_pos = defaultdict(list)
    for r in rows:
        by_pos[r["position"]].append(r)
    for pos, arr in by_pos.items():
        keep = POSITION_RELIABILITY.get(pos, 1.0)
        if keep >= 0.999 or not arr:
            continue
        mean = sum(r["points"] for r in arr) / len(arr)
        for r in arr:
            for key in ("points", "floor", "ceiling"):
                r[key] = mean + (r[key] - mean) * keep
            r["ppg"] = r["points"] / r["games"] if r["games"] else 0.0


def _dst_points_allowed(conn, team) -> float:
    row = conn.execute(
        "SELECT AVG(CASE WHEN home_team=? THEN away_score ELSE home_score END) pa "
        "FROM schedule WHERE season=? AND home_score IS NOT NULL AND (home_team=? OR away_team=?)",
        (team, HISTORY_SEASONS[0], team, team),
    ).fetchone()
    return (row["pa"] if row and row["pa"] is not None else 22.0)


def _finish(p, pos, team, c, prof, games, pts, notes, e, scoring_key) -> dict:
    """Common tail: confidence, floor/ceiling, breakout, narrative note."""
    games = max(games, 0.1)
    ppg = pts / games if games else 0.0

    # Confidence: how much do we actually know about this role?
    conf = 0.5
    n_seasons = len(e.get("seasons", {}))
    conf += min(n_seasons, 3) * 0.11
    if e.get("moved"):
        conf -= 0.10
    if e.get("rookie"):
        conf -= 0.16
    if c.get("caller_changed"):
        conf -= 0.07
    if (e.get("depth") or 9) <= 1:
        conf += 0.06
    if prof.get("risk") == "high":
        conf -= 0.08
    conf = max(0.12, min(0.97, conf))

    # Spread widens as confidence falls and injury risk rises.
    vol = {"QB": 0.26, "RB": 0.38, "WR": 0.36, "TE": 0.34, "K": 0.30, "DST": 0.40}.get(pos, 0.35)
    spread = vol * (1.35 - conf) + {"high": 0.12, "medium": 0.05}.get(prof.get("risk"), 0.0)
    ceiling = pts * (1.0 + spread)
    floor = pts * max(0.15, 1.0 - spread * 1.15)

    # Breakout: young, ascending role, on a team with volume to give away.
    breakout = 0.0
    age = p["age"] or 27
    if pos != "DST":
        if age <= 24:
            breakout += 0.32
        elif age <= 26:
            breakout += 0.18
        if (e.get("depth") or 9) <= 2:
            breakout += 0.16
        if pos in ("WR", "TE"):
            breakout += min(c.get("vacated_target_pct", 0.0) * 1.1, 0.30)
        elif pos == "RB":
            breakout += min(c.get("vacated_carry_pct", 0.0) * 1.1, 0.30)
        if (p["years_exp"] or 0) in (1, 2):
            breakout += 0.12
        breakout = min(breakout, 1.0)

    if c.get("caller_changed"):
        notes = list(notes) + [c.get("note", "")]
    if e.get("moved") and e.get("last_team"):
        notes = list(notes) + [f"New team - was with {e['last_team']}"]
    if pos in ("WR", "TE") and c.get("vacated_target_pct", 0) > 0.22:
        notes = list(notes) + [
            f"{c['vacated_target_pct'] * 100:.0f}% of {team}'s targets vacated from last season"]
    if pos == "RB" and c.get("vacated_carry_pct", 0) > 0.28:
        notes = list(notes) + [
            f"{c['vacated_carry_pct'] * 100:.0f}% of {team}'s carries vacated from last season"]

    return {
        "player_id": p["player_id"], "name": p["name"], "position": pos, "team": team,
        "age": p["age"], "years_exp": p["years_exp"], "depth": e.get("depth"),
        "headshot": p["headshot"], "bye": c.get("bye"),
        "games": games, "points": pts, "ppg": ppg,
        "floor": floor, "ceiling": ceiling, "confidence": conf, "breakout": breakout,
        "availability": prof.get("availability", 1.0),
        "injury_risk": prof.get("risk", "low"),
        "injury_note": prof.get("note", ""),
        "injury_status": prof.get("status", ""),
        "coach_note": c.get("note", ""), "caller": c.get("caller", ""),
        "sos": c.get("sos", 1.0),
        "notes": "; ".join(n for n in notes if n),
        "pass_att": 0.0, "pass_yd": 0.0, "pass_td": 0.0, "ints": 0.0,
        "carries": 0.0, "rush_yd": 0.0, "rush_td": 0.0,
        "targets": 0.0, "receptions": 0.0, "rec_yd": 0.0, "rec_td": 0.0,
        "tgt_share": 0.0, "car_share": 0.0,
    }
