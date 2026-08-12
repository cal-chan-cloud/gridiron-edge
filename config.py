"""
Gridiron Edge - central configuration.

IMPORTANT: this project is deliberately **stdlib-only** (plus `requests` + `flask`).
No pandas / numpy anywhere. Reason: the interpreter Windows Task Scheduler uses
(the python.org build at %LocalAppData%\\Programs\\Python\\Python311) does NOT have
pandas or numpy installed, while the MS Store build does. Depending on pandas would
make the daily task fail in exactly the silent way that has bitten the other
trackers on this machine. Everything here runs on csv + sqlite3 + statistics.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "fantasy.db"

for _d in (CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Season / server
# --------------------------------------------------------------------------
# The target season. Env-driven so the whole model can be pointed at a PAST
# season and scored against what actually happened (see backtest.py) — and so it
# rolls forward to 2027 without edits.
SEASON = int(os.environ.get("FF_SEASON", "2026"))
# Seasons of history pulled for the projection model, newest first.
HISTORY_SEASONS = [SEASON - 1, SEASON - 2, SEASON - 3]
# Deeper history, used only for injury/durability rates and coach career profiles.
INJURY_SEASONS = [SEASON - n for n in range(1, 6)]
COACH_HISTORY_START = 2012

# Optional model inputs, each independently switchable so backtest.py can ablate
# them and measure whether they actually earn their place. Defaults are the
# shipping configuration. Never guess whether a feature helps — turn it off and
# score it.
# Settings below are what backtesting 2023, 2024 and 2025 actually supports.
# Each number is the mean change in rank correlation when the feature is toggled,
# across three held-out seasons (`python backtest.py --ablate`). "sign flips"
# means it helped in some seasons and hurt in others, i.e. it is noise.
FEATURES = {
    # EARN THEIR PLACE — large, same sign in all three seasons.
    "td_regression": True,      # -0.0304 when removed. The single biggest input.
    "availability": True,       # -0.0236 when removed. Projecting games matters.

    # SMALL BUT CONSISTENT — same sign in all three seasons.
    "efficiency_epa": True,     # +0.0034 when added. EPA per opportunity carries
                                # game state and leverage that raw yardage does not.
    "ngs_separation": True,     # -0.0022 when removed. Marginal, but never harmful.

    # MEASURED AND REJECTED.
    "adv_rush_yac": False,      # +0.0018 when REMOVED, same sign 3/3: yards after
                                # contact consistently cost a little accuracy. It
                                # duplicates information already in yards per carry.
    "implied_points": False,    # sign flips (-0.004 / +0.010 / +0.002) and the
                                # mechanism is thin anyway: in August only ~3 games
                                # per team have a line posted. Removed as noise.
    "sos": False,               # exactly 0.0000 mean. Strength of schedule is
                                # computed and DISPLAYED, but deliberately does not
                                # feed a projection, because it measurably does not help.
    "snap_share": False,        # sign flips (+0.018 in 2023, negative in 2024/25).
                                # Snap share is already implicit in target share.

    # RETAINED ON PRIOR GROUNDS, NOT ON EVIDENCE — stated plainly rather than
    # dressed up. Three seasons of ~300 players cannot resolve either of these.
    "age_curve": True,          # sign flips. Kept because the aging effect is well
                                # established and the sample contains few players at
                                # the extremes where it bites; it is damped to 55%.
    "coach_blend": True,        # sign flips, BUT the backtest forces curated_coaching
                                # off, so it only ever tested the blend mechanism with
                                # schedule-derived coaches — never the hand-verified
                                # 2026 staff list (10 HC and 21 OC changes). A null
                                # here is weak evidence about the shipping feature.

    "curated_coaching": True,   # 2026 staff table; off when backtesting a past year
}


def feature(name: str) -> bool:
    """Env override wins, so a backtest can flip a flag without editing config."""
    env = os.environ.get("FF_FEAT_" + name.upper())
    if env is not None:
        return env not in ("0", "false", "False", "")
    return FEATURES.get(name, False)

PORT = int(os.environ.get("FF_PORT", "5057"))
HOST = os.environ.get("FF_HOST", "127.0.0.1")

HTTP_TIMEOUT = 90
HTTP_RETRIES = 3
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GridironEdge/1.0"

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
FFC_ADP = "https://fantasyfootballcalculator.com/api/v1/adp/{fmt}?teams={teams}&year={year}&position=all"

# --------------------------------------------------------------------------
# Scoring presets. Every number on the site is recomputed from whichever of
# these the user picks, so the league format is never baked in.
# --------------------------------------------------------------------------
SCORING_PRESETS = {
    "ppr": {
        "label": "Full PPR",
        "pass_yd": 0.04, "pass_td": 4.0, "interception": -2.0, "pass_2pt": 2.0,
        "rush_yd": 0.1, "rush_td": 6.0, "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
        "fumble_lost": -2.0, "rec_2pt": 2.0, "rush_2pt": 2.0,
        "bonus_pass_300": 0.0, "bonus_rush_100": 0.0, "bonus_rec_100": 0.0,
        "te_premium": 0.0,
    },
    "half_ppr": {
        "label": "Half PPR",
        "pass_yd": 0.04, "pass_td": 4.0, "interception": -2.0, "pass_2pt": 2.0,
        "rush_yd": 0.1, "rush_td": 6.0, "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0,
        "fumble_lost": -2.0, "rec_2pt": 2.0, "rush_2pt": 2.0,
        "bonus_pass_300": 0.0, "bonus_rush_100": 0.0, "bonus_rec_100": 0.0,
        "te_premium": 0.0,
    },
    "standard": {
        "label": "Standard (no PPR)",
        "pass_yd": 0.04, "pass_td": 4.0, "interception": -2.0, "pass_2pt": 2.0,
        "rush_yd": 0.1, "rush_td": 6.0, "rec": 0.0, "rec_yd": 0.1, "rec_td": 6.0,
        "fumble_lost": -2.0, "rec_2pt": 2.0, "rush_2pt": 2.0,
        "bonus_pass_300": 0.0, "bonus_rush_100": 0.0, "bonus_rec_100": 0.0,
        "te_premium": 0.0,
    },
    "superflex": {
        "label": "Superflex / 2QB (PPR)",
        "pass_yd": 0.04, "pass_td": 6.0, "interception": -2.0, "pass_2pt": 2.0,
        "rush_yd": 0.1, "rush_td": 6.0, "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
        "fumble_lost": -2.0, "rec_2pt": 2.0, "rush_2pt": 2.0,
        "bonus_pass_300": 0.0, "bonus_rush_100": 0.0, "bonus_rec_100": 0.0,
        "te_premium": 0.0,
    },
    "te_premium": {
        "label": "TE Premium (PPR + 0.5/rec TE)",
        "pass_yd": 0.04, "pass_td": 4.0, "interception": -2.0, "pass_2pt": 2.0,
        "rush_yd": 0.1, "rush_td": 6.0, "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
        "fumble_lost": -2.0, "rec_2pt": 2.0, "rush_2pt": 2.0,
        "bonus_pass_300": 0.0, "bonus_rush_100": 0.0, "bonus_rec_100": 0.0,
        "te_premium": 0.5,
    },
}
DEFAULT_SCORING = "half_ppr"

# ADP source format keyed by scoring. FFC only publishes these three.
FFC_FORMAT_FOR_SCORING = {
    "ppr": "ppr", "half_ppr": "half-ppr", "standard": "standard",
    "superflex": "ppr", "te_premium": "ppr",
}

# --------------------------------------------------------------------------
# League structure defaults (all overridable from the UI)
# --------------------------------------------------------------------------
DEFAULT_LEAGUE = {
    "teams": 12,
    "rounds": 16,
    "scoring": DEFAULT_SCORING,
    # SUPERFLEX is the second QB-eligible starting slot. It stays at 0 for a
    # normal league; setting it to 1 is what actually re-prices quarterbacks,
    # because it doubles league-wide QB starter demand and so drags replacement
    # level down a full tier. Changing only the scoring preset does not do that.
    "roster": {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "SUPERFLEX": 0,
               "K": 1, "DST": 1, "BENCH": 6},
    "flex_eligible": ["RB", "WR", "TE"],
    "auction_budget": 200,
    "keeper_max": 3,
}

POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"]
SKILL_POSITIONS = ["QB", "RB", "WR", "TE"]

# Games in a season, and the games a healthy player is expected to be rested for.
GAMES = 17

# --------------------------------------------------------------------------
# Model constants
#
# These are the knobs of the projection engine. They are gathered here rather
# than scattered through the model so they can be tuned in one place.
# --------------------------------------------------------------------------

# Recency weights applied to HISTORY_SEASONS (2025, 2024, 2023).
SEASON_WEIGHTS = [0.60, 0.28, 0.12]

# Shrinkage constants. An observed rate is pulled toward its prior as
#   (n*observed + K*prior) / (n + K)
# where n is the OPPORTUNITY COUNT in the same units as K. Small samples get
# pulled hard toward the prior; a full season of volume barely moves.
# Usage share is the STICKIEST thing a player carries year to year (season-over-
# season correlation runs ~0.7-0.75 for target and carry share), so it is shrunk
# only lightly. Touchdown rate, the noisiest, is shrunk hardest. Getting this
# ordering backwards compounds four separate haircuts onto elite players and
# flattens the top of the board, which is exactly where draft value is decided.
SHRINK = {
    "target_share_games": 5.0,   # units: games
    "carry_share_games": 4.0,    # units: games
    "ypt_targets": 45.0,         # units: targets
    "ypc_carries": 70.0,         # units: carries
    "ypa_attempts": 180.0,       # units: pass attempts
    "td_rate_opp": 90.0,         # units: targets or carries
}

# A player joining a new team carries less of their old role with them; their
# effective sample is discounted by this before shrinking toward the depth prior.
NEW_TEAM_SAMPLE_DISCOUNT = 0.55

# Pass attempts are dropbacks minus sacks; league sack rate runs ~6.5%.
ATTEMPTS_PER_DROPBACK = 0.935
# Not every attempt has a charted target (throwaways, spikes, batted balls):
# the league threw 17,439 attempts against 16,609 targets last season.
TARGETS_PER_ATTEMPT = 0.952

# Touchdown rates regress much harder than yardage - TD rate is the noisiest
# thing in fantasy football and the single biggest source of bust/breakout.
# Weight on the player's OWN td rate vs. the position/usage-implied expectation.
TD_PERSONAL_WEIGHT = {"QB": 0.45, "RB": 0.30, "WR": 0.28, "TE": 0.28}

# League-average team pace/efficiency fallbacks, refreshed from data at build
# time; these are only used if a team has no usable history.
LEAGUE_FALLBACK = {
    "plays_per_game": 63.0,
    "pass_rate": 0.575,
    "points_per_game": 22.5,
}

# How hard a coaching change moves a team off its own baseline toward the new
# play-caller's career profile. 0 = ignore coaching, 1 = coach fully overrides.
COACH_BLEND_WITH_HISTORY = 0.45     # new play-caller who has a track record
COACH_BLEND_NO_HISTORY = 0.22       # first-time play-caller -> mostly regress to mean

# Age curves: multiplier applied to projected per-game production by position.
# Derived from the well-established shape of NFL aging (RBs fall off a cliff,
# WRs peak mid-20s and hold, TEs peak later, QBs hold longest).
AGE_CURVES = {
    "RB": {21: 0.97, 22: 1.00, 23: 1.02, 24: 1.03, 25: 1.02, 26: 1.00, 27: 0.96,
           28: 0.90, 29: 0.84, 30: 0.77, 31: 0.70, 32: 0.63, 33: 0.57},
    "WR": {21: 0.90, 22: 0.94, 23: 0.98, 24: 1.01, 25: 1.03, 26: 1.03, 27: 1.02,
           28: 1.01, 29: 0.99, 30: 0.95, 31: 0.90, 32: 0.85, 33: 0.79, 34: 0.73},
    "TE": {22: 0.85, 23: 0.90, 24: 0.95, 25: 0.99, 26: 1.02, 27: 1.03, 28: 1.03,
           29: 1.02, 30: 1.00, 31: 0.96, 32: 0.91, 33: 0.86, 34: 0.80},
    "QB": {22: 0.95, 23: 0.97, 24: 0.99, 25: 1.01, 26: 1.02, 27: 1.03, 28: 1.03,
           29: 1.03, 30: 1.02, 31: 1.01, 32: 1.00, 33: 0.99, 34: 0.97, 35: 0.95,
           36: 0.92, 37: 0.89, 38: 0.85, 39: 0.81, 40: 0.77},
}

# Baseline share of the season a healthy player at each position is available.
POS_AVAILABILITY_BASE = {"QB": 0.89, "RB": 0.855, "WR": 0.875, "TE": 0.87, "K": 0.97, "DST": 1.0}

# Current-injury designations -> (expected games missed to start 2026, risk label).
INJURY_STATUS_COST = {
    "IR":            (8.0, "high"),
    "PUP":           (6.0, "high"),
    "NFI":           (5.0, "high"),
    "Sus":           (3.0, "medium"),
    "Out":           (2.0, "medium"),
    "Doubtful":      (1.2, "medium"),
    "Questionable":  (0.5, "low"),
    "Probable":      (0.2, "low"),
    "DNR":           (4.0, "high"),
    "COV":           (0.5, "low"),
}

# Body parts whose injuries recur and/or suppress production after return.
SOFT_TISSUE = {"hamstring", "groin", "calf", "quadricep", "quad", "hip", "adductor"}

# Multiplier applied to the following season's efficiency. Restricted to
# injuries that genuinely predict reduced production on return.
#
# Minor entries (ankle, shoulder, wrist, hand) were removed deliberately. Almost
# every NFL player appears on an injury report with one at some point across five
# seasons, so including them tagged ~90% of the league as "returning from injury"
# — which both buried the players with a real problem and made the note
# meaningless. Ordinary knocks still cost a player games; that shows up properly
# in the availability model rather than as a phantom efficiency penalty.
# "neck" and "back" are excluded for the same reason as ankles: a stinger is
# reported the same way a serious cervical injury is, and the common case
# dominates. Only injuries that are unambiguously season-altering are listed.
STRUCTURAL = {
    "acl": 0.90, "achilles": 0.86, "lisfranc": 0.89, "patellar": 0.89,
    "microfracture": 0.90,
}

# Positional priors for share-of-team-volume by depth chart rank. Used for
# players with no usable history (rookies, role changes, new signings).
#
# Calibrated so that a full team's priors sum to roughly the real league split:
# targets go ~56% WR / ~21% TE / ~21% RB, and carries go ~80% RB / ~15% QB /
# ~4% WR. Getting the carry split wrong is not cosmetic - RB priors that summed
# to ~1.0 left no room for quarterback rushing and badly under-projected every
# mobile QB after normalisation.
DEPTH_PRIOR_TARGET_SHARE = {
    "WR": [0.225, 0.150, 0.092, 0.048, 0.026, 0.014],
    "TE": [0.140, 0.045, 0.018, 0.009],
    "RB": [0.108, 0.056, 0.028, 0.014],
}
DEPTH_PRIOR_CARRY_SHARE = {
    "RB": [0.450, 0.205, 0.090, 0.040, 0.018],
    "WR": [0.020, 0.010, 0.005],
    "QB": [0.130, 0.030],
    "TE": [0.004],
}

# Guard rail on share normalisation. If a team's raw shares sum far from the
# expected total we are looking at missing data (an unpublished depth chart, a
# roster feed that has not caught up), not a real distribution. Rescaling
# without a limit turns that gap into a 3x projection for a camp body.
NORMALISE_SCALE_BOUNDS = (0.65, 1.60)

# How much of a position's projected spread is actually PREDICTABLE, in advance,
# from prior data. Skill positions carry real signal year to year. Kicker and
# defense scoring is close to noise: the correlation between one season's finish
# and the next is near zero, so a model that confidently ranks K1 over K12 is
# fooling itself. Projections at those positions are shrunk toward the positional
# mean by these factors, which is what stops a kicker showing up as the 56th most
# valuable asset in the draft.
POSITION_RELIABILITY = {"QB": 1.0, "RB": 1.0, "WR": 1.0, "TE": 1.0, "K": 0.22, "DST": 0.32}

# Rookie draft capital -> opportunity multiplier on the depth-chart prior.
ROOKIE_CAPITAL_BOOST = [
    (10, 1.45), (32, 1.28), (64, 1.12), (105, 1.00), (150, 0.90), (999, 0.80),
]
