"""
2026 coaching staffs + play-calling tendencies.

Two layers, deliberately kept separate:

  Layer A (CURATED, this file): who is on each staff in 2026 and who calls plays.
    Verified 2026-08-11 against gridironexperts.com and profootballmania.com, with
    the three teams where nflverse's schedule `coach` column is STALE confirmed
    individually (ATL -> Stefanski, ARI -> Mike LaFleur, BUF -> Joe Brady).
    Do NOT trust games.csv's coach field for 2026; it still lists the 2025 coach
    for those teams.

  Layer B (COMPUTED, models/projection.py): what each coach's offenses have
    ACTUALLY done - pass rate, plays per game, points per game, RB committee
    shape - aggregated from every season they were a head coach, using nflverse
    team stats joined to the games.csv coach column for 1999-2025.

The qualitative `tree` / `identity` labels below are used for exactly one
numeric purpose: when a 2026 play-caller has no play-calling track record
(a first-time coordinator), we inherit the *computed* average of their
coaching tree instead of falling all the way back to league average. Everything
else about them is displayed as text only, never silently baked into a number.
"""
from __future__ import annotations

# tree: offensive lineage, used to borrow tendencies for coaches with no history.
#   shanahan  - outside/wide zone, heavy play-action, condensed formations, 12/21 personnel
#   mcvay     - shanahan derivative, 11 personnel, motion, tight splits
#   reid      - west coast + spread concepts, RB receiving usage, high red-zone creativity
#   payton    - aggressive early-down passing, big slot/TE usage
#   erhardt   - Patriots lineage, game-plan specific, matchup driven
#   airraid   - spread, tempo, 4-wide, high pass rate
#   gap       - gap/power run identity, condensed, lower neutral pass rate
COACHES_2026 = {
    "ARI": {"hc": "Mike LaFleur",         "oc": "Nathaniel Hackett",  "dc": "Nick Rallis",       "play_caller": "hc", "tree": "shanahan", "identity": "Wide-zone rushing attack with heavy play-action; LaFleur's first HC job after 3 years running the Rams offense."},
    "ATL": {"hc": "Kevin Stefanski",      "oc": "Tommy Rees",         "dc": "Jeff Ulbrich",      "play_caller": "hc", "tree": "shanahan", "identity": "Two-time Coach of the Year; 13-personnel wide zone, strong TE integration, run-first on early downs."},
    "BAL": {"hc": "Jesse Minter",         "oc": "Declan Doyle",       "dc": "Anthony Weaver",    "play_caller": "oc", "tree": "shanahan", "identity": "Defensive HC hands the offense to Doyle; designed-QB-run element with Lamar Jackson stays central."},
    "BUF": {"hc": "Joe Brady",            "oc": "Pete Carmichael",    "dc": "Jim Leonhard",      "play_caller": "hc", "tree": "payton",   "identity": "Promoted from OC; leaned run-heavy and efficient rather than volume-passing with Allen."},
    "CAR": {"hc": "Dave Canales",         "oc": "Brad Idzik",         "dc": "Ejiro Evero",       "play_caller": "hc", "tree": "shanahan", "identity": "Play-action heavy, condensed splits, committed to establishing the run."},
    "CHI": {"hc": "Ben Johnson",          "oc": "Press Taylor",       "dc": "Dennis Allen",      "play_caller": "hc", "tree": "airraid",  "identity": "Creative motion-and-tempo scheme, elite red-zone design, aggressive on early downs."},
    "CIN": {"hc": "Zac Taylor",           "oc": "Dan Pitcher",        "dc": "Al Golden",         "play_caller": "hc", "tree": "mcvay",    "identity": "11-personnel spread, extremely concentrated target distribution to the top two WRs."},
    "CLE": {"hc": "Todd Monken",          "oc": "Travis Switzer",     "dc": "Mike Rutenberg",    "play_caller": "hc", "tree": "airraid",  "identity": "Vertical, explosive-play hunting; first HC job after coordinating Baltimore and Georgia."},
    "DAL": {"hc": "Brian Schottenheimer", "oc": "Klayton Adams",      "dc": "Christian Parker",  "play_caller": "hc", "tree": "gap",      "identity": "Gap-scheme run game, condensed sets, wants balance but throws when trailing."},
    "DEN": {"hc": "Sean Payton",          "oc": "Davis Webb",         "dc": "Vance Joseph",      "play_caller": "hc", "tree": "payton",   "identity": "Aggressive situational play-caller; RB receiving usage and slot/TE volume are signatures."},
    "DET": {"hc": "Dan Campbell",         "oc": "Drew Petzing",       "dc": "Kelvin Sheppard",   "play_caller": "oc", "tree": "shanahan", "identity": "Campbell delegates play-calling; aggressive on 4th down, top-tier scoring environment."},
    "GB":  {"hc": "Matt LaFleur",         "oc": "Adam Stenavich",     "dc": "Jonathan Gannon",   "play_caller": "hc", "tree": "shanahan", "identity": "Wide zone + play-action; historically the most committee-heavy backfield and flattest WR target tree."},
    "HOU": {"hc": "DeMeco Ryans",         "oc": "Nick Caley",         "dc": "Matt Burke",        "play_caller": "oc", "tree": "erhardt",  "identity": "Defensive HC; Caley brings a matchup-driven Patriots/Rams hybrid with real TE usage."},
    "IND": {"hc": "Shane Steichen",       "oc": "Jim Bob Cooter",     "dc": "Lou Anarumo",       "play_caller": "hc", "tree": "reid",     "identity": "QB-run design, tempo, and a high-volume workhorse back."},
    "JAX": {"hc": "Liam Coen",            "oc": "Grant Udinski",      "dc": "Anthony Campanile", "play_caller": "hc", "tree": "mcvay",    "identity": "McVay/Shanahan hybrid; funnels targets to a clear alpha WR and uses the RB in the pass game."},
    "KC":  {"hc": "Andy Reid",            "oc": "Eric Bieniemy",      "dc": "Steve Spagnuolo",   "play_caller": "hc", "tree": "reid",     "identity": "Pass-first, elite red-zone efficiency, RB and TE heavily featured as receivers."},
    "LA":  {"hc": "Sean McVay",           "oc": "Nate Scheelhaase",   "dc": "Chris Shula",       "play_caller": "hc", "tree": "mcvay",    "identity": "Condensed 11 personnel with jet motion; hyper-concentrated target share to the WR1."},
    "LAC": {"hc": "Jim Harbaugh",         "oc": "Mike McDaniel",      "dc": "Chris O'Leary",     "play_caller": "oc", "tree": "shanahan", "identity": "Harbaugh's run-first identity now paired with McDaniel's motion-and-speed passing scheme."},
    "LV":  {"hc": "Klint Kubiak",         "oc": "Andrew Janocko",     "dc": "Rob Leonard",       "play_caller": "hc", "tree": "shanahan", "identity": "Kubiak wide-zone tree; heavy play-action and a bell-cow rushing workload."},
    "MIA": {"hc": "Jeff Hafley",          "oc": "Bobby Slowik",       "dc": "Sean Duggan",       "play_caller": "oc", "tree": "shanahan", "identity": "Defensive HC; Slowik keeps the Shanahan speed-and-motion passing game."},
    "MIN": {"hc": "Kevin O'Connell",      "oc": "Wes Phillips",       "dc": "Brian Flores",      "play_caller": "hc", "tree": "mcvay",    "identity": "Pass-heavy in neutral scripts, extremely concentrated to the WR1, strong scheme-created separation."},
    "NE":  {"hc": "Mike Vrabel",          "oc": "Josh McDaniels",     "dc": "Zak Kuhr",          "play_caller": "oc", "tree": "erhardt",  "identity": "Game-plan-specific attack; weekly role volatility is the cost of the matchup approach."},
    "NO":  {"hc": "Kellen Moore",         "oc": "Doug Nussmeier",     "dc": "Brandon Staley",    "play_caller": "hc", "tree": "payton",   "identity": "Vertical shot plays off play-action with a high early-down pass rate."},
    "NYG": {"hc": "John Harbaugh",        "oc": "Matt Nagy",          "dc": "Dennard Wilson",    "play_caller": "oc", "tree": "reid",     "identity": "Harbaugh's first year in New York; Nagy runs a Reid-tree offense with RB receiving usage."},
    "NYJ": {"hc": "Aaron Glenn",          "oc": "Frank Reich",        "dc": "Brian Duker",       "play_caller": "oc", "tree": "erhardt",  "identity": "Defensive HC; Reich brings a veteran, balanced, methodical approach."},
    "PHI": {"hc": "Nick Sirianni",        "oc": "Sean Mannion",       "dc": "Vic Fangio",        "play_caller": "oc", "tree": "gap",      "identity": "Gap-scheme run identity behind an elite line; first-time play-caller in Mannion is the wildcard."},
    "PIT": {"hc": "Mike McCarthy",        "oc": "Brian Angelichio",   "dc": "Patrick Graham",    "play_caller": "hc", "tree": "reid",     "identity": "West-coast rhythm passing with a firm commitment to early-down running."},
    "SEA": {"hc": "Mike Macdonald",       "oc": "Brian Fleury",       "dc": "Aden Durde",        "play_caller": "oc", "tree": "shanahan", "identity": "Defensive HC; Fleury is a first-time play-caller, which widens the range of outcomes."},
    "SF":  {"hc": "Kyle Shanahan",        "oc": "Klay Kubiak",        "dc": "Raheem Morris",     "play_caller": "hc", "tree": "shanahan", "identity": "The archetype: wide zone, play-action, YAC-heavy scheme that inflates RB and TE efficiency."},
    "TB":  {"hc": "Todd Bowles",          "oc": "Zac Robinson",       "dc": "Danny Smith",       "play_caller": "oc", "tree": "mcvay",    "identity": "Defensive HC; Robinson's McVay-tree offense leans on 11 personnel and a featured WR."},
    "TEN": {"hc": "Robert Saleh",         "oc": "Brian Daboll",       "dc": "Gus Bradley",       "play_caller": "oc", "tree": "erhardt",  "identity": "Defensive HC; Daboll's offense is QB-friendly and uses the RB heavily in the pass game."},
    "WAS": {"hc": "Dan Quinn",            "oc": "David Blough",       "dc": "Daronte Jones",     "play_caller": "hc", "tree": "shanahan", "identity": "Defensive HC with a first-time play-caller in Blough; wide range of offensive outcomes."},
}

