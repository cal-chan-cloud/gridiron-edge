# Gridiron Edge — 2026 fantasy football draft intelligence

A local site that turns free NFL data into draft decisions: a projection model, a
snake-draft advisor, a keeper valuator, and a live auction tracker. Everything is
computed from real data that refreshes daily; nothing is hand-typed rankings.

    http://127.0.0.1:5057

---

## Putting it on the internet (GitHub Pages)

The site is a static build: Python does everything that depends only on the data,
the browser does everything that depends on your league. That means it can be
hosted for free, forever, with no server to keep alive.

```
GitHub Actions, 11:30 UTC daily
  └─ fetch upstream → verify freshness → build docs/ → run tests → (encrypt) → deploy
GitHub Pages serves docs/
  └─ data/*.json  +  engine.js (valuation + draft maths, in the browser)
```

**One-time setup**

1. Create a repo and push:
   ```powershell
   cd "C:\Users\Calvin Chan\fantasy_football"
   git init && git add -A && git commit -m "Gridiron Edge"
   git branch -M main
   git remote add origin https://github.com/<you>/gridiron-edge.git
   git push -u origin main
   ```
2. **Settings → Pages → Source: GitHub Actions.**
3. **Settings → Actions → General → Workflow permissions: Read and write**
   (the daily job commits the ADP history snapshot back).
4. Optional but recommended — **Settings → Secrets and variables → Actions → New
   repository secret**, named `SITE_PASSPHRASE`. Ten characters minimum.
5. **Actions → Daily data refresh → Run workflow** to build it immediately rather
   than waiting for the cron.

Your URL will be `https://<you>.github.io/gridiron-edge/`.

### About that password

**GitHub Pages cannot check a password.** There is no server to check it, so any
gate implemented purely in JavaScript is bypassed by opening devtools or
requesting the JSON directly. A lock that does not lock is worse than none,
because you would trust it.

So if `SITE_PASSPHRASE` is set, the build **encrypts the data itself**
(AES-256-GCM, key derived by PBKDF2-SHA256 at 250k iterations). What gets
published is ciphertext; the page asks for the passphrase, derives the key and
decrypts in your browser. Without it there is nothing to read — verified by
grepping the published payload for player names.

Honest limits: anyone you give the passphrase to can pass it on, and because the
ciphertext is public it can be attacked offline, so use a real phrase rather than
a word. The build refuses anything under ten characters.

If you would rather have proper accounts, deploy the same `docs/` folder to
**Cloudflare Pages** and turn on **Cloudflare Access** — free for up to 50 users
and gives genuine email-based login. Nothing in the build changes.

### What is and is not committed

Neither the database nor the built site is committed. `fantasy.db` is 2.7MB of
binary that changes on every run — a gigabyte a year of useless diffs — and it is
rebuilt from upstream in about fifteen seconds anyway. `docs/` would add ~5MB
daily and is published as a Pages artifact instead.

The one piece of state that genuinely cannot be re-derived is the day-over-day
ADP history behind the "movers" view, since it is a record of what the market
looked like on days that have passed. That is committed as
`data/adp_history.csv` — a ~200KB text file, pruned to the ten-day window the
view actually reads, so it does not grow without bound.

## Running it

```powershell
cd "C:\Users\Calvin Chan\fantasy_football"
.\run_site.bat            # starts it windowless if not already up
```

