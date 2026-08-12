"""
Turning projected points into draft decisions.

Projected points alone do not tell you who to draft. 300 points from a
quarterback is worth far less than 300 from a tight end, because the twelfth-best
quarterback also scores a lot and the twelfth-best tight end does not. Everything
here is about converting points into value ABOVE WHAT YOU COULD HAVE HAD ANYWAY.

  REPLACEMENT LEVEL  Derived from the actual league settings: how many players at
                     each position will be starting across the league once flex
                     slots are allocated to whoever fills them. The next player
                     after that is free, and is the baseline.

  VORP               Projected points minus that baseline. This is the number
                     that should drive every pick.

  TIERS              Gap-based clusters within a position. Tiers matter more than
                     ranks on draft day: the difference between the last player
                     in a tier and the first player in the next one is where the
                     real decisions are.

  AUCTION DOLLARS    The league's spendable money distributed across positive
                     VORP, with a max bid that reflects tier position rather than
                     a flat premium.
"""
from __future__ import annotations

from collections import defaultdict

FLEX_ELIGIBLE = ("RB", "WR", "TE")


def roster_size(league: dict) -> int:
    r = league["roster"]
    return sum(r.values())


def starter_demand(league: dict, players_by_pos: dict) -> dict:
    """League-wide starters at each position, allocating flex to whoever wins it.

    The flex slot is not "an extra RB". It goes to whichever flex-eligible player
    is best at the moment it is filled, which in practice means the position with
    the deepest talent absorbs it. Assigning flex correctly is what makes
    replacement level - and therefore every value on the board - honest.
    """
    teams = league["teams"]
    roster = league["roster"]
    demand = {pos: roster.get(pos, 0) * teams for pos in ("QB", "RB", "WR", "TE", "K", "DST")}

    flex_slots = roster.get("FLEX", 0) * teams
    if flex_slots > 0:
        # Walk the best remaining flex-eligible players and count where they come from.
        pool = []
        for pos in FLEX_ELIGIBLE:
            arr = players_by_pos.get(pos, [])
            start = demand.get(pos, 0)
            for p in arr[start:start + flex_slots]:
                pool.append((p["points"], pos))
        pool.sort(reverse=True, key=lambda x: x[0])
        for _pts, pos in pool[:flex_slots]:
            demand[pos] = demand.get(pos, 0) + 1

    # Superflex / 2QB: the extra QB slot moves replacement level dramatically.
    if roster.get("SUPERFLEX"):
        demand["QB"] += roster["SUPERFLEX"] * teams
    return demand