# Head coach for 2025, used to detect genuine coaching turnover. Taken from the
# nflverse 2025 schedule (which IS reliable for completed seasons).
HC_2025 = {
    "ARI": "Jonathan Gannon", "ATL": "Raheem Morris", "BAL": "John Harbaugh",
    "BUF": "Sean McDermott", "CAR": "Dave Canales", "CHI": "Ben Johnson",
    "CIN": "Zac Taylor", "CLE": "Kevin Stefanski", "DAL": "Brian Schottenheimer",
    "DEN": "Sean Payton", "DET": "Dan Campbell", "GB": "Matt LaFleur",
    "HOU": "DeMeco Ryans", "IND": "Shane Steichen", "JAX": "Liam Coen",
    "KC": "Andy Reid", "LA": "Sean McVay", "LAC": "Jim Harbaugh",
    "LV": "Pete Carroll", "MIA": "Mike McDaniel", "MIN": "Kevin O'Connell",
    "NE": "Mike Vrabel", "NO": "Kellen Moore", "NYG": "Brian Daboll",
    "NYJ": "Aaron Glenn", "PHI": "Nick Sirianni", "PIT": "Mike Tomlin",
    "SEA": "Mike Macdonald", "SF": "Kyle Shanahan", "TB": "Todd Bowles",
    "TEN": "Brian Callahan", "WAS": "Dan Quinn",
}

