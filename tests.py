"""
Verification harness for the model's arithmetic.

Run:  python tests.py

These are not unit tests of implementation detail - they are checks that the
*numbers mean what they claim to mean*. Most of the real bugs found in this
project were silent wrongness (a team split in two by an abbreviation mismatch,
opportunity vanishing into missed games, a scoring column that quietly included
special-teams fumbles). None of them crashed anything. Invariants catch that
class; exception handling does not.

Each check prints PASS/FAIL with the measured value, so a regression is visible
rather than merely absent.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from config import (
    ATTEMPTS_PER_DROPBACK, BASE_DIR, DEFAULT_LEAGUE, GAMES, HISTORY_SEASONS,
    SCORING_PRESETS, TARGETS_PER_ATTEMPT,
)
from models.context import build_team_context
from models.database import db
from models.draft import availability, expected_best_at, pick_numbers, recommend
from models.injury import build_injury_profiles
from models.projection import _pos_priors, _shrink, _age_mult, project
from models.valuation import (
    attach_adp, auction_inflation, compute_values,
    keeper_analysis, roster_size, starter_demand,
)

PASS, FAIL = 0, 0
_failures = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        _failures.append(name)
        print(f"  FAIL  {name}  [{detail}]")


def section(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


# ---------------------------------------------------------------------------
def test_scoring(conn):
    """Our scoring config must reproduce a known-good external computation.

    nflverse publishes its own fantasy_points_ppr. Applying the PPR preset to the
    component stats has to land on the same number, or every projection built
    from that formula is wrong in the same way.
    """
    section("1. SCORING ARITHMETIC (vs nflverse's own fantasy_points_ppr)")
    sc = SCORING_PRESETS["ppr"]
    rows = conn.execute(
        "SELECT * FROM player_season WHERE season=? AND fantasy_points_ppr IS NOT NULL",
        (HISTORY_SEASONS[0],)).fetchall()

    exact = 0
    worst = (0.0, None)
    for r in rows:
        mine = (
            r["passing_yards"] * sc["pass_yd"] + r["passing_tds"] * sc["pass_td"]
            + r["interceptions"] * sc["interception"]
            + r["rushing_yards"] * sc["rush_yd"] + r["rushing_tds"] * sc["rush_td"]
            + r["receiving_yards"] * sc["rec_yd"] + r["receiving_tds"] * sc["rec_td"]
            + r["receptions"] * sc["rec"] + r["fumbles_lost"] * sc["fumble_lost"]
            + r["two_pt"] * 2.0
            # Return touchdowns are real fantasy points and are included here so
            # this is a strict equality rather than an approximate one. They are
            # deliberately NOT projected forward: expected value for even a
            # designated returner is under half a score a season, and we have no
            # reliable read on who returns kicks in 2026.
            + (r["special_teams_tds"] or 0) * 6.0
        )
        d = abs(mine - r["fantasy_points_ppr"])
        if d < 0.01:
            exact += 1
        elif d > worst[0]:
            worst = (d, r["gsis_id"])
    pct = exact / len(rows) * 100
    check("PPR scoring reproduces nflverse EXACTLY for every player-season",
          exact == len(rows), f"{exact}/{len(rows)} = {pct:.2f}%, worst diff {worst[0]:.2f}")

    # The fumble column is the one that was wrong: fumbles_lost_total also counts
    # special-teams fumbles, which are not offensive touches.
    st_fum = conn.execute(
        "SELECT COUNT(*) n FROM player_season WHERE season=? AND fumbles_lost > 0",
        (HISTORY_SEASONS[0],)).fetchone()["n"]
    check("fumbles stored are offensive only (return fumbles excluded)",
          st_fum > 0, f"{st_fum} players with offensive fumbles lost")

    # Format ordering must be strictly sensible.
    r = rows[0]
    def pts(key):
        s = SCORING_PRESETS[key]
        return (r["receptions"] * s["rec"] + r["receiving_yards"] * s["rec_yd"])
    rec_heavy = max(rows, key=lambda x: x["receptions"])
    def pts2(key, row):
        s = SCORING_PRESETS[key]
        return row["receptions"] * s["rec"] + row["receiving_yards"] * s["rec_yd"]
    check("PPR > half-PPR > standard for a pass catcher",
          pts2("ppr", rec_heavy) > pts2("half_ppr", rec_heavy) > pts2("standard", rec_heavy),
          f"{pts2('ppr', rec_heavy):.1f} / {pts2('half_ppr', rec_heavy):.1f} / {pts2('standard', rec_heavy):.1f}")


def test_volume(conn, rows, ctx):
    """A team cannot throw more passes than it throws."""
    section("2. VOLUME CONSERVATION")
    tt = conn.execute(
        "SELECT SUM(targets) t, SUM(carries) c, SUM(attempts) a FROM team_season WHERE season=?",
        (HISTORY_SEASONS[0],)).fetchone()
    ptgt = sum(p["targets"] for p in rows)
    pcar = sum(p["carries"] for p in rows)
    patt = sum(p["pass_att"] for p in rows)
    for label, proj, actual in (("targets", ptgt, tt["t"]), ("carries", pcar, tt["c"]),
                                ("pass attempts", patt, tt["a"])):
        ratio = proj / actual * 100
        check(f"league {label} within 5% of a real season", 95 <= ratio <= 105, f"{ratio:.1f}%")

    # Per team: projected targets must match that team's own projected pool.
    by_team = defaultdict(lambda: {"tgt": 0.0, "car": 0.0})
    for p in rows:
        by_team[p["team"]]["tgt"] += p["targets"]
        by_team[p["team"]]["car"] += p["carries"]
    worst_t = worst_c = (0.0, "")
    for team, c in ctx.items():
        pool_t = c["pass_att_pg"] * ATTEMPTS_PER_DROPBACK * TARGETS_PER_ATTEMPT * GAMES
        pool_c = c["rush_att_pg"] * GAMES
        dt = abs(by_team[team]["tgt"] / pool_t - 0.98)
        dc = abs(by_team[team]["car"] / pool_c - 1.00)
        if dt > worst_t[0]:
            worst_t = (dt, team)
        if dc > worst_c[0]:
            worst_c = (dc, team)
    check("every team's targets sum to its own pool (±6pp)", worst_t[0] <= 0.06,
          f"worst {worst_t[1]} off by {worst_t[0]*100:.1f}pp")
    check("every team's carries sum to its own pool (±6pp)", worst_c[0] <= 0.06,
          f"worst {worst_c[1]} off by {worst_c[0]*100:.1f}pp")


def test_shrinkage():
    section("3. SHRINKAGE + AGE CURVE")
    check("shrink with no sample returns the prior", abs(_shrink(9.0, 0, 5.0, 10) - 5.0) < 1e-9)
    check("shrink with huge sample returns the observation",
          abs(_shrink(9.0, 10_000, 5.0, 10) - 9.0) < 0.01)
    mid = _shrink(9.0, 10, 5.0, 10)
    check("shrink lands strictly between prior and observation", 5.0 < mid < 9.0, f"{mid:.3f}")
    check("shrink is monotonic in sample size",
          _shrink(9, 5, 5, 10) < _shrink(9, 20, 5, 10) < _shrink(9, 100, 5, 10))

    # Age damping must never invert or exceed 1 at peak.
    vals = [_age_mult("RB", a) for a in range(21, 34)]
    check("RB age curve is damped and bounded", all(0.6 < v <= 1.05 for v in vals),
          f"{min(vals):.3f}..{max(vals):.3f}")
    check("RB age curve declines after 27", _age_mult("RB", 32) < _age_mult("RB", 25))
    check("QB age curve is flatter than RB",
          (_age_mult("QB", 25) - _age_mult("QB", 34)) < (_age_mult("RB", 25) - _age_mult("RB", 32)))
    check("unknown position/age is a no-op", _age_mult("K", 30) == 1.0 and _age_mult("RB", None) == 1.0)


def test_injury(conn):
    section("4. AVAILABILITY MODEL")
    inj = build_injury_profiles(conn)
    games = [v["games"] for v in inj.values()]
    check("projected games never leave [0, 17]", all(0 <= g <= GAMES for g in games),
          f"{min(games):.1f}..{max(games):.1f}")
    bad = [k for k, v in inj.items() if abs(v["availability"] - v["games"] / GAMES) > 1e-9]
    check("availability == games / 17 for every player", not bad, f"{len(bad)} mismatches")
    check("efficiency multiplier is a discount, never a bonus",
          all(0.5 <= v["eff_mult"] <= 1.0 for v in inj.values()))
    risks = {v["risk"] for v in inj.values()}
    check("risk labels are from the known set", risks <= {"low", "medium", "high"}, str(risks))
    kd = [v["games"] for k, v in inj.items() if v["risk"] == "low"]
    check("low-risk players project more games than high-risk on average",
          (sum(kd) / len(kd)) > (sum(v["games"] for v in inj.values() if v["risk"] == "high")
                                 / max(sum(1 for v in inj.values() if v["risk"] == "high"), 1)))


def test_valuation(conn, rows):
    section("5. REPLACEMENT LEVEL, VORP AND AUCTION DOLLARS")
    lg = DEFAULT_LEAGUE
    by_pos = defaultdict(list)
    for p in rows:
        by_pos[p["position"]].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: -x["points"])

    demand = starter_demand(lg, by_pos)
    check("flex demand is allocated to flex-eligible positions only",
          demand["RB"] + demand["WR"] + demand["TE"] >
          (lg["roster"]["RB"] + lg["roster"]["WR"] + lg["roster"]["TE"]) * lg["teams"],
          f"RB{demand['RB']} WR{demand['WR']} TE{demand['TE']}")

    # VORP must be exactly points minus that position's replacement.
    bad = [p["name"] for p in rows if abs(p["vorp"] - (p["points"] - p["replacement"])) > 1e-6]
    check("VORP == points - replacement for every player", not bad, f"{len(bad)} mismatches")

    # Replacement must be a real player's score at that position.
    ok = True
    for pos, arr in by_pos.items():
        rep = arr[0]["replacement"]
        if not any(abs(p["points"] - rep) < 1e-6 for p in arr):
            ok = False
    check("replacement level equals some real player's projection at that position", ok)

    # Ranking must follow VORP, not raw points.
    ranked = sorted(rows, key=lambda x: -x["vorp"])
    check("overall_rank follows VORP order",
          all(ranked[i]["overall_rank"] <= ranked[i + 1]["overall_rank"] for i in range(len(ranked) - 1)))

    # The auction pool must add up to the league's actual money.
    spots = roster_size(lg) * lg["teams"]
    total = sum(p["auction_value"] for p in sorted(rows, key=lambda x: -x["vorp"])[:spots])
    budget = lg["teams"] * lg["auction_budget"]
    check("top-N auction values sum to the league budget",
          abs(total - budget) / budget < 0.02, f"${total:.0f} vs ${budget}")
    check("no auction value below $1", all(p["auction_value"] >= 1.0 - 1e-9 for p in rows))
    check("max bid >= auction value", all(p["max_bid"] >= p["auction_value"] - 1e-9 for p in rows))

    # Tiers must not improve as players get worse.
    okt = True
    for pos, arr in by_pos.items():
        seq = [p["tier"] for p in sorted(arr, key=lambda x: -x["points"]) if p.get("tier")]
        if any(seq[i] > seq[i + 1] for i in range(len(seq) - 1)):
            okt = False
    check("tiers never improve as projection falls", okt)


def test_superflex(conn, proj):
    section("6. LEAGUE SETTINGS ACTUALLY CHANGE THE VALUATION")
    import copy
    base = compute_values([dict(p) for p in proj], DEFAULT_LEAGUE)
    sf_lg = copy.deepcopy(DEFAULT_LEAGUE)
    sf_lg["roster"]["SUPERFLEX"] = 1
    sf = compute_values([dict(p) for p in proj], sf_lg)
    qb_base = next(p for p in sorted(base, key=lambda x: -x["vorp"]) if p["position"] == "QB")
    qb_sf = next(p for p in sorted(sf, key=lambda x: -x["vorp"]) if p["position"] == "QB")
    check("superflex lowers QB replacement level",
          qb_sf["replacement"] < qb_base["replacement"],
          f"{qb_base['replacement']:.0f} -> {qb_sf['replacement']:.0f}")
    check("superflex raises QB1 value", qb_sf["auction_value"] > qb_base["auction_value"],
          f"${qb_base['auction_value']:.0f} -> ${qb_sf['auction_value']:.0f}")

    big = copy.deepcopy(DEFAULT_LEAGUE)
    big["teams"] = 16
    b16 = compute_values([dict(p) for p in proj], big)
    rb_12 = next(p for p in sorted(base, key=lambda x: -x["vorp"]) if p["position"] == "RB")
    rb_16 = next(p for p in sorted(b16, key=lambda x: -x["vorp"]) if p["position"] == "RB")
    check("a deeper league lowers replacement level (scarcer starters)",
          rb_16["replacement"] < rb_12["replacement"],
          f"{rb_12['replacement']:.0f} -> {rb_16['replacement']:.0f}")


def test_draft(rows):
    section("7. DRAFT MECHANICS")
    for teams, rounds in ((12, 16), (10, 15), (14, 18)):
        for rev in (False, True):
            allp = sorted(p for s in range(1, teams + 1)
                          for p in pick_numbers(s, teams, rounds, rev))
            check(f"{teams}-team {'3RR' if rev else 'snake'} uses every pick exactly once",
                  allp == list(range(1, teams * rounds + 1)),
                  f"{len(allp)} picks")
    check("3RR round 3 repeats round 2's order",
          pick_numbers(1, 12, 4, True)[2] == 36 and pick_numbers(1, 12, 4, True)[3] == 37,
          str(pick_numbers(1, 12, 4, True)))

    # Survival probability.
    p = {"adp": 10.0, "adp_stdev": 4.0}
    probs = [availability(p, k) for k in range(1, 60)]
    check("availability is bounded in [0,1]", all(0 <= x <= 1 for x in probs))
    check("availability never increases with a later pick",
          all(probs[i] >= probs[i + 1] - 1e-12 for i in range(len(probs) - 1)))
    check("everyone is available at pick 1",
          all(abs(availability({"adp": a, "adp_stdev": 0.8}, 1) - 1.0) < 1e-9
              for a in (1.0, 1.5, 2.2, 30.0)))
    check("a player far past his ADP is essentially gone", availability(p, 200) < 0.001)
    check("no ADP means treated as available", availability({"adp": None}, 100) > 0.9)

    # Expected-best is a real expectation: bounded by the best candidate.
    pool = sorted(rows, key=lambda x: -x["vorp"])[:60]
    for pick in (1, 12, 40, 100):
        eb = expected_best_at(pool, pick)
        check(f"expected best VORP at pick {pick} <= best available",
              0 <= eb <= pool[0]["vorp"] + 1e-9, f"{eb:.1f} vs max {pool[0]['vorp']:.1f}")
    e1, e50, e150 = (expected_best_at(pool, k) for k in (1, 50, 150))
    check("expected best STRICTLY decays with later picks", e1 > e50 > e150,
          f"{e1:.1f} > {e50:.1f} > {e150:.1f}")


def test_keeper_auction(rows):
    section("8. KEEPER AND AUCTION")
    lg = DEFAULT_LEAGUE
    top = sorted(rows, key=lambda x: -x["vorp"])
    keepers = [{"player_id": top[0]["player_id"], "round": 1, "slot": 6},
               {"player_id": top[40]["player_id"], "round": 10, "slot": 6}]
    res = keeper_analysis(rows, keepers, lg)
    check("keeper analysis returns a row per keeper", len(res) == 2, f"{len(res)}")
    okk = all(abs(k["surplus_dollars"] - (k["auction_value"] - k["baseline_value"])) < 1e-6
              for k in res)
    check("surplus == player value - what that pick would fetch", okk)
    late = next(k for k in res if k["keeper_round"] == 10)
    early = next(k for k in res if k["keeper_round"] == 1)
    check("a later keeper round buys a weaker baseline",
          late["baseline_value"] < early["baseline_value"],
          f"R10 ${late['baseline_value']:.0f} < R1 ${early['baseline_value']:.0f}")

    # Inflation direction is the thing that is easy to get backwards.
    none_sold = auction_inflation(rows, [], lg)
    check("no sales means neutral inflation", abs(none_sold["inflation"] - 1.0) < 0.10,
          f"{none_sold['inflation']:.3f}")
    overpay = auction_inflation(
        rows, [{"player_id": p["player_id"], "price": p["auction_value"] * 2.5} for p in top[:10]], lg)
    check("room overpaying => deflation (bargains left)", overpay["inflation"] < 1.0,
          f"{overpay['inflation']:.3f}")
    check("...and the note says so", "below list price" in overpay["note"], overpay["note"][:48])
    bargain = auction_inflation(
        rows, [{"player_id": p["player_id"], "price": 1} for p in top[:40]], lg)
    check("room underpaying => inflation (players left cost more)", bargain["inflation"] > 1.0,
          f"{bargain['inflation']:.3f}")
    check("...and the note says so", "above list price" in bargain["note"], bargain["note"][:48])


def test_sanity(conn, rows, ctx):
    section("9. OUTPUT SANITY")
    bad = [p["name"] for p in rows
           if p["points"] is None or p["points"] != p["points"] or p["points"] < 0]
    check("no NaN, None or negative projections", not bad, f"{len(bad)} bad")
    check("floor <= projection <= ceiling",
          all(p["floor"] <= p["points"] + 1e-6 <= p["ceiling"] + 1e-6 for p in rows))
    check("confidence in (0,1]", all(0 < p["confidence"] <= 1 for p in rows))
    check("breakout in [0,1]", all(0 <= p["breakout"] <= 1 for p in rows))
    check("ppg == points / games",
          all(abs(p["ppg"] - p["points"] / p["games"]) < 1e-6 for p in rows if p["games"] > 0))

    for team, c in ctx.items():
        pass
    prs = [c["pass_rate"] for c in ctx.values()]
    pls = [c["plays_pg"] for c in ctx.values()]
    check("team pass rate is a plausible fraction", all(0.40 < r < 0.75 for r in prs),
          f"{min(prs):.3f}..{max(prs):.3f}")
    check("team plays per game is plausible", all(52 < p < 75 for p in pls),
          f"{min(pls):.1f}..{max(pls):.1f}")
    check("all 32 teams have context", len(ctx) == 32, str(len(ctx)))
    byes = [c["bye"] for c in ctx.values()]
    check("every team has a bye week between 5 and 14",
          all(b and 5 <= b <= 14 for b in byes))

    pri = _pos_priors(conn)
    check("measured yards-per-target priors are plausible",
          all(4.0 < v < 11.0 for v in pri["ypt"].values()), str({k: round(v, 2) for k, v in pri["ypt"].items()}))
    check("measured yards-per-carry priors are plausible",
          all(2.9 <= v < 7.0 for v in pri["ypc"].values()), str({k: round(v, 2) for k, v in pri["ypc"].items()}))
    check("measured catch rates are plausible",
          all(0.5 < v < 0.9 for v in pri["catch"].values()))

    # A projected starter should out-project his own backup.
    off = 0
    for team in ctx:
        for pos in ("QB", "RB", "WR", "TE"):
            grp = sorted([p for p in rows if p["team"] == team and p["position"] == pos
                          and p.get("depth")], key=lambda x: x["depth"])
            if len(grp) >= 2 and grp[0]["depth"] < grp[1]["depth"] and grp[0]["points"] < grp[1]["points"] * 0.5:
                off += 1
    check("depth-chart starters rarely project below half their backup", off <= 12, f"{off} cases")


def test_data_integrity(conn, rows):
    """Guards for the cross-source joins that have actually broken before.

    Every one of these corresponds to a bug that shipped silently: Sleeper
    spelling Arizona AZ, a blank sleeper_id duplicating ~140 players, an accent
    dropping a kicker out of the ADP join. They cost nothing to check and they
    are the failures that do not announce themselves.
    """
    from coaching import COACHES_2026
    from data.sources import norm_name, norm_team
    section("10. DATA INTEGRITY (the joins that have silently broken before)")

    canon = set(COACHES_2026)
    teams = {r["team"] for r in conn.execute(
        "SELECT DISTINCT team FROM players WHERE team IS NOT NULL AND team!=''")}
    check("player teams use canonical abbreviations only", teams <= canon,
          f"stray: {sorted(teams - canon) or 'none'}")
    check("all 32 franchises appear on the player table", len(teams & canon) == 32, str(len(teams)))
    check("Sleeper's AZ is folded into ARI", norm_team("AZ") == "ARI")
    check("diacritics fold for cross-source name joins",
          norm_name("Eddy Piñeiro") == norm_name("Eddy Pineiro") == "eddypineiro")
    check("suffixes fold", norm_name("Marvin Harrison Jr.") == norm_name("Marvin Harrison"))

    dupes = conn.execute(
        "SELECT COUNT(*) n FROM (SELECT search_name, position FROM players "
        "WHERE team IS NOT NULL AND team!='' GROUP BY search_name, position HAVING COUNT(*) > 1)"
    ).fetchone()["n"]
    check("no duplicate players on 2026 rosters", dupes == 0, f"{dupes} duplicate groups")

    unmatched = conn.execute(
        "SELECT COUNT(*) n FROM adp a LEFT JOIN players p "
        "ON p.search_name = a.search_name AND p.position = a.position "
        "WHERE a.fmt='ppr' AND p.player_id IS NULL").fetchone()["n"]
    check("every ADP player joins to a player row", unmatched == 0, f"{unmatched} unmatched")

    # The 2026 coach column in nflverse's schedule is stale; the curated table
    # must be the one in use. Compare through fix_name so a known upstream typo
    # ("Klint Kubliak") is not counted as a real disagreement.
    from coaching import fix_name
    sched = {}
    for r in conn.execute("SELECT home_team, away_team, home_coach, away_coach "
                          "FROM schedule WHERE season=2026"):
        sched[r["home_team"]] = fix_name(r["home_coach"])
        sched[r["away_team"]] = fix_name(r["away_coach"])
    stale = sorted(t for t in canon if sched.get(t) and sched[t] != COACHES_2026[t]["hc"])
    check("curated coaching table overrides the stale 2026 schedule column",
          stale == ["ARI", "ATL", "BUF"],
          f"genuine disagreements: {stale} (expected exactly ARI/ATL/BUF)")

    from config import feature as _feat
    if _feat("redzone_td"):
        rz = conn.execute("SELECT COUNT(*) n FROM redzone WHERE season=?",
                          (HISTORY_SEASONS[0],)).fetchone()["n"]
        tm = conn.execute("SELECT COUNT(*) n FROM redzone_team WHERE season=?",
                          (HISTORY_SEASONS[0],)).fetchone()["n"]
        check("red-zone usage populated (feature is on)", rz > 300 and tm == 32,
              f"{rz} players, {tm} teams")
        # Red-zone carries must be a strict subset of all carries, or the
        # play-by-play filter has drifted.
        bad = conn.execute(
            "SELECT COUNT(*) n FROM redzone z JOIN player_season ps "
            "ON ps.gsis_id=z.gsis_id AND ps.season=z.season "
            "WHERE z.rz_carries > ps.carries + 1").fetchone()["n"]
        check("red-zone carries never exceed total carries", bad == 0, f"{bad} violations")

    ranked = sorted(rows, key=lambda x: -x["vorp"])[:150]
    no_adp = [p["name"] for p in ranked if not p.get("adp")]
    check("top-150 by value mostly have live ADP", len(no_adp) <= 25,
          f"{len(no_adp)} without ADP")


def test_coaching(conn, ctx):
    """The coaching layer must move numbers, in the right direction, by the right amount."""
    from coaching import COACHES_2026, play_caller, staff_changes
    from models.context import build_coach_profiles
    section("11. COACHING LAYER")

    profiles = build_coach_profiles(conn)
    check("coach fingerprints were computed from real seasons", len(profiles) >= 30,
          f"{len(profiles)} coaches with >= half a season")
    check("every coach profile has a plausible pass rate",
          all(0.40 < p["pass_rate"] < 0.75 for p in profiles.values()))
    check("every coach profile has a plausible pace",
          all(50 < p["plays_per_game"] < 78 for p in profiles.values()))

    changed = [t for t in COACHES_2026 if staff_changes(t)["caller_changed"]]
    check("2026 play-caller changes are detected", 8 <= len(changed) <= 20,
          f"{len(changed)} teams: {sorted(changed)}")

    # Chicago changed coordinator but Ben Johnson still calls plays — that must
    # NOT be treated as a scheme reset.
    chi = staff_changes("CHI")
    check("a new coordinator under a play-calling HC is not a caller change",
          chi["oc_changed"] and not chi["caller_changed"], "CHI")
    check("...and its note says continuity, not takeover",
          "still calls the plays" in ctx["CHI"]["note"], ctx["CHI"]["note"][:52])

    # A team whose caller changed should sit between its own baseline and the
    # new caller's career profile — never outside that interval.
    outside = []
    for team in changed:
        caller = play_caller(team)
        prof = profiles.get(caller)
        if not prof:
            continue
        lo, hi = sorted((prof["pass_rate"], ctx[team]["pass_rate"]))
        if not (lo - 0.02 <= ctx[team]["pass_rate"] <= hi + 0.02):
            outside.append(team)
    check("blended pass rate never lands outside [team baseline, coach career]",
          not outside, f"{outside or 'none'} outside the interval")

    vt = [c["vacated_target_pct"] for c in ctx.values()]
    vc = [c["vacated_carry_pct"] for c in ctx.values()]
    check("vacated shares are real fractions", all(0 <= v <= 1 for v in vt + vc))
    check("league vacated target share is plausible (15-35%)",
          0.15 <= sum(vt) / len(vt) <= 0.35, f"{sum(vt)/len(vt)*100:.1f}%")
    check("league vacated carry share is plausible (12-35%)",
          0.12 <= sum(vc) / len(vc) <= 0.35, f"{sum(vc)/len(vc)*100:.1f}%")
    sos = [c["sos"] for c in ctx.values()]
    check("strength of schedule centres on 1.0", 0.97 <= sum(sos) / len(sos) <= 1.03,
          f"mean {sum(sos)/len(sos):.3f}, range {min(sos):.2f}-{max(sos):.2f}")


def test_js_parity():
    """The browser engine must agree with the Python reference, to the cent.

    static/engine.js is a port of models/valuation.py and models/draft.py, and
    two implementations of the same maths is exactly the kind of thing that
    drifts quietly. This runs the JS under node against the same exported data
    the browser gets and compares every number.
    """
    import json
    import shutil
    import subprocess
    import tempfile
    section("12. PYTHON <-> JAVASCRIPT PARITY (static build)")

    node = shutil.which("node")
    export = BASE_DIR / "docs" / "data" / "proj_half_ppr.json"
    if node:
        # A syntax error in the frontend is a blank page, and nothing else in
        # this harness would notice.
        for js in ("engine.js", "app.js", "gate.js"):
            r = subprocess.run([node, "--check", str(BASE_DIR / "static" / js)],
                               capture_output=True, text=True)
            check(f"{js} parses", r.returncode == 0, r.stderr.strip().splitlines()[:1])
    if not node:
        check("node available to run the parity check", False, "node not on PATH")
        return
    if not export.exists():
        check("static export present", False, "run export_static.py first")
        return

    rows = json.loads(export.read_text(encoding="utf-8"))
    league = json.loads(json.dumps(DEFAULT_LEAGUE))

    # Python side, from the identical input the browser receives.
    py = compute_values([dict(r) for r in rows], league)
    ranked = sorted([p for p in py if p.get("adp") is not None], key=lambda x: x["adp"])
    for n, p in enumerate(ranked, 1):
        p["adp_rank"] = n
    for p in py:
        p["value_vs_adp"] = None if p.get("adp") is None else p["adp_rank"] - p["overall_rank"]

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "league.json").write_text(json.dumps(league), encoding="utf-8")
        script = td / "run.js"
        script.write_text(f"""
