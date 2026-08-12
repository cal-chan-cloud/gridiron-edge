"""
Build a fully static copy of the site into docs/, for GitHub Pages.

Why this exists: Pages serves files, not Python. Anything that has to happen per
request - re-pricing the board when you change league size, working out who
survives to your next pick - therefore has to happen in the browser. So the split
is:

  PYTHON  does everything that depends only on the data and the scoring system:
          fetch, projections, coaching context, injury profiles. Output is a
          handful of JSON files, refreshed daily by GitHub Actions.

  BROWSER does everything that depends on YOUR league: replacement level, VORP,
          tiers, auction dollars, draft recommendations. That lives in
          web/engine.js and is a direct port of models/valuation.py and
          models/draft.py.

Two implementations of the same maths is a real risk, so tests.py runs both over
the same inputs and asserts they agree to the cent. If they ever drift, the
harness fails.

Run:  python export_static.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from coaching import COACHES_2026, TREE_LABELS, play_caller, staff_changes
from config import (
    BASE_DIR, DEFAULT_LEAGUE, FFC_FORMAT_FOR_SCORING, SCORING_PRESETS, SEASON,
)
from models.context import build_team_context
from models.database import db, get_meta
from models.injury import build_injury_profiles
from models.projection import project

DOCS = BASE_DIR / "docs"
DATA = DOCS / "data"

# Fields the browser needs. Kept tight: this payload is downloaded on a phone,
# possibly on draft-room wifi.
PLAYER_FIELDS = (
    "player_id", "name", "position", "team", "age", "years_exp", "depth", "bye",
    "games", "points", "ppg", "floor", "ceiling", "confidence", "breakout",
    "availability", "injury_risk", "injury_note", "injury_status",
    "notes", "coach_note", "caller", "sos",
    "pass_att", "pass_yd", "pass_td", "ints", "carries", "rush_yd", "rush_td",
    "targets", "receptions", "rec_yd", "rec_td", "tgt_share", "car_share",
    "adp", "adp_stdev", "adp_formatted", "times_drafted",
)


def _round(v, nd=3):
    """Trim float noise so the payload compresses well."""
    if isinstance(v, float):
        return round(v, nd)
    return v


def _attach_adp(conn, rows, fmt):
    """Bake ADP straight into the payload - the browser should not have to join."""
    adp = {(r["search_name"], r["position"]): r
           for r in conn.execute("SELECT * FROM adp WHERE fmt=?", (fmt,))}
    ids = {r["player_id"]: (r["search_name"], r["position"])
           for r in conn.execute("SELECT player_id, search_name, position FROM players")}
    for p in rows:
        key = ids.get(p["player_id"])
        a = adp.get(key) if key else None
        if a:
            p["adp"] = a["adp"]
            p["adp_formatted"] = a["adp_formatted"]
            p["adp_stdev"] = a["stdev"] or max(a["adp"] * 0.18, 3.0)
            p["times_drafted"] = a["times_drafted"]
        else:
            p["adp"] = None
            p["adp_stdev"] = None
            p["adp_formatted"] = ""
            p["times_drafted"] = 0
    return rows


def write_json(path: Path, payload) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text)


def build() -> int:
    if DOCS.exists():
        for child in DOCS.iterdir():
            if child.name == ".nojekyll":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    DATA.mkdir(parents=True, exist_ok=True)
    # Pages otherwise runs Jekyll over the output and drops files starting "_".
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    total = 0
    with db() as conn:
        ctx = build_team_context(conn)
        inj = build_injury_profiles(conn)

        # --- projections, one file per scoring system (lazy-loaded by the page)
        for key in SCORING_PRESETS:
            rows = project(conn, key, ctx, inj)
            fmt = FFC_FORMAT_FOR_SCORING.get(key, "ppr")
            rows = _attach_adp(conn, rows, fmt)
            slim = [{k: _round(p.get(k)) for k in PLAYER_FIELDS} for p in rows]
            n = write_json(DATA / f"proj_{key}.json", slim)
            total += n
            print(f"  proj_{key}.json{'':<8} {len(slim):5d} players  {n/1024:7.0f} KB")

        # --- team + coaching context
        teams = []
        for team, c in sorted(ctx.items()):
            staff = COACHES_2026.get(team, {})
            chg = staff_changes(team)
            caller = play_caller(team)
            prof = conn.execute("SELECT * FROM coach_profile WHERE coach=?", (caller,)).fetchone()
            teams.append({
                "team": team, "hc": staff.get("hc"), "oc": staff.get("oc"),
                "dc": staff.get("dc"), "caller": caller,
                "tree": TREE_LABELS.get(staff.get("tree"), staff.get("tree")),
                "identity": staff.get("identity"),
                "hc_changed": chg["hc_changed"], "oc_changed": chg["oc_changed"],
                "prev_hc": chg["prev_hc"], "prev_oc": chg["prev_oc"],
                "plays_pg": _round(c["plays_pg"], 2), "pass_rate": _round(c["pass_rate"], 4),
                "exp_ppg": _round(c.get("exp_ppg"), 2), "bye": c["bye"],
                "sos": _round(c["sos"], 4),
                "vacated_target_pct": _round(c["vacated_target_pct"], 4),
                "vacated_carry_pct": _round(c["vacated_carry_pct"], 4),
                "note": c["note"],
                "caller_profile": ({k: _round(prof[k], 4) for k in prof.keys()} if prof else None),
            })
        total += write_json(DATA / "teams.json", {"teams": teams})
        print(f"  teams.json{'':<14} {len(teams):5d} teams")

        # --- per-player history, one file, loaded only when a card is opened
        hist = {}
        for r in conn.execute(
            "SELECT ps.*, p.player_id AS pid FROM player_season ps "
            "JOIN players p ON p.gsis_id = ps.gsis_id "
            "WHERE p.team IS NOT NULL AND p.team != '' ORDER BY ps.season DESC"
        ):
            hist.setdefault(r["pid"], {"history": [], "injuries": []})["history"].append({
                "season": r["season"], "team": r["team"], "games": r["games"],
                "carries": _round(r["carries"], 0), "rushing_yards": _round(r["rushing_yards"], 0),
                "rushing_tds": _round(r["rushing_tds"], 0), "targets": _round(r["targets"], 0),
                "receptions": _round(r["receptions"], 0),
                "receiving_yards": _round(r["receiving_yards"], 0),
                "receiving_tds": _round(r["receiving_tds"], 0),
                "attempts": _round(r["attempts"], 0), "passing_yards": _round(r["passing_yards"], 0),
                "passing_tds": _round(r["passing_tds"], 0),
            })
        for r in conn.execute(
            "SELECT ih.*, p.player_id AS pid FROM injury_history ih "
            "JOIN players p ON p.gsis_id = ih.gsis_id "
            "WHERE p.team IS NOT NULL AND p.team != '' ORDER BY ih.season DESC"
        ):
            hist.setdefault(r["pid"], {"history": [], "injuries": []})["injuries"].append({
                "season": r["season"], "weeks_out": r["weeks_out"], "weeks_dnp": r["weeks_dnp"],
                "weeks_questionable": r["weeks_questionable"], "body_parts": r["body_parts"],
            })
        n = write_json(DATA / "history.json", hist)
        total += n
        print(f"  history.json{'':<12} {len(hist):5d} players  {n/1024:7.0f} KB")

        # --- ADP movement, computed here so the browser does not need the table
        movers = []
        for fmt in sorted(set(FFC_FORMAT_FOR_SCORING.values())):
            dates = [r["date"] for r in conn.execute(
                "SELECT DISTINCT date FROM adp_history WHERE fmt=? ORDER BY date DESC LIMIT 8",
                (fmt,))]
            if len(dates) < 2:
                continue
            new, old = dates[0], dates[-1]
            cur = {r["search_name"]: r["adp"] for r in conn.execute(
                "SELECT search_name, adp FROM adp_history WHERE fmt=? AND date=?", (fmt, new))}
            prev = {r["search_name"]: r["adp"] for r in conn.execute(
                "SELECT search_name, adp FROM adp_history WHERE fmt=? AND date=?", (fmt, old))}
            names = {r["search_name"]: (r["name"], r["position"], r["team"]) for r in conn.execute(
                "SELECT search_name, name, position, team FROM adp WHERE fmt=?", (fmt,))}
            rows = []
            for k, v in cur.items():
                if k in prev and prev[k] and v and abs(prev[k] - v) >= 1.0 and k in names:
                    rows.append({"name": names[k][0], "position": names[k][1],
                                 "team": names[k][2], "adp": v, "prev": prev[k],
                                 "delta": _round(prev[k] - v, 2)})
            rows.sort(key=lambda x: -abs(x["delta"]))
            movers.append({"fmt": fmt, "from": old, "to": new, "movers": rows[:40]})
        total += write_json(DATA / "movers.json", {"formats": movers})
        print(f"  movers.json{'':<13} {len(movers):5d} formats")

        # --- meta
        adp_win = conn.execute(
            "SELECT window_start, window_end, total_drafts FROM adp LIMIT 1").fetchone()
        n_players = conn.execute(
            "SELECT COUNT(*) n FROM players WHERE team IS NOT NULL AND team!=''").fetchone()["n"]
        total += write_json(DATA / "meta.json", {
            "season": SEASON,
            "last_fetch": get_meta(conn, "last_fetch"),
            "data_version": get_meta(conn, "data_version"),
            "players": n_players,
            "adp_window": [adp_win["window_start"], adp_win["window_end"]] if adp_win else None,
            "adp_drafts": adp_win["total_drafts"] if adp_win else 0,
            "scoring_presets": {k: v["label"] for k, v in SCORING_PRESETS.items()},
            "defaults": DEFAULT_LEAGUE,
            "static": True,
        })

    # --- frontend. index.html is a Jinja template with exactly one variable.
    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{ season }}", str(SEASON)).replace("/static/", "")
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    for name in ("style.css", "app.js", "engine.js", "gate.js"):
        src = BASE_DIR / "static" / name
        if src.exists():
            shutil.copy2(src, DOCS / name)
        else:
            print(f"  ! missing static/{name}")

    print(f"\nWrote {DOCS} - {total/1024/1024:.2f} MB of JSON (gzips to roughly a third)")
    return 0


if __name__ == "__main__":
    sys.exit(build())