# 2026 offensive coordinator hires that represent a change from 2025. Used to
# flag "new voice calling plays" even when the head coach stayed.
OC_2025 = {
    "ARI": "Drew Petzing", "ATL": "Zac Robinson", "BAL": "Todd Monken",
    "BUF": "Joe Brady", "CAR": "Brad Idzik", "CHI": "Declan Doyle",
    "CIN": "Dan Pitcher", "CLE": "Tommy Rees", "DAL": "Klayton Adams",
    "DEN": "Joe Lombardi", "DET": "John Morton", "GB": "Adam Stenavich",
    "HOU": "Nick Caley", "IND": "Jim Bob Cooter", "JAX": "Grant Udinski",
    "KC": "Matt Nagy", "LA": "Mike LaFleur", "LAC": "Greg Roman",
    "LV": "Chip Kelly", "MIA": "Frank Smith", "MIN": "Wes Phillips",
    "NE": "Josh McDaniels", "NO": "Doug Nussmeier", "NYG": "Mike Kafka",
    "NYJ": "Tanner Engstrand", "PHI": "Kevin Patullo", "PIT": "Arthur Smith",
    "SEA": "Klint Kubiak", "SF": "Klay Kubiak", "TB": "Josh Grizzard",
    "TEN": "Nick Holz", "WAS": "Kliff Kingsbury",
}

