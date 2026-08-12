"""
Snake draft logic.

The question a draft board should answer is not "who is the best player left" -
it is "who will still be there next time, and what does waiting cost me". A
running back you can have two rounds from now is worth less to you right now
than a receiver who will certainly be gone, even if the back scores more points.

So the recommendation engine scores every available player as:

    VORP(player)  -  E[VORP of the best player at that position who survives
                      until my next pick]

which is literally the cost of waiting on that position. That is then adjusted
for what the roster still needs and for bye-week collisions.

Availability is modelled analytically rather than by simulation: a player's draft
slot is treated as normal around his ADP with the spread the ADP source actually
reports, so P(still there at pick k) = 1 - CDF(k). With thousands of real drafts
behind each ADP, that spread is measured, not assumed.
"""
from __future__ import annotations

import math
from collections import defaultdict

STARTER_PRIORITY = ("RB", "WR", "TE", "QB", "K", "DST")


def pick_numbers(slot: int, teams: int, rounds: int, reversal: bool = False) -> list:
    """Overall pick numbers for a given draft slot in a snake order.

    `reversal` supports the third-round-reversal variant, where round 3 repeats
    round 2's order instead of flipping, which materially changes what the
    early- and late-slot strategies should be.
    """
    picks = []
    for rnd in range(1, rounds + 1):
        if reversal and rnd >= 3:
            # Round 3 repeats round 2's backward order, and the snake then
            # continues from there - so the parity is INVERTED for round 3 and
            # every round after it, not just round 3. Flipping only round 3 (and
            # leaving round 4 backward as well) produces a legal-looking but
            # wrong order: at slot 1 it gave picks 24, 36, 48 instead of the
            # correct 24, 36, 37.
            forward = (rnd % 2 == 0)
        else:
            forward = (rnd % 2 == 1)
        pos = slot if forward else (teams - slot + 1)
        picks.append((rnd - 1) * teams + pos)
    return picks


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def availability(player: dict, pick_no: int) -> float:
    """P(this player is still on the board at pick_no).

    A player's draft slot is modelled as normal around his ADP, but a raw normal
    puts probability mass on slots below 1 - which is nonsense, since nobody can
    be drafted before the draft starts. For a player with ADP 1.5 that mass is
    large, and it was reporting the consensus 1.01 as only 66% likely to be
    available AT THE FIRST PICK. The distribution is therefore truncated at the
    top of the board and renormalised, which forces P(available at pick 1) = 1
    for everyone while leaving later picks essentially unchanged.
    """
    adp = player.get("adp")
    if adp is None:
        # No ADP means the market barely drafts him: effectively always there.
        return 0.97
    sd = max(player.get("adp_stdev") or max(adp * 0.18, 3.0), 1.2)
    # Continuity correction: "available at pick k" means "not taken in 1..k-1".
    surviving = 1.0 - _norm_cdf((pick_no - 0.5 - adp) / sd)
    before_board = _norm_cdf((0.5 - adp) / sd)   # impossible mass, below pick 1
    denom = 1.0 - before_board
    if denom <= 1e-9:
        return 0.0
    return min(max(surviving / denom, 0.0), 1.0)


def expected_best_at(players: list, pick_no: int) -> float:
    """Expected VORP of the best of these players still available at pick_no.

    Walks candidates best-first and accumulates
        VORP(q) x P(q available) x P(nobody better is available)
    which is the exact expectation under independent survival.
    """
    ranked = sorted(players, key=lambda x: -x["vorp"])[:40]
    exp, none_better = 0.0, 1.0
    for q in ranked:
        pa = availability(q, pick_no)
        exp += q["vorp"] * pa * none_better
        none_better *= (1.0 - pa)
        if none_better < 0.001:
            break
    return exp


def roster_needs(roster: list, league: dict) -> dict:
    """How many starters are still unfilled at each position."""
    slots = dict(league["roster"])
    have = defaultdict(int)
    for p in roster:
        have[p["position"]] += 1

    need = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        need[pos] = max(slots.get(pos, 0) - have[pos], 0)

    # Flex demand goes to whichever flex position is least stocked.
    flex = slots.get("FLEX", 0)
    if flex:
        surplus = {p: max(have[p] - slots.get(p, 0), 0) for p in ("RB", "WR", "TE")}
        if sum(surplus.values()) < flex:
            for p in ("RB", "WR"):
                need[p] = need.get(p, 0) + 0.5
    return need