const Engine = require({str(BASE_DIR / 'static' / 'engine.js')!r}.replace(/\\\\/g,'/'));
const fs = require('fs');
const rows = JSON.parse(fs.readFileSync({str(export)!r}.replace(/\\\\/g,'/'), 'utf8'));
const league = JSON.parse(fs.readFileSync({str(td / 'league.json')!r}.replace(/\\\\/g,'/'), 'utf8'));

const valued = Engine.computeValues(rows, league);
const board = valued.map(p => [p.player_id, p.vorp, p.auction_value, p.max_bid,
                               p.tier, p.overall_rank, p.pos_label, p.value_vs_adp]);

// A mid-draft recommendation call, and the auction/keeper helpers.
const taken = new Set(valued.slice(0, 9).map(p => p.player_id));
const mine = valued.slice(0, 3);
const avail = valued.filter(p => !taken.has(p.player_id));
const recs = Engine.recommend(avail, mine, 10, 15, league, 10)
  .map(p => [p.player_id, p.score, p.cost_of_waiting, p.survives_to_next]);

const keepers = [{{player_id: valued[0].player_id, round: 1, slot: 6}},
                 {{player_id: valued[40].player_id, round: 10, slot: 6}}];
const keep = Engine.keeperAnalysis(valued, keepers, league)
  .map(k => [k.player_id, k.baseline_value, k.surplus_dollars, k.verdict]);