Refresh the data by hand at any time:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe" -m data.fetch_all
& "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe" -m data.assert_fresh
```

`--full` on `fetch_all` also re-pulls the large historical files (last season's
stats, snap counts, five years of injury reports). The daily run skips those
because they don't change.

### Dependencies

Only `requests` and `flask`. **The pipeline and model are stdlib-only — no pandas,
no numpy.** That is deliberate: the interpreter Windows Task Scheduler runs (the
python.org build at `%LocalAppData%\Programs\Python\Python311`) does not have
pandas installed, while the MS Store build on `PATH` does. Depending on pandas
would make the daily task fail in exactly the silent way that has bitten other
trackers on this machine. If you add a dependency, install it against *that*
interpreter, not whatever `python` resolves to.

---

## The five tabs

**Board** — every player ranked by VORP, with projections, tiers, auction value,
live ADP, and the gap between the two. Click any row for the full breakdown:
projected box score, target/carry share, range of outcomes, injury history,
production history, and the coaching context behind the number.

> Filter to a single position and the board draws **tier breaks** with a running
> count ("3 players left at this grade", and a warning colour on the last one).
> Tiers are the thing that actually decides whether to reach now or wait a round,
> and a number in a column does not communicate that — the rule across the board
> does. **? Columns** explains the jargon; **Export** downloads a CSV cheat sheet,
> and the page has a print stylesheet for a paper copy.

**Snake Draft** — pick your slot (1–N, with optional third-round reversal) and it
plans the draft around it. Mark players as *Mine* or *Gone* as the draft runs and
the recommendations update. Drafting is **keyboard-first**, because a draft is a
timed event: <kbd>1</kbd>–<kbd>6</kbd> takes that recommendation, <kbd>shift</kbd>+number marks him gone to
someone else, <kbd>u</kbd> undoes, <kbd>/</kbd> focuses search. A scarcity strip across the top
shows how many players remain *at the current grade* for each position, the
round-by-round panel becomes a draft log once picks are spent, and your roster
flags two starters at the same position sharing a bye. The ranking metric is
**not** raw value — it is:

> VORP(player) − E[VORP of the best player at that position who survives to your next pick]

which is literally what waiting on that position costs you. A back you can still
have two rounds from now is worth less right now than a receiver who will
certainly be gone. Each recommendation says why in plain English, and the sidebar
projects who is likely to be there at every one of your remaining picks.

**Keeper** — add the players you can keep and the round each one costs. The
surplus compares the keeper against *the best player realistically still on the
board at that pick*, taken as an expectation over who survives — because the real
question is never "is this player good", it is "is he better than what that pick
would otherwise buy me".

**Auction** — target price and max bid for every player, plus live inflation. As
you record what players actually sell for, every remaining value re-prices
itself. If the room overspends early there is less money chasing the same talent
and everyone left goes cheap; if the room sits on its cash, the players left get
bid up. That number is the biggest edge in an auction and almost nobody tracks it.
Tick **mine** when you win a player and the page also tracks *your* budget, your
remaining roster spots, and the largest bid you can still make while filling
every slot — players above that line are greyed out.

**Team & Injury Intel** — all 32 coaching staffs with what changed for 2026, each
play-caller's measured career tendencies, team pace and pass rate, vacated
opportunity, bye weeks; an injury watchlist; and day-over-day ADP movers.

---

## The model

For every player on a 2026 roster:

1. **Opportunity.** Historical share of the team's targets and carries, weighted
   for recency and sample size, shrunk toward a depth-chart prior, and discounted
   if the player changed teams. Rookies start from the depth prior scaled by
   draft capital.

2. **Normalisation.** A team can only throw so many passes. Shares are rescaled
   so each team's pool balances — which is what makes vacated volume redistribute
   automatically when a team loses its WR1. Two details matter here:
   - The constraint is `Σ(share × games/17) = 1`, not `Σ(share) = 1`. Otherwise
     every game anyone misses *deletes* those targets instead of handing them to
     a backup. Getting this wrong drained ~14% of the league's opportunity.
   - The correction is allocated by *uncertainty*, not evenly. A proven
     28%-target-share tight end shouldn't be taxed as hard as the eighth receiver
     on a 90-man August roster just because his team signed depth.

3. **Volume** = share × the team's projected attempts, which already carry the
   coaching and pace adjustments.

4. **Efficiency.** Yards per target/carry/attempt shrunk toward positional means
   by opportunity count, nudged by Next Gen separation and yards after contact.

5. **Touchdowns**, regressed hardest of anything — a player's own TD rate blended
   with what his usage implies. TD rate is the noisiest input in fantasy and the
   biggest driver of both busts and breakouts.

6. **Availability** — projected games from the injury model, with an efficiency
   discount for players returning from structural injury.

Then replacement level (from your actual roster settings, with flex allocated to
whichever position fills it) converts points into VORP, tiers, and auction dollars.

### Coaching

Two layers, kept separate on purpose:

- **Curated** (`coaching.py`): who is on each 2026 staff and who calls plays,
  verified 2026-08-11 against two sources. Note that nflverse's schedule `coach`
  column is **stale** for ATL, ARI and BUF — it still lists the 2025 coach. Those
  three were confirmed individually.
- **Computed** (`models/context.py`): what each coach's offenses have *actually*
  done — pace, pass rate, points — aggregated from every season they ran a team.
  No opinions. A first-time coordinator with no record inherits his coaching
  tree's measured average and the site says so rather than inventing precision.

Ten teams changed head coach for 2026 and 21 changed coordinator, so this matters
more than usual this year.

### Injuries

Five seasons of weekly injury reports, cross-checked against games with recorded
snaps. Soft-tissue injuries (hamstring, groin, calf) are treated as a *recurrence*
risk; structural ones (ACL, Achilles, Lisfranc) discount the following season's
efficiency. Live designations come from Sleeper and are subtracted directly — a
player on PUP in August starts the year in a hole. A single lost season is floored
so that one acute injury doesn't permanently mark a player unstartable.

### Deliberate positions

- **Kickers and defenses are suppressed.** Their year-to-year signal is close to
  noise, so their projections are shrunk toward the positional mean, and their
  replacement level is set near the *top* of the position because you stream them
  off waivers weekly. Priced honestly they kept surfacing inside the top 100
  overall, which is exactly the pick this site exists to talk you out of.
- **Projections are flatter than last year's results, on purpose.** The realised
  top 12 at a position is always an ex-post selection of players who got lucky and
  stayed healthy. Model top-12 lands at 78–90% of realised top-12, which is where
  sound projections sit.
- **Model vs market correlation is ~0.69.** High enough to be sane, low enough to
  be useful — the disagreements are the point.

---

## Data sources (all free, no API keys)

| Source | Used for |
|---|---|
| **Sleeper** | player master, live injury designation, practice status |
| **nflverse** | 2026 rosters + depth charts, 2023–25 player/team production, snap counts, 5 seasons of injury reports, advanced rushing, Next Gen Stats, 2026 schedule with posted betting lines |
| **FantasyFootballCalculator** | live ADP for PPR / half-PPR / standard, from thousands of real drafts |

The 2026 schedule gives bye weeks and strength of schedule outright. Only ~52 of
272 games have betting lines posted in August, so market-implied scoring is used
as a *weak* prior and the sample size is shown in the UI.

---

## Daily refresh

Windows Scheduled Task **`GridironEdge-DailyFetch`**, 06:30 daily, runs
`run_daily_fetch.bat`. Verified working — it has run end to end under the
scheduler and exited 0.

Hardening applied, each of which corresponds to a way a scheduled task on this
machine has actually failed before:

- Action is `cmd.exe` with the batch path passed as a **quoted** argument.
  Registering the `.bat` directly splits on the space in `Calvin Chan` and the
  scheduler tries to execute `C:\Users\Calvin` → `0x800700C1`.
- `StopIfGoingOnBatteries=false`, `DisallowStartIfOnBatteries=false`. The default
  sends a running task Control-C on unplug.
- `StartWhenAvailable=true` so a missed run catches up.
- The batch file hardcodes the python.org interpreter, because the MS Store alias
  is a 0-byte reparse point that fails to launch in a non-interactive session.
- **Success is defined as data that landed, not a process that finished.**
  `data/assert_fresh.py` re-reads the database and fails the task unless today's
  fetch stamped, ADP is present and recent for all three formats, the roster and
  schedule are populated, and the model still builds to sane numbers. A fetcher
  exiting 0 having written nothing is the failure mode that hides for weeks.

Check it with:

```powershell
Get-ScheduledTaskInfo -TaskName "GridironEdge-DailyFetch" | Select LastRunTime, LastTaskResult, NextRunTime
Get-Content .\logs\fetch.log -Tail 20
```

`LastTaskResult` of `0` here genuinely means the data is fresh, because of the
gate above.

### Two things not yet automated

- **Site autostart.** `GridironEdge-Site` could not be registered — creating a
  logon-triggered task returned *Access is denied* under a non-elevated shell.
  To add it, run this from an **elevated** PowerShell:

  ```powershell
  $bat = "C:\Users\Calvin Chan\fantasy_football\run_site.bat"
  Register-ScheduledTask -TaskName "GridironEdge-Site" `
    -Action (New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c ""{0}""' -f $bat)) `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
    -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew)
  ```

  Until then, start it with `run_site.bat` when you want it.

- **S4U principal.** The fetch task runs under the interactive token, so it fires
  when you're logged on (and catches up if you weren't). To make it run
  regardless, from an elevated shell:

  ```powershell
  Set-ScheduledTask -TaskName "GridironEdge-DailyFetch" `
    -Principal (New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType S4U -RunLevel Limited)
  ```