def recommend(available: list, roster: list, my_pick: int, next_pick, league: dict,
              limit: int = 40) -> list:
    """Rank the board for the pick that is on the clock."""
    needs = roster_needs(roster, league)
    slots = dict(league["roster"])
    have = defaultdict(int)
    bye_by_pos = defaultdict(list)
    for p in roster:
        have[p["position"]] += 1
        if p.get("bye"):
            bye_by_pos[p["position"]].append(p["bye"])

    by_pos = defaultdict(list)
    for p in available:
        by_pos[p["position"]].append(p)

    # What the best option at each position looks like if I wait one turn.
    wait_value = {}
    for pos, arr in by_pos.items():
        wait_value[pos] = expected_best_at(arr, next_pick) if next_pick else 0.0

    out = []
    for p in available:
        pos = p["position"]
        cost_of_waiting = p["vorp"] - wait_value.get(pos, 0.0)

        # Roster need. Positions already stocked past their starters are damped;
        # positions with nobody in them are boosted. Kickers and defenses are
        # actively suppressed until the last rounds, which is where they belong.
        starters = slots.get(pos, 0)
        filled = have[pos]
        if pos in ("K", "DST"):
            need_mult = 0.05 if filled >= starters else 0.35
        elif filled < starters:
            need_mult = 1.15
        elif needs.get(pos, 0) > 0:
            need_mult = 1.05
        else:
            depth_over = filled - starters
            need_mult = max(0.55, 1.0 - 0.16 * depth_over)

        # Bye-week collision with a starter already rostered at the position.
        bye_pen = 1.0
        if p.get("bye") and p["bye"] in bye_by_pos.get(pos, []):
            bye_pen = 0.96

        score = (0.62 * p["vorp"] + 0.38 * max(cost_of_waiting, 0.0)) * need_mult * bye_pen

        surv = availability(p, next_pick) if next_pick else 0.0
        out.append({
            **p,
            "score": score,
            "cost_of_waiting": cost_of_waiting,
            "survives_to_next": surv,
            "wait_value": wait_value.get(pos, 0.0),
            "need_mult": need_mult,
            "reason": _reason(p, cost_of_waiting, surv, need_mult, pos, filled, starters),
        })
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def _reason(p, cost, surv, need_mult, pos, filled, starters) -> str:
    bits = []
    if surv < 0.18:
        bits.append(f"almost certainly gone by your next pick ({surv * 100:.0f}% to last)")
    elif surv > 0.65:
        bits.append(f"likely still there next turn ({surv * 100:.0f}%)")
    if cost > 18:
        bits.append(f"waiting on {pos} costs about {cost:.0f} points of value")
    elif cost < 4:
        bits.append(f"{pos} is deep here - the next one is nearly as good")
    if p.get("value_vs_adp") and p["value_vs_adp"] >= 12:
        bits.append(f"the room is letting him slide {p['value_vs_adp']} spots past his value")
    elif p.get("value_vs_adp") is not None and p["value_vs_adp"] <= -12:
        bits.append(f"going {abs(p['value_vs_adp'])} spots earlier than the model justifies")
    if filled < starters:
        bits.append(f"fills an open {pos} starting spot")
    if p.get("injury_risk") == "high":
        bits.append("carries real injury risk")
    if p.get("tier") and p.get("tier") <= 2:
        bits.append(f"still in tier {p['tier']} at the position")
    return "; ".join(bits[:3]) if bits else "solid value at this pick"


def plan_draft(rows: list, slot: int, league: dict, keepers=None, reversal=False) -> list:
    """Round-by-round expectation for a given draft slot.

    For each of your picks it reports the players most likely to be there, so you
    can see the shape of your draft before it starts rather than improvising.
    """
    teams, rounds = league["teams"], league["rounds"]
    picks = pick_numbers(slot, teams, rounds, reversal)
    kept = {k for k in (keepers or [])}

    pool = [p for p in rows if p["player_id"] not in kept]
    plan = []
    for rnd, pick in enumerate(picks, 1):
        cands = []
        for p in pool:
            pa = availability(p, pick)
            if pa > 0.03:
                cands.append((p["vorp"] * pa, pa, p))
        cands.sort(key=lambda x: -x[0])
        top = [{
            "name": c[2]["name"], "position": c[2]["position"], "team": c[2]["team"],
            "pos_label": c[2].get("pos_label", ""), "tier": c[2].get("tier"),
            "vorp": c[2]["vorp"], "points": c[2]["points"], "adp": c[2].get("adp"),
            "prob": c[1], "auction_value": c[2].get("auction_value"),
            "injury_risk": c[2].get("injury_risk"),
        } for c in cands[:8]]
        plan.append({"round": rnd, "pick": pick, "candidates": top})
    return plan