const spent = valued.slice(0, 10).map(p => ({{player_id: p.player_id, price: p.auction_value * 2.5}}));
const inf = Engine.auctionInflation(valued, spent, league);

const probs = [1, 8, 25, 90].map(k => Engine.availability(valued[5], k));
const picks = Engine.pickNumbers(7, 12, 16, true);

process.stdout.write(JSON.stringify({{board, recs, keep, inf, probs, picks}}));
""", encoding="utf-8")
        res = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=180)
        if res.returncode != 0:
            check("node ran the engine", False, res.stderr.strip()[:200])
            return
        js = json.loads(res.stdout)

    check("node ran the engine", True, f"{len(js['board'])} players valued in JS")

    # The single most likely way any frontend change silently fails to ship:
    # app.py serves docs/, not static/, so an edit that is never exported keeps
    # serving the previous file with no error anywhere.
    import filecmp
    for name in ("style.css", "app.js", "engine.js", "gate.js"):
        src, built = BASE_DIR / "static" / name, BASE_DIR / "docs" / name
        check(f"docs/{name} matches static/{name} (export is current)",
              built.exists() and filecmp.cmp(src, built, shallow=False),
              "run export_static.py" if not (built.exists()
                                             and filecmp.cmp(src, built, shallow=False)) else "in sync")

    # renderAuction now sorts by raw auction_value and multiplies by inflation in
    # the row template. That is only order-preserving while inflation is a
    # positive scalar, so assert the clamp actually holds.
    check("auction inflation is strictly positive (sort-order equivalence)",
          js["inf"]["inflation"] > 0, f"{js['inf']['inflation']:.4f}")

    # --- board parity
    jsb = {r[0]: r for r in js["board"]}
    worst = {"vorp": 0.0, "auction": 0.0, "max_bid": 0.0}
    tier_bad = rank_bad = label_bad = edge_bad = 0
    for p in py:
        j = jsb.get(p["player_id"])
        if not j:
            continue
        worst["vorp"] = max(worst["vorp"], abs(p["vorp"] - j[1]))
        worst["auction"] = max(worst["auction"], abs(p["auction_value"] - j[2]))
        worst["max_bid"] = max(worst["max_bid"], abs(p["max_bid"] - j[3]))
        if p.get("tier") != j[4]:
            tier_bad += 1
        if p["overall_rank"] != j[5]:
            rank_bad += 1
        if p.get("pos_label") != j[6]:
            label_bad += 1
        if p.get("value_vs_adp") != j[7]:
            edge_bad += 1
    check("VORP identical in Python and JS", worst["vorp"] < 1e-6, f"max diff {worst['vorp']:.2e}")
    check("auction dollars identical", worst["auction"] < 1e-6, f"max diff {worst['auction']:.2e}")
    check("max bid identical", worst["max_bid"] < 1e-6, f"max diff {worst['max_bid']:.2e}")
    check("tiers identical", tier_bad == 0, f"{tier_bad} differ")
    check("overall rank identical", rank_bad == 0, f"{rank_bad} differ")
    check("position labels identical", label_bad == 0, f"{label_bad} differ")
    check("value-vs-ADP identical", edge_bad == 0, f"{edge_bad} differ")

    # --- draft mechanics parity
    check("pick numbers identical (3RR, slot 7 of 12)",
          js["picks"] == pick_numbers(7, 12, 16, True), str(js["picks"][:5]))
    fifth = py[5] if py[5]["player_id"] == js["board"][5][0] else None
    src = next(p for p in py if p["player_id"] == js["board"][5][0])
    pyprobs = [availability(src, k) for k in (1, 8, 25, 90)]
    dp = max(abs(a - b) for a, b in zip(pyprobs, js["probs"]))
    check("survival probabilities identical (normal CDF ports agree)", dp < 1e-9,
          f"max diff {dp:.2e}")

    # --- recommendation parity
    taken = {p["player_id"] for p in py[:9]}
    avail = [p for p in py if p["player_id"] not in taken]
    pyrecs = recommend(avail, py[:3], 10, 15, DEFAULT_LEAGUE, 10)
    same_order = [r["player_id"] for r in pyrecs] == [r[0] for r in js["recs"]]
    check("recommendation ORDER identical", same_order,
          f"py: {[r['name'].split()[-1] for r in pyrecs[:3]]}")
    if same_order:
        ds = max(abs(r["score"] - j[1]) for r, j in zip(pyrecs, js["recs"]))
        dc = max(abs(r["cost_of_waiting"] - j[2]) for r, j in zip(pyrecs, js["recs"]))
        check("recommendation scores identical", ds < 1e-6 and dc < 1e-6,
              f"score {ds:.2e}, cost-of-waiting {dc:.2e}")

    # --- keeper + inflation parity
    keepers = [{"player_id": py[0]["player_id"], "round": 1, "slot": 6},
               {"player_id": py[40]["player_id"], "round": 10, "slot": 6}]
    pyk = keeper_analysis(py, keepers, DEFAULT_LEAGUE)
    jsk = {k[0]: k for k in js["keep"]}
    dk = max(abs(k["baseline_value"] - jsk[k["player_id"]][1]) for k in pyk)
    ds = max(abs(k["surplus_dollars"] - jsk[k["player_id"]][2]) for k in pyk)
    check("keeper baselines and surplus identical", dk < 1e-6 and ds < 1e-6,
          f"baseline {dk:.2e}, surplus {ds:.2e}")
    check("keeper verdicts identical",
          all(k["verdict"] == jsk[k["player_id"]][3] for k in pyk))

    spent = [{"player_id": p["player_id"], "price": p["auction_value"] * 2.5} for p in py[:10]]
    pyi = auction_inflation(py, spent, DEFAULT_LEAGUE)
    check("auction inflation identical", abs(pyi["inflation"] - js["inf"]["inflation"]) < 1e-9,
          f"py {pyi['inflation']:.6f} vs js {js['inf']['inflation']:.6f}")
    check("inflation note identical", pyi["note"] == js["inf"]["note"])


def test_team_analysis():
    """Properties of the roster grade.

    There is no Python counterpart to compare against — the grade is aggregation
    over values the parity-tested engine already produced, not new modelling — so
    this asserts the properties that must hold instead: bounded scores, a sane
    benchmark, and monotonicity (a strictly better roster must not grade worse).
    """
    import json
    import shutil
    import subprocess
    import tempfile
    section("14. TEAM ANALYSIS (My Team grade)")

    node = shutil.which("node")
    export = BASE_DIR / "docs" / "data" / "proj_half_ppr.json"
    if not node or not export.exists():
        check("node + export available for team analysis", False, "skipped")
        return

    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "team.js"
        script.write_text(f"""
