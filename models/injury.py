"""
Availability and injury risk.

Fantasy points are (points per game) x (games played), and the second term is
routinely ignored or hand-waved. This module estimates it from evidence:

  * PAST AVAILABILITY - how much of each of the last five seasons the player was
    actually active, measured from weekly injury reports (games listed Out or
    Doubtful) cross-checked against games with recorded snaps.
  * INJURY TYPE - structural injuries (ACL, Achilles, Lisfranc) suppress the
    following season's efficiency; soft-tissue injuries (hamstring, groin, calf)
    predict recurrence rather than a talent decline. They are treated differently.
  * CURRENT STATUS - a live designation from Sleeper. A player on PUP or IR in
    August starts the season in a hole, and that is subtracted directly.
  * AGE - the risk curve steepens with age, hardest for running backs.

The output is a projected games-played figure, a risk label, and a plain-English
note explaining the number, so nothing is a black box.
"""
from __future__ import annotations

from collections import defaultdict

from config import (
    GAMES, INJURY_SEASONS, INJURY_STATUS_COST, POS_AVAILABILITY_BASE,
    SOFT_TISSUE, STRUCTURAL,
)

# How much weight each season of history carries, newest first.
_W = [0.34, 0.26, 0.18, 0.13, 0.09]
# Shrinkage toward the positional base rate, in season-equivalents. Kept modest:
# a player with five straight healthy seasons has earned the benefit of the
# doubt, and over-shrinking here silently docks every durable starter 1-2 games.
_SHRINK = 0.9


def _classify(parts: str) -> tuple:
    """Split a season's injury body-parts into soft-tissue and structural sets."""
    soft, struct = set(), set()
    for raw in (parts or "").split(","):
        p = raw.strip().lower()
        if not p:
            continue
        for key in SOFT_TISSUE:
            if key in p:
                soft.add(key)
        for key in STRUCTURAL:
            if key in p:
                struct.add(key)
    return soft, struct