# Name normalisation: nflverse spells a couple of coaches differently.
NAME_FIXES = {
    "Klint Kubliak": "Klint Kubiak",
    "Nathan Scheelhaase": "Nate Scheelhaase",
    "Pete Carmichael Jr.": "Pete Carmichael",
}

TREE_LABELS = {
    "shanahan": "Shanahan wide-zone",
    "mcvay": "McVay 11-personnel",
    "reid": "Reid west-coast",
    "payton": "Payton vertical",
    "erhardt": "Erhardt-Perkins matchup",
    "airraid": "Spread / tempo",
    "gap": "Gap-scheme power",
}


def fix_name(name: str) -> str:
    return NAME_FIXES.get((name or "").strip(), (name or "").strip())


def play_caller(team: str) -> str:
    """Name of the person actually calling offensive plays in 2026."""
    c = COACHES_2026.get(team)
    if not c:
        return ""
    return c["hc"] if c["play_caller"] == "hc" else c["oc"]


def staff_changes(team: str) -> dict:
    """What changed on this staff for 2026, and how disruptive it is."""
    c = COACHES_2026.get(team)
    if not c:
        return {"hc_changed": False, "oc_changed": False, "caller_changed": False, "severity": 0.0}
    hc_changed = HC_2025.get(team) != c["hc"]
    oc_changed = OC_2025.get(team) != c["oc"]
    caller = play_caller(team)

    # Whether the PLAY-CALLER changed is a different question from whether the
    # staff changed. Chicago hired a new coordinator, but Ben Johnson still calls
    # the plays himself, so nothing about how that offense operates is new. Only
    # compare the seat that actually holds the call sheet.
    if c["play_caller"] == "hc":
        prev_caller = HC_2025.get(team)
        caller_changed = hc_changed
    else:
        prev_caller = OC_2025.get(team)
        caller_changed = oc_changed

    # Severity drives how far we move a team off its own statistical baseline.
    # A new play-caller is the disruptive event; a staff change around a
    # returning play-caller is a much smaller one.
    severity = 0.0
    if caller_changed:
        severity += 0.75
        if hc_changed and oc_changed:
            severity += 0.25
    elif hc_changed or oc_changed:
        severity += 0.15
    severity = min(severity, 1.0)
    return {
        "hc_changed": hc_changed,
        "oc_changed": oc_changed,
        "caller_changed": caller_changed,
        "prev_hc": HC_2025.get(team),
        "prev_oc": OC_2025.get(team),
        "prev_caller": prev_caller,
        "new_caller": caller,
        "severity": severity,
    }


def all_teams() -> list:
    return sorted(COACHES_2026)