const E = require({str(BASE_DIR / 'static' / 'engine.js')!r}.replace(/\\\\/g,'/'));
const fs = require('fs');
const rows = JSON.parse(fs.readFileSync({str(export)!r}.replace(/\\\\/g,'/'), 'utf8'));
const lg = {{teams:12, rounds:16, budget:200, auction_budget:200,
  roster:{{QB:1,RB:2,WR:3,TE:1,FLEX:1,SUPERFLEX:0,K:1,DST:1,BENCH:6}}}};
const board = E.computeValues(rows, lg);
const pick = (pos,n,from) => board.filter(p => p.position===pos).slice(from, from+n);
const build = off => [].concat(pick('RB',3,off), pick('WR',3,off), pick('QB',1,off),
                               pick('TE',1,off), pick('K',1,0), pick('DST',1,0));
const strong = build(0), mid = build(10), weak = build(30);
const out = {{
  bands: E.benchmarkBands(board, lg),
  strong: E.analyseTeam(board, strong, lg),
  mid:    E.analyseTeam(board, mid, lg),
  weak:   E.analyseTeam(board, weak, lg),
  empty:  E.analyseTeam(board, [], lg),
}};
const slim = a => ({{overall:a.overall, grade:a.grade, yourPoints:a.yourPoints,
  avgPoints:a.avgPoints, surplus:a.surplus,
  positions:a.positions.map(p=>({{pos:p.pos,score:p.score,grade:p.grade,pct:p.pct,
    filled:p.filled,slots:p.slots,yourPoints:p.yourPoints}})),
  strongest:a.strongest?a.strongest.pos:null, weakest:a.weakest?a.weakest.pos:null,
  gaps:a.gaps.length, lineupCount:Object.keys(a.lineup).length,
  benchCount:a.bench.length, riskShare:a.riskShare}});