---

## Note when editing the UI

`app.py` serves the **built** site out of `docs/`, not the sources in `static/`
and `templates/`. So after editing any frontend file:

```powershell
python export_static.py     # copies static/* and renders templates/index.html into docs/
```

Without that step the change appears to have silently failed, because the server
is still serving the previous build. `app.py` re-exports automatically on startup
when the *data* has moved on, but it cannot know you edited a stylesheet.

(It leaves an encrypted build alone rather than overwriting ciphertext with
plaintext, so re-run `node encrypt_build.js` if you were testing that path.)

## Layout

```
config.py              scoring presets, league defaults, every model constant
coaching.py            2026 staffs (curated, verified) + play-caller logic
data/
  sources.py           HTTP + CSV plumbing, caching, name/team normalisation
  fetch_all.py         all fetchers  ->  fantasy.db
  assert_fresh.py      the freshness gate the daily task is judged by
models/
  database.py          SQLite schema (WAL, so fetch writes while the site reads)
  context.py           team pace/pass rate, coach fingerprints, vacated volume
  injury.py            durability, risk, current designations
  projection.py        the projection engine
  valuation.py         replacement level, VORP, tiers, auction $, keeper, inflation
  draft.py             snake order, survival probability, pick recommendation
export_static.py       builds docs/ — the thing that actually gets served
encrypt_build.js       optional AES-GCM pass over docs/data (node, no deps)
app.py                 static file server for docs/ (the SAME build Pages serves)
tests.py               109 invariant checks, incl. Python<->JS parity
static/
  engine.js            valuation + draft maths, in the browser
  app.js               UI
  gate.js              passphrase gate for encrypted builds
  style.css
templates/index.html   rendered into docs/index.html by the exporter
docs/                  the build (gitignored; published as a Pages artifact)
```