def compute_values(projections: list, league: dict) -> list:
    """Attach replacement level, VORP, tier, auction value and rank to each player."""
    rows = [dict(p) for p in projections]
    by_pos = defaultdict(list)
    for p in rows:
        by_pos[p["position"]].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: -x["points"])

    demand = starter_demand(league, by_pos)

    # Replacement = the best player you could still get for nothing at that
    # position once every league starter is accounted for.
    #
    # Kickers and defenses are the exception, and the exception matters. Their
    # season-long replacement (the 12th defense in a 12-team league) badly
    # overstates their value, because nobody actually rosters one defense all
    # year - you stream the best matchup off waivers every week. The replacement
    # you can genuinely access is therefore close to the TOP of the position, not
    # the bottom of the starters. Priced against DST12 they kept surfacing inside
    # the top 100 overall, which is exactly the pick this site should talk you
    # out of.
    teams = league["teams"]
    replacement = {}
    for pos, arr in by_pos.items():
        if not arr:
            replacement[pos] = 0.0
            continue
        if pos in ("K", "DST"):
            idx = max(1, teams // 4)
        else:
            idx = demand.get(pos, 0)
        idx = min(max(idx, 1), len(arr) - 1)
        replacement[pos] = arr[idx]["points"]

    for p in rows:
        p["replacement"] = replacement.get(p["position"], 0.0)
        p["vorp"] = p["points"] - p["replacement"]
        # Floor/ceiling expressed the same way, so risk is comparable to value.
        p["vorp_floor"] = p["floor"] - p["replacement"]
        p["vorp_ceiling"] = p["ceiling"] - p["replacement"]

    _assign_tiers(by_pos)
    _assign_auction(rows, league)

    rows.sort(key=lambda x: -x["vorp"])
    for n, p in enumerate(rows, 1):
        p["overall_rank"] = n
    for pos, arr in by_pos.items():
        for n, p in enumerate(sorted(arr, key=lambda x: -x["vorp"]), 1):
            p["pos_rank"] = n
            p["pos_label"] = f"{pos}{n}"
    return rows


def _assign_tiers(by_pos: dict) -> None:
    """Cluster each position into tiers by finding the real gaps.

    A tier break is a drop that is large relative to the typical gap between
    neighbours at that position. This is what tells you whether to reach now or
    wait a round - if six players sit in the same tier, waiting costs nothing.
    """
    for pos, arr in by_pos.items():
        ranked = sorted(arr, key=lambda x: -x["points"])
        if not ranked:
            continue
        # Only tier the fantasy-relevant portion; the tail is all one tier.
        head = ranked[:60]
        gaps = [head[i]["points"] - head[i + 1]["points"] for i in range(len(head) - 1)]
        if not gaps:
            for p in ranked:
                p["tier"] = 1
            continue
        avg_gap = sum(gaps) / len(gaps)
        var = sum((g - avg_gap) ** 2 for g in gaps) / len(gaps)
        sd = var ** 0.5
        threshold = avg_gap + 0.85 * sd

        tier, size = 1, 0
        for i, p in enumerate(head):
            p["tier"] = tier
            size += 1
            if i < len(gaps):
                # Break on a real gap, or when a tier gets unwieldy.
                if (gaps[i] >= threshold and size >= 2) or size >= 8:
                    tier += 1
                    size = 0
        for p in ranked[60:]:
            p["tier"] = tier + 1


def _assign_auction(rows: list, league: dict) -> None:
    """Distribute the league's spendable money across positive VORP."""
    teams = league["teams"]
    budget = league.get("auction_budget", 200)
    spots = roster_size(league)

    total_money = teams * budget
    total_spots = teams * spots
    # Every roster spot costs at least $1, so that money is not distributable.
    spendable = max(total_money - total_spots, 1)

    draftable = sorted(rows, key=lambda x: -x["vorp"])[:total_spots]
    pool = sum(p["vorp"] for p in draftable if p["vorp"] > 0)

    for p in rows:
        if pool > 0 and p["vorp"] > 0:
            p["auction_value"] = 1.0 + p["vorp"] * (spendable / pool)
        else:
            p["auction_value"] = 1.0
        # Max bid: what you can justify paying before it stops being value. The
        # premium is larger at the top of a tier (the drop behind them is real)
        # and near zero deep in one (the next guy is the same guy).
        tier_prem = 0.16 if p.get("tier", 9) <= 2 else 0.10 if p.get("tier", 9) <= 4 else 0.05
        if p.get("injury_risk") == "high":
            tier_prem -= 0.06
        p["max_bid"] = p["auction_value"] * (1.0 + max(tier_prem, 0.0))


def attach_adp(conn, rows: list, fmt: str, league: dict) -> list:
    """Join live ADP and compute where the market and the model disagree."""
    adp = {}
    for r in conn.execute("SELECT * FROM adp WHERE fmt=?", (fmt,)):
        adp[(r["search_name"], r["position"])] = r

    ids = {}
    for r in conn.execute("SELECT player_id, search_name, position FROM players"):
        ids[r["player_id"]] = (r["search_name"], r["position"])

    for p in rows:
        key = ids.get(p["player_id"])
        a = adp.get(key) if key else None
        if a:
            p["adp"] = a["adp"]
            p["adp_formatted"] = a["adp_formatted"]
            p["adp_stdev"] = a["stdev"] or max(a["adp"] * 0.18, 3.0)
            p["adp_high"] = a["high"]
            p["adp_low"] = a["low"]
            p["times_drafted"] = a["times_drafted"]
        else:
            p["adp"] = None
            p["adp_formatted"] = ""
            p["adp_stdev"] = None
            p["times_drafted"] = 0

    # Market rank vs model rank. Positive value_vs_adp = the model likes them
    # more than the room does.
    ranked = [p for p in rows if p["adp"] is not None]
    ranked.sort(key=lambda x: x["adp"])
    for n, p in enumerate(ranked, 1):
        p["adp_rank"] = n
    for p in rows:
        if p.get("adp") is None:
            p["value_vs_adp"] = None
            p["adp_rank"] = None
        else:
            p["value_vs_adp"] = p["adp_rank"] - p["overall_rank"]
    return rows


# ------------------------------------------------------------------ keeper --
def _expected_best(pool: list, pick_no: int):
    """Expected best player still on the board at a given pick.

    Walks candidates in model order and accumulates
        value(q) x P(q survives to pick) x P(nobody better survives)
    which is the expectation of "best available" under independent survival.
    Returns (representative player, expected auction value, expected VORP).
    """
    from models.draft import availability

    ranked = pool[:120]
    exp_val = exp_vorp = 0.0
    none_better = 1.0
    rep, rep_p = None, 0.0
    for q in ranked:
        pa = availability(q, pick_no)
        w = pa * none_better
        exp_val += q["auction_value"] * w
        exp_vorp += q["vorp"] * w
        if w > rep_p:
            rep, rep_p = q, w
        none_better *= (1.0 - pa)
        if none_better < 0.001:
            break
    return rep, exp_val, exp_vorp


def keeper_analysis(rows: list, keepers: list, league: dict) -> list:
    """Value of keeping a player at a given round cost.

    The question a keeper decision actually asks is: "if I did NOT keep him, what
    would I get with that pick instead?" So the comparison is always against the
    player the board says is available at that draft slot, never against the
    keeper in isolation.
    """
    teams = league["teams"]
    by_id = {p["player_id"]: p for p in rows}

    # Built once: every keeper is compared against the same board, minus the
    # players being kept. Rebuilding and re-sorting this inside the loop made the
    # cost quadratic in roster size for no benefit.
    kept_ids = {kk.get("player_id") for kk in keepers}
    pool = sorted((q for q in rows if q["player_id"] not in kept_ids),
                  key=lambda x: -x["vorp"])[:150]

    out = []
    for k in keepers:
        p = by_id.get(k.get("player_id"))
        if not p:
            continue
        rnd = int(k.get("round") or 1)
        # The pick you spend is your slot in that round.
        pick_no = (rnd - 1) * teams + max(1, min(teams, int(k.get("slot") or teams // 2)))

        # Baseline: the BEST player our model likes who is realistically still on
        # the board at that pick - not simply whoever the market happens to draft
        # at that exact slot. Anchoring on one player makes the whole comparison
        # hostage to a single model-vs-market disagreement: a keeper looks like a
        # steal purely because the guy at that ADP is someone we happen to rate
        # at a dollar. Taking the expectation over who survives fixes that.
        baseline, base_val, base_vorp = _expected_best(pool, pick_no)
        surplus = p["auction_value"] - base_val
        out.append({
            **p,
            "keeper_round": rnd,
            "keeper_pick": pick_no,
            "baseline_name": baseline["name"] if baseline else "-",
            "baseline_pos": baseline.get("pos_label", "") if baseline else "",
            "baseline_value": base_val,
            "baseline_vorp": base_vorp,
            "surplus_dollars": surplus,
            "surplus_vorp": p["vorp"] - base_vorp,
            "verdict": ("Strong keep" if surplus >= 15 else
                        "Keep" if surplus >= 5 else
                        "Marginal" if surplus >= -3 else "Let him go"),
        })
    out.sort(key=lambda x: -x["surplus_dollars"])
    return out


# --------------------------------------------------------------- inflation --
def auction_inflation(rows: list, spent: list, league: dict) -> dict:
    """Live auction inflation.

    Once players start going over or under value, the money left in the room no
    longer matches the value left on the board. If the room has overspent, every
    remaining player is effectively cheaper than his listed value; if it has been
    frugal, the players left will cost more than they are worth. Tracking this is
    the single biggest edge available in an auction, and almost nobody does it.
    """
    teams = league["teams"]
    budget = league.get("auction_budget", 200)
    spots = roster_size(league)

    total_money = teams * budget
    gone_ids = {s["player_id"] for s in spent}
    money_spent = sum(float(s.get("price") or 0) for s in spent)
    money_left = total_money - money_spent
    spots_left = teams * spots - len(spent)

    remaining = [p for p in rows if p["player_id"] not in gone_ids]
    remaining.sort(key=lambda x: -x["vorp"])
    draftable = remaining[:max(spots_left, 1)]
    value_left = sum(p["auction_value"] for p in draftable)

    # Money that must be committed to $1 roster filler is not chasing value.
    committed_min = max(spots_left - len(draftable), 0)
    spendable_left = max(money_left - committed_min, 1)
    factor = spendable_left / value_left if value_left > 0 else 1.0
    factor = min(max(factor, 0.4), 2.5)

    # Direction matters and is easy to get backwards. factor = money left per
    # dollar of value left. ABOVE 1 means more cash than talent is still in the
    # room, so the players left will be bid UP. BELOW 1 means the room has
    # already blown its budget and what remains goes CHEAP.
    return {
        "money_left": money_left,
        "money_spent": money_spent,
        "spots_left": spots_left,
        "value_left": value_left,
        "inflation": factor,
        "inflation_pct": (factor - 1.0) * 100.0,
        "buyers_market": factor < 1.0,
        "note": ("The room is sitting on its money - expect the players left to be bid "
                 "above list price, so budget accordingly"
                 if factor > 1.06 else
                 "The room has overspent - the players left should go below list price, "
                 "so hold your cash and pounce"
                 if factor < 0.94 else
                 "Spending is tracking value so far"),
    }