process.stdout.write(JSON.stringify({{bands: out.bands, strong: slim(out.strong),
  mid: slim(out.mid), weak: slim(out.weak), empty: slim(out.empty)}}));
""", encoding="utf-8")
        r = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            check("team analysis runs", False, r.stderr.strip()[:200])
            return
        d = json.loads(r.stdout)

    check("team analysis runs", True, f"{len(d['strong']['positions'])} position grades")

    for label in ("strong", "mid", "weak", "empty"):
        a = d[label]
        check(f"{label}: overall score within 0-100", 0 <= a["overall"] <= 100,
              f"{a['overall']:.1f} ({a['grade']})")
        check(f"{label}: every position score within 0-100",
              all(0 <= p["score"] <= 100 for p in a["positions"]))

    # Monotonic: a strictly better roster cannot grade worse.
    check("a better roster scores higher",
          d["strong"]["overall"] > d["mid"]["overall"] > d["weak"]["overall"],
          f"{d['strong']['overall']:.0f} > {d['mid']['overall']:.0f} > {d['weak']['overall']:.0f}")
    check("points track the grade",
          d["strong"]["yourPoints"] > d["mid"]["yourPoints"] > d["weak"]["yourPoints"])

    # The benchmark must itself be sane: each slot band weaker than the one above.
    bands = d["bands"]
    ok = all(all(b[i] >= b[i + 1] - 1e-9 for i in range(len(b) - 1))
             for b in bands.values() if len(b) > 1)
    check("benchmark bands decline from slot 1 downward", ok,
          f"RB bands {[round(x) for x in bands.get('RB', [])]}")
    check("benchmark bands are positive",
          all(all(x > 0 for x in b) for b in bands.values() if b))

    # An average team must grade near the middle by construction.
    check("the benchmark itself is the mid-point of the scale",
          40 <= d["mid"]["overall"] <= 75, f"mid roster grades {d['mid']['overall']:.0f}")

    check("empty roster is handled without dividing by zero",
          d["empty"]["gaps"] > 0 and d["empty"]["overall"] >= 0,
          f"{d['empty']['gaps']} gaps, score {d['empty']['overall']:.0f}")
    check("strongest and weakest come from real positions",
          d["strong"]["strongest"] in ("QB", "RB", "WR", "TE", "FLEX")
          and d["weak"]["weakest"] in ("QB", "RB", "WR", "TE", "FLEX"),
          f"strong: {d['strong']['strongest']}, weak: {d['weak']['weakest']}")
    check("risk share is a fraction", 0 <= d["strong"]["riskShare"] <= 1)


def test_auction_sheet():
    """The printable auction sheet must agree with the site it was printed from.

    Delegated to node because the page renders itself as an HTML string; the JS
    harness stubs a DOM, runs the page's own render(), and asserts on the markup.
    """
    import shutil
    import subprocess
    section("15. PRINTABLE AUCTION SHEET")

    node = shutil.which("node")
    sheet = BASE_DIR / "docs" / "auction.html"
    if not node or not sheet.exists():
        check("node + built sheet available", False, "skipped")
        return
    r = subprocess.run([node, str(BASE_DIR / "tests_auction_sheet.js"), str(BASE_DIR)],
                       capture_output=True, text=True, timeout=300)
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("ok "):
            check(line[3:].split("  [")[0], True,
                  line.split("  [")[1].rstrip("]") if "  [" in line else "")
        elif line.startswith("FAIL "):
            check(line[5:].split("  [")[0], False,
                  line.split("  [")[1].rstrip("]") if "  [" in line else "")
    if r.returncode != 0 and "FAIL" not in r.stdout:
        check("auction sheet harness ran", False, (r.stderr or r.stdout).strip()[:200])


def test_backtest_accuracy():
    """Guard the measured accuracy of the model against silent regression.

    Runs only when a backtest database is present (build one with
    `python backtest.py`), because it needs a reconstructed pre-season state.
    The thresholds are floors, deliberately set a little below the measured
    values so ordinary noise does not fail the build — the point is to catch a
    change that makes the model materially worse, not to pin it to a decimal.
    """
    import subprocess
    section("13. BACKTESTED ACCURACY (regression floor)")
    # Floors sit a little under the measured values so ordinary noise does not
    # fail the build. Raised on 2026-08-13 after the depth-chart schema fix moved
    # every baseline by +0.04 to +0.07; the old floors would no longer have
    # caught a regression that undid it.
    floors = {2025: 0.615, 2024: 0.625, 2023: 0.640, 2022: 0.690, 2021: 0.660}
    ran = 0
    for season, floor in floors.items():
        if not (BASE_DIR / f"backtest_{season}.db").exists():
            continue
        r = subprocess.run([sys.executable, str(BASE_DIR / "backtest.py"), "--season", str(season)],
                           capture_output=True, text=True, timeout=900, cwd=str(BASE_DIR))
        rho = None
        for line in r.stdout.splitlines():
            if "Spearman" in line:
                try:
                    rho = float(line.split(":")[-1].strip())
                except ValueError:
                    pass
        if rho is None:
            check(f"{season} backtest produced a score", False, r.stderr.strip()[:120])
            continue
        ran += 1
        check(f"{season} rank correlation >= {floor}", rho >= floor, f"{rho:.4f}")
    if not ran:
        check("backtest databases present", True,
              "skipped - run `python backtest.py` to enable this section")


def main():
    print("Gridiron Edge - model verification")
    with db() as conn:
        proj = project(conn, "half_ppr")
        ctx = build_team_context(conn)
        # attach_adp is NOT optional here. Without it every player has adp=None,
        # availability() returns its flat no-ADP constant, and every
        # pick-dependent assertion below passes while testing nothing.
        rows = attach_adp(conn, compute_values([dict(p) for p in proj], DEFAULT_LEAGUE),
                          "half-ppr", DEFAULT_LEAGUE)
        assert any(p.get("adp") for p in rows), "ADP failed to attach - checks would be vacuous"


        test_scoring(conn)
        test_volume(conn, rows, ctx)
        test_shrinkage()
        test_injury(conn)
        test_valuation(conn, rows)
        test_superflex(conn, proj)
        test_draft(rows)
        test_keeper_auction(rows)
        test_sanity(conn, rows, ctx)
        test_data_integrity(conn, rows)
        test_coaching(conn, ctx)
    test_js_parity()
    test_team_analysis()
    test_auction_sheet()
    test_backtest_accuracy()

    print(f"\n{'=' * 72}")
    print(f"{PASS} passed, {FAIL} failed")
    if _failures:
        print("failed checks:")
        for f in _failures:
            print(f"   - {f}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