def build_injury_profiles(conn) -> dict:
    """One availability/risk record per player with a gsis id."""
    # Weeks flagged Out/Doubtful per season, plus the body parts involved.
    hist = defaultdict(dict)
    for r in conn.execute("SELECT * FROM injury_history"):
        hist[r["gsis_id"]][r["season"]] = {
            "out": r["weeks_out"] or 0,
            "dnp": r["weeks_dnp"] or 0,
            "q": r["weeks_questionable"] or 0,
            "parts": r["body_parts"] or "",
        }

    # Games with recorded snaps, as an independent read on availability.
    #
    # Snap counts are keyed by (player, season, TEAM), so a player traded
    # mid-season has two rows. They must be SUMMED - he played both halves.
    # Taking the larger of the two read Jakobi Meyers' 16-game 2025 as 9 games
    # and marked him injury-prone for it; 45 player-seasons were affected, and
    # every one of them was a trade, not an injury. Capped at a full season
    # because the two rows can overlap in the week a player changes hands.
    played = defaultdict(dict)
    for r in conn.execute(
        "SELECT s.pfr_id, s.season, s.games, p.gsis_id FROM snaps s "
        "JOIN players p ON p.pfr_id = s.pfr_id WHERE p.gsis_id IS NOT NULL"
    ):
        prev = played[r["gsis_id"]].get(r["season"], 0)
        played[r["gsis_id"]][r["season"]] = min(prev + (r["games"] or 0), GAMES)

    # Fall back to games with a stat line for players we cannot match to snaps.
    statgames = defaultdict(dict)
    for r in conn.execute("SELECT gsis_id, season, games FROM player_season"):
        statgames[r["gsis_id"]][r["season"]] = r["games"] or 0

    out = {}
    rows = conn.execute(
        "SELECT player_id, gsis_id, position, age, years_exp, injury_status, "
        "injury_body, injury_notes, practice FROM players"
    ).fetchall()

    for p in rows:
        pos = p["position"]
        base = POS_AVAILABILITY_BASE.get(pos, 0.85)
        gid = p["gsis_id"]

        obs, weights = [], []
        soft_seasons, struct_recent = 0, {}
        for w, season in zip(_W, INJURY_SEASONS):
            h = hist.get(gid, {}).get(season)
            g = played.get(gid, {}).get(season) or statgames.get(gid, {}).get(season)
            if h is None and not g:
                continue
            if g:
                rate = min(g / GAMES, 1.0)
            else:
                rate = max(0.0, (GAMES - h["out"]) / GAMES)
            # A player listed Out for weeks but credited with games is usually a
            # mid-season return; trust the lower of the two reads.
            if h is not None and g:
                rate = min(rate, max(0.0, (GAMES - h["out"]) / GAMES) * 0.35 + rate * 0.65)
            # Floor a single season. One acute injury - a torn ACL in October -
            # says a player is injury-prone; it does not say he is a 30%-of-the-
            # season player from now on. Without this floor a lost season drags
            # the weighted average so far down that anyone returning from a major
            # injury projects as unstartable, which is a much stronger claim than
            # the evidence supports. Chronic risk is captured separately by the
            # soft-tissue recurrence count below.
            obs.append(max(rate, 0.45))
            weights.append(w)
            if h is not None:
                soft, struct = _classify(h["parts"])
                if soft:
                    soft_seasons += 1
                if struct and season >= INJURY_SEASONS[0] - 1:
                    struct_recent.update({k: STRUCTURAL[k] for k in struct if k in STRUCTURAL})

        if obs:
            wsum = sum(weights)
            observed = sum(o * w for o, w in zip(obs, weights)) / wsum
            n_eff = wsum / _W[0]
            durability = (n_eff * observed + _SHRINK * base) / (n_eff + _SHRINK)
        else:
            durability = base  # rookie or no history

        # Age. Running backs degrade fastest; everyone degrades eventually.
        age = p["age"] or 0
        if age:
            if pos == "RB" and age >= 27:
                durability *= 1.0 - 0.030 * (age - 26)
            elif pos in ("WR", "TE") and age >= 30:
                durability *= 1.0 - 0.022 * (age - 29)
            elif pos == "QB" and age >= 34:
                durability *= 1.0 - 0.018 * (age - 33)
        durability = min(max(durability, 0.35), 0.99)

        games = durability * GAMES
        notes, risk_score = [], 0.0

        # Live designation: subtract expected missed time directly.
        status = (p["injury_status"] or "").strip()
        if status:
            cost, sev = INJURY_STATUS_COST.get(status, (0.5, "low"))
            body = (p["injury_body"] or "").strip()
            # A knee/Achilles PUP is materially worse than a "questionable" hamstring.
            _soft, struct_now = _classify(body)
            if struct_now and status in ("IR", "PUP", "NFI", "DNR"):
                cost *= 1.25
            games -= cost
            risk_score += {"high": 3.0, "medium": 1.5, "low": 0.4}[sev]
            notes.append(f"Currently {status}" + (f" ({body})" if body else ""))

        # Recurring soft tissue is the best predictor of missing time again.
        if soft_seasons >= 3:
            games -= 1.1
            risk_score += 2.0
            notes.append(f"Soft-tissue injuries in {soft_seasons} of the last 5 seasons")
        elif soft_seasons == 2:
            games -= 0.5
            risk_score += 1.0
            notes.append("Repeat soft-tissue history")

        # Structural injury in the last ~18 months suppresses efficiency on return.
        eff_mult = 1.0
        if struct_recent:
            worst = min(struct_recent.values())
            eff_mult = worst
            risk_score += 1.5
            notes.append("Returning from " + "/".join(sorted(struct_recent)) +
                         f" - efficiency discounted {int((1 - worst) * 100)}%")

        if durability < 0.72 and not notes:
            notes.append(f"Missed time in {len([o for o in obs if o < 0.9])} of the "
                         f"last {len(obs)} seasons")

        games = min(max(games, 0.0), GAMES)
        if pos in ("K", "DST"):
            games, risk_score, eff_mult = GAMES, 0.0, 1.0
            notes = []

        risk = "high" if risk_score >= 3.0 else "medium" if risk_score >= 1.3 else "low"
        out[p["player_id"]] = {
            "games": games,
            "availability": games / GAMES,
            "durability": durability,
            "risk": risk,
            "risk_score": risk_score,
            "eff_mult": eff_mult,
            "note": "; ".join(notes) if notes else "No notable injury history",
            "status": status,
            "seasons_observed": len(obs),
        }
    return out