The Python model and `static/engine.js` implement the same valuation maths in two
languages, which is a real drift risk — so `tests.py` runs the JS under node
against identical inputs and asserts the results are bit-identical.

---

## Verifying the arithmetic

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe" tests.py
```

90 checks across scoring, volume conservation, shrinkage, availability, VORP,
auction dollars, draft mechanics, keeper, inflation, data integrity and the
coaching layer. They are invariants, not unit tests of implementation detail —
because every real bug in this project was silent wrongness that crashed nothing.

The anchor check is **scoring**: applying the PPR preset to 2025 component stats
reproduces nflverse's own independently-computed `fantasy_points_ppr` for
**657/657 player-seasons exactly**. If that formula were wrong, every projection
built on it would be wrong the same way, and nothing else would reveal it.

Run it after any model change. Several of the checks below were added *because*
they would have caught a bug that shipped.

## Model audit (2026-08-11)

A pass over the model after the initial build found these. All are fixed and
regression-checked; none changed the headline board much, but each was wrong:

- **Third-round reversal was wrong from round 4 onward.** Only round 3 was
  flipped, leaving round 4 backward too — at slot 1 it produced picks
  `24, 36, 48` instead of `24, 36, 37`. The order was still legal (every pick
  used once), which is exactly why it survived the first check. After a 3RR the
  parity inverts for *every* subsequent round.
- **Mid-season trades were being read as injuries.** Snap counts are keyed by
  (player, season, **team**), so a traded player has two rows. They were being
  combined with `max` instead of summed, turning Jakobi Meyers' 16-game season
  into 9 games and marking him injury-prone. 45 player-seasons affected — every
  one a trade, not an injury.
- **Survival probability ignored that the draft starts at pick 1.** A raw normal
  around ADP puts mass on picks below 1, so the consensus 1.01 was reported as
  only 66% likely to be available *at the first pick*. The distribution is now
  truncated at the top of the board and renormalised.
- **Efficiency priors were hand-typed, not measured** — despite a comment
  claiming otherwise. They ran high (RB yards per target 6.3 vs 5.79 actual; QB
  yards per carry 5.4 vs 4.40), biasing every low-sample player upward. Now
  computed from the loaded seasons, so the model recalibrates itself each year.
- **Fumbles used an unexplained 0.55 haircut.** Replaced with proper shrinkage
  toward the measured positional rate by opportunity count.
- **Draft capital never reached quarterbacks.** The rookie boost was applied to
  target and carry shares only, so a first-overall pick listed behind a veteran
  projected as a permanent backup. High picks now carry a floor on their share.
- **Two-point conversions were defined in the scoring config and never scored.**
- The projection cache cleared *every* entry rather than superseded ones, so
  switching scoring format recomputed both sides each time.

Second pass, driven by the verification harness:

- **Fumbles counted special-teams fumbles as offensive ones.**
  `fumbles_lost_total` includes fumbles on kick and punt returns, which are not
  offensive touches. Charging them against a player's fumble-per-touch rate
  penalised exactly the young, athletic receivers who double as returners — DJ
  Moore, Tetairoa McMillan, Josh Downs and Jalen Hurts were all carrying phantom
  points. Switching to `rushing + receiving + sack` fumbles lost took the scoring
  reconstruction from 98.37% to **100.00%** exact.
- **Two-point conversions were the one season TOTAL in a table of rates.**
  `_weighted()` sample-weights its inputs, which only means anything for rates,
  so a player who scored a conversion in a half season was credited with half the
  rate. Now stored per game like everything else.
- Return touchdowns are now stored, so the scoring check is a strict equality
  rather than an approximate one. They are still deliberately not projected
  forward — under half a score a season for even a designated returner, and we
  have no reliable read on 2026 return duties.
- Removed three genuinely unused imports; the rest of the codebase is clean.

Two "failures" in the first harness run turned out to be bugs in the *tests*,
not the model, and are worth recording because they are the easy mistake:
the board was being built without `attach_adp`, so every player had `adp=None`,
`availability()` fell back to its flat no-ADP constant, and every pick-dependent
assertion passed while testing nothing. The harness now asserts ADP attached
before it proceeds.

## Cross-source gotchas worth knowing

These caused real, silent wrongness during the build and are now handled:

- **Sleeper calls Arizona `AZ`; everyone else says `ARI`.** This split the
  Cardinals into two half-teams and wrecked every Arizona projection.
- **Accents differ between sources** (`Pineiro` vs `Piñeiro`), so names are
  diacritic-folded before joining or players silently vanish from the ADP join.
- **nflverse rosters sometimes have a blank `sleeper_id`**, which was inserting
  ~140 players a second time. A duplicate isn't cosmetic — it inflates a team's
  share denominator and taxes every real contributor on that roster.
- **Kickers are `PK` in ADP, `K` in Sleeper; defenses are `DEF`/`DST`** and every
  source spells team names differently, so defenses join on team abbreviation.
- **nflverse's 2026 `coach` column is stale.** See the coaching section.
