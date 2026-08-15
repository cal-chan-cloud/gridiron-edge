/* Gridiron Edge - valuation and draft engine.
 *
 * A direct port of models/valuation.py and models/draft.py. It runs in the
 * browser because everything here depends on YOUR league - size, roster shape,
 * budget, which players are already gone - and none of that is known when the
 * daily data build runs.
 *
 * Python remains the reference implementation. tests.py executes this file under
 * node against the same inputs and asserts the two agree to the cent, so the
 * pair cannot drift apart silently.
 *
 * Pure functions, no DOM, no globals beyond the exported namespace, so it can be
 * loaded by the page or required by node.
 */
(function (root) {
  'use strict';

  const FLEX_ELIGIBLE = ['RB', 'WR', 'TE'];
  const SUPERFLEX_ELIGIBLE = ['QB', 'RB', 'WR', 'TE'];
  const ALL_POS = ['QB', 'RB', 'WR', 'TE', 'K', 'DST'];

  /* ------------------------------------------------------------ normal -- */
  /* Hart's double-precision algorithm for the standard normal CDF. The usual
   * Abramowitz & Stegun approximation is only good to ~1.5e-7, which is enough
   * for a probability but shows up as a mismatch against Python's math.erf in
   * the parity test. This is accurate to ~1e-15. */
  function normCdf(z) {
    if (!isFinite(z)) return z > 0 ? 1 : 0;
    const a = Math.abs(z);
    if (a > 37) return z > 0 ? 1 : 0;
    const e = Math.exp(-a * a / 2);
    let p;
    if (a < 7.07106781186547) {
      let b = 3.52624965998911e-02 * a + 0.700383064443688;
      b = b * a + 6.37396220353165;
      b = b * a + 33.912866078383;
      b = b * a + 112.079291497871;
      b = b * a + 221.213596169931;
      b = b * a + 220.206867912376;
      let c = 8.83883476483184e-02 * a + 1.75566716318264;
      c = c * a + 16.064177579207;
      c = c * a + 86.7807322029461;
      c = c * a + 296.564248779674;
      c = c * a + 637.333633378831;
      c = c * a + 793.826512519948;
      c = c * a + 440.413735824752;
      p = e * b / c;
    } else {
      let d = a + 0.65;
      d = a + 4 / d;
      d = a + 3 / d;
      d = a + 2 / d;
      d = a + 1 / d;
      p = e / (d * 2.506628274631);
    }
    return z > 0 ? 1 - p : p;
  }

  /* ------------------------------------------------------------- draft -- */
  function pickNumbers(slot, teams, rounds, reversal) {
    const picks = [];
    for (let rnd = 1; rnd <= rounds; rnd++) {
      // After a third-round reversal the parity inverts for round 3 AND every
      // round after it, not just round 3.
      const forward = (reversal && rnd >= 3) ? (rnd % 2 === 0) : (rnd % 2 === 1);
      const pos = forward ? slot : (teams - slot + 1);
      picks.push((rnd - 1) * teams + pos);
    }
    return picks;
  }

  /* P(still on the board at pick_no). The normal around ADP is truncated at the
   * top of the board, because nobody can be drafted before the draft starts. */
  function availability(player, pickNo) {
    const adp = player.adp;
    if (adp === null || adp === undefined) return 0.97;
    const sd = Math.max(player.adp_stdev || Math.max(adp * 0.18, 3.0), 1.2);
    const surviving = 1 - normCdf((pickNo - 0.5 - adp) / sd);
    const beforeBoard = normCdf((0.5 - adp) / sd);
    const denom = 1 - beforeBoard;
    if (denom <= 1e-9) return 0;
    return Math.min(Math.max(surviving / denom, 0), 1);
  }

  /* Expected value of the best of these players still available at a pick:
   *   sum over q of  value(q) x P(q available) x P(nobody better available) */
  function expectedBestAt(players, pickNo, key) {
    key = key || 'vorp';
    const ranked = players.slice().sort((a, b) => b.vorp - a.vorp).slice(0, 40);
    let exp = 0, noneBetter = 1;
    for (const q of ranked) {
      const pa = availability(q, pickNo);
      exp += (q[key] || 0) * pa * noneBetter;
      noneBetter *= (1 - pa);
      if (noneBetter < 0.001) break;
    }
    return exp;
  }

  /* Same walk, but returning the representative player and both scales. */
  function expectedBest(pool, pickNo) {
    const ranked = pool.slice(0, 120);
    let expVal = 0, expVorp = 0, noneBetter = 1, rep = null, repP = 0;
    for (const q of ranked) {
      const pa = availability(q, pickNo);
      const w = pa * noneBetter;
      expVal += q.auction_value * w;
      expVorp += q.vorp * w;
      if (w > repP) { rep = q; repP = w; }
      noneBetter *= (1 - pa);
      if (noneBetter < 0.001) break;
    }
    return { rep: rep, value: expVal, vorp: expVorp };
  }

  /* --------------------------------------------------------- valuation -- */
  function rosterSize(league) {
    return Object.values(league.roster).reduce((a, b) => a + (Number(b) || 0), 0);
  }

  function starterDemand(league, byPos) {
    const teams = league.teams;
    const roster = league.roster;
    const demand = {};
    for (const pos of ALL_POS) demand[pos] = (roster[pos] || 0) * teams;

    const flexSlots = (roster.FLEX || 0) * teams;
    if (flexSlots > 0) {
      const pool = [];
      for (const pos of FLEX_ELIGIBLE) {
        const arr = byPos[pos] || [];
        const start = demand[pos] || 0;
        for (const p of arr.slice(start, start + flexSlots)) pool.push([p.points, pos]);
      }
      pool.sort((a, b) => b[0] - a[0]);
      for (const [, pos] of pool.slice(0, flexSlots)) demand[pos] = (demand[pos] || 0) + 1;
    }
    if (roster.SUPERFLEX) demand.QB += roster.SUPERFLEX * teams;
    return demand;
  }

  function assignTiers(byPos) {
    for (const pos of Object.keys(byPos)) {
      const ranked = byPos[pos].slice().sort((a, b) => b.points - a.points);
      if (!ranked.length) continue;
      const head = ranked.slice(0, 60);
      const gaps = [];
      for (let i = 0; i < head.length - 1; i++) gaps.push(head[i].points - head[i + 1].points);
      if (!gaps.length) { ranked.forEach(p => { p.tier = 1; }); continue; }
      const avg = gaps.reduce((a, b) => a + b, 0) / gaps.length;
      const varr = gaps.reduce((a, g) => a + (g - avg) * (g - avg), 0) / gaps.length;
      const threshold = avg + 0.85 * Math.sqrt(varr);

      let tier = 1, size = 0;
      for (let i = 0; i < head.length; i++) {
        head[i].tier = tier;
        size++;
        if (i < gaps.length && ((gaps[i] >= threshold && size >= 2) || size >= 8)) {
          tier++; size = 0;
        }
      }
      ranked.slice(60).forEach(p => { p.tier = tier + 1; });
    }
  }

  function assignAuction(rows, league) {
    const teams = league.teams;
    const budget = league.auction_budget != null ? league.auction_budget : league.budget;
    const spots = rosterSize(league);
    const totalSpots = teams * spots;
    // Every roster spot costs at least $1, so that money is not distributable.
    const spendable = Math.max(teams * budget - totalSpots, 1);

    const draftable = rows.slice().sort((a, b) => b.vorp - a.vorp).slice(0, totalSpots);
    const pool = draftable.reduce((a, p) => a + (p.vorp > 0 ? p.vorp : 0), 0);

    for (const p of rows) {
      p.auction_value = (pool > 0 && p.vorp > 0) ? 1 + p.vorp * (spendable / pool) : 1;
      let prem = (p.tier <= 2) ? 0.16 : (p.tier <= 4) ? 0.10 : 0.05;
      if (p.injury_risk === 'high') prem -= 0.06;
      p.max_bid = p.auction_value * (1 + Math.max(prem, 0));
    }
  }

  function computeValues(input, league) {
    const rows = input.map(p => Object.assign({}, p));
    const byPos = {};
    for (const p of rows) (byPos[p.position] = byPos[p.position] || []).push(p);
    for (const pos of Object.keys(byPos)) byPos[pos].sort((a, b) => b.points - a.points);

    const demand = starterDemand(league, byPos);
    const teams = league.teams;

    // Kickers and defenses are streamed off waivers weekly, so the replacement
    // you can actually access sits near the TOP of the position, not at the
    // bottom of the starters. Priced against DST12 they float into the top 100.
    const replacement = {};
    for (const pos of Object.keys(byPos)) {
      const arr = byPos[pos];
      if (!arr.length) { replacement[pos] = 0; continue; }
      let idx = (pos === 'K' || pos === 'DST') ? Math.max(1, Math.floor(teams / 4))
                                               : (demand[pos] || 0);
      idx = Math.min(Math.max(idx, 1), arr.length - 1);
      replacement[pos] = arr[idx].points;
    }

    for (const p of rows) {
      p.replacement = replacement[p.position] || 0;
      p.vorp = p.points - p.replacement;
      p.vorp_floor = p.floor - p.replacement;
      p.vorp_ceiling = p.ceiling - p.replacement;
    }

    assignTiers(byPos);
    assignAuction(rows, league);

    rows.sort((a, b) => b.vorp - a.vorp);
    rows.forEach((p, i) => { p.overall_rank = i + 1; });
    for (const pos of Object.keys(byPos)) {
      byPos[pos].slice().sort((a, b) => b.vorp - a.vorp).forEach((p, i) => {
        p.pos_rank = i + 1;
        p.pos_label = pos + (i + 1);
      });
    }

    // Market rank vs model rank. Positive = the model likes him more than the
    // room does.
    const ranked = rows.filter(p => p.adp !== null && p.adp !== undefined)
                       .sort((a, b) => a.adp - b.adp);
    ranked.forEach((p, i) => { p.adp_rank = i + 1; });
    for (const p of rows) {
      if (p.adp === null || p.adp === undefined) {
        p.value_vs_adp = null; p.adp_rank = null;
      } else {
        p.value_vs_adp = p.adp_rank - p.overall_rank;
      }
    }
    return rows;
  }

  /* ----------------------------------------------------- recommendation -- */
  function rosterNeeds(roster, league) {
    const slots = league.roster;
    const have = {};
    for (const p of roster) have[p.position] = (have[p.position] || 0) + 1;
    const need = {};
    for (const pos of ALL_POS) need[pos] = Math.max((slots[pos] || 0) - (have[pos] || 0), 0);
    const flex = slots.FLEX || 0;
    if (flex) {
      let surplus = 0;
      for (const pos of FLEX_ELIGIBLE) surplus += Math.max((have[pos] || 0) - (slots[pos] || 0), 0);
      if (surplus < flex) { need.RB += 0.5; need.WR += 0.5; }
    }
    const sf = slots.SUPERFLEX || 0;
    if (sf) {
      let surplus = 0;
      for (const pos of SUPERFLEX_ELIGIBLE) surplus += Math.max((have[pos] || 0) - (slots[pos] || 0), 0);
      // In a superflex room the second QB is the scarcest startable asset there
      // is, so it carries the need rather than splitting it across the flex.
      if (surplus < sf) need.QB += 0.9;
    }
    return need;
  }

  function reasonFor(p, cost, surv, pos, filled, starters) {
    const bits = [];
    if (surv < 0.18) bits.push(`almost certainly gone by your next pick (${Math.round(surv * 100)}% to last)`);
    else if (surv > 0.65) bits.push(`likely still there next turn (${Math.round(surv * 100)}%)`);
    if (cost > 18) bits.push(`waiting on ${pos} costs about ${cost.toFixed(0)} points of value`);
    else if (cost < 4) bits.push(`${pos} is deep here - the next one is nearly as good`);
    if (p.value_vs_adp && p.value_vs_adp >= 12)
      bits.push(`the room is letting him slide ${p.value_vs_adp} spots past his value`);
    else if (p.value_vs_adp !== null && p.value_vs_adp !== undefined && p.value_vs_adp <= -12)
      bits.push(`going ${Math.abs(p.value_vs_adp)} spots earlier than the model justifies`);
    if (filled < starters) bits.push(`fills an open ${pos} starting spot`);
    if (p.injury_risk === 'high') bits.push('carries real injury risk');
    if (p.tier && p.tier <= 2) bits.push(`still in tier ${p.tier} at the position`);
    return bits.slice(0, 3).join('; ') || 'solid value at this pick';
  }

  function recommend(available, roster, myPick, nextPick, league, limit) {
    limit = limit || 40;
    const needs = rosterNeeds(roster, league);
    const slots = league.roster;
    const have = {}, byeByPos = {};
    for (const p of roster) {
      have[p.position] = (have[p.position] || 0) + 1;
      if (p.bye) (byeByPos[p.position] = byeByPos[p.position] || []).push(p.bye);
    }

    const byPos = {};
    for (const p of available) (byPos[p.position] = byPos[p.position] || []).push(p);
    const waitValue = {};
    for (const pos of Object.keys(byPos)) {
      waitValue[pos] = nextPick ? expectedBestAt(byPos[pos], nextPick) : 0;
    }

    const out = [];
    for (const p of available) {
      const pos = p.position;
      const cost = p.vorp - (waitValue[pos] || 0);
      const starters = slots[pos] || 0;
      const filled = have[pos] || 0;

      let needMult;
      if (pos === 'K' || pos === 'DST') needMult = filled >= starters ? 0.05 : 0.35;
      else if (filled < starters) needMult = 1.15;
      else if ((needs[pos] || 0) > 0) needMult = 1.05;
      else needMult = Math.max(0.55, 1 - 0.16 * (filled - starters));

      const byePen = (p.bye && (byeByPos[pos] || []).indexOf(p.bye) !== -1) ? 0.96 : 1;
      const score = (0.62 * p.vorp + 0.38 * Math.max(cost, 0)) * needMult * byePen;
      const surv = nextPick ? availability(p, nextPick) : 0;

      out.push(Object.assign({}, p, {
        score: score, cost_of_waiting: cost, survives_to_next: surv,
        wait_value: waitValue[pos] || 0, need_mult: needMult,
        reason: reasonFor(p, cost, surv, pos, filled, starters),
      }));
    }
    out.sort((a, b) => b.score - a.score);
    return out.slice(0, limit);
  }

  function planDraft(rows, slot, league, keepers, reversal) {
    const picks = pickNumbers(slot, league.teams, league.rounds, reversal);
    const kept = new Set(keepers || []);
    const pool = rows.filter(p => !kept.has(p.player_id));
    return picks.map((pick, i) => {
      const cands = [];
      for (const p of pool) {
        const pa = availability(p, pick);
        if (pa > 0.03) cands.push([p.vorp * pa, pa, p]);
      }
      cands.sort((a, b) => b[0] - a[0]);
      return {
        round: i + 1, pick: pick,
        candidates: cands.slice(0, 8).map(c => ({
          name: c[2].name, position: c[2].position, team: c[2].team,
          pos_label: c[2].pos_label, tier: c[2].tier, vorp: c[2].vorp,
          points: c[2].points, adp: c[2].adp, prob: c[1],
          auction_value: c[2].auction_value, injury_risk: c[2].injury_risk,
          player_id: c[2].player_id,
        })),
      };
    });
  }

  /* -------------------------------------------------------------- keeper -- */
  function keeperAnalysis(rows, keepers, league) {
    const teams = league.teams;
    const byId = {};
    for (const p of rows) byId[p.player_id] = p;

    const keptIds = new Set(keepers.map(k => k.player_id));
    const pool = rows.filter(p => !keptIds.has(p.player_id))
                     .sort((a, b) => b.vorp - a.vorp).slice(0, 150);

    const out = [];
    for (const k of keepers) {
      const p = byId[k.player_id];
      if (!p) continue;
      const rnd = parseInt(k.round, 10) || 1;
      const slot = Math.max(1, Math.min(teams, parseInt(k.slot, 10) || Math.floor(teams / 2)));
      const pickNo = (rnd - 1) * teams + slot;

      const base = expectedBest(pool, pickNo);
      const surplus = p.auction_value - base.value;
      out.push(Object.assign({}, p, {
        keeper_round: rnd, keeper_pick: pickNo,
        baseline_name: base.rep ? base.rep.name : '-',
        baseline_pos: base.rep ? base.rep.pos_label : '',
        baseline_value: base.value, baseline_vorp: base.vorp,
        surplus_dollars: surplus, surplus_vorp: p.vorp - base.vorp,
        verdict: surplus >= 15 ? 'Strong keep' : surplus >= 5 ? 'Keep'
               : surplus >= -3 ? 'Marginal' : 'Let him go',
      }));
    }
    out.sort((a, b) => b.surplus_dollars - a.surplus_dollars);
    return out;
  }

  /* ----------------------------------------------------------- inflation -- */
  function auctionInflation(rows, spent, league) {
    const teams = league.teams;
    const budget = league.auction_budget != null ? league.auction_budget : league.budget;
    const spots = rosterSize(league);

    const totalMoney = teams * budget;
    const gone = new Set(spent.map(s => s.player_id));
    const moneySpent = spent.reduce((a, s) => a + (Number(s.price) || 0), 0);
    const moneyLeft = totalMoney - moneySpent;
    const spotsLeft = teams * spots - spent.length;

    const remaining = rows.filter(p => !gone.has(p.player_id)).sort((a, b) => b.vorp - a.vorp);
    const draftable = remaining.slice(0, Math.max(spotsLeft, 1));
    const valueLeft = draftable.reduce((a, p) => a + p.auction_value, 0);

    const committedMin = Math.max(spotsLeft - draftable.length, 0);
    const spendableLeft = Math.max(moneyLeft - committedMin, 1);
    let factor = valueLeft > 0 ? spendableLeft / valueLeft : 1;
    factor = Math.min(Math.max(factor, 0.4), 2.5);

    // ABOVE 1 = more cash than talent left, so the rest get bid up.
    // BELOW 1 = the room has already spent, so what remains goes cheap.
    return {
      money_left: moneyLeft, money_spent: moneySpent, spots_left: spotsLeft,
      value_left: valueLeft, inflation: factor, inflation_pct: (factor - 1) * 100,
      buyers_market: factor < 1,
      note: factor > 1.06
        ? 'The room is sitting on its money - expect the players left to be bid above list price, so budget accordingly'
        : factor < 0.94
        ? 'The room has overspent - the players left should go below list price, so hold your cash and pounce'
        : 'Spending is tracking value so far',
    };
  }

  /* ------------------------------------------------------- team analysis -- */
  /* Grades a roster by comparing YOUR starters, slot by slot, against what an
   * average team in the same league would have in that slot.
   *
   * The benchmark is not hand-waved. In a 12-team league starting two backs, the
   * twelve RB1 slots across the league are filled by the top twelve backs — so
   * the average RB1 is the mean of RB1-12, and the average RB2 the mean of
   * RB13-24. Comparing your starter against that band is what "above average at
   * the position" actually means, and it falls straight out of the league
   * settings rather than out of an opinion about what a good back looks like.
   *
   * Everything here aggregates values the projection engine already produced, so
   * it adds no new modelling — only a way of reading what is there. */
  function benchmarkBands(rows, league) {
    const teams = league.teams;
    const slots = league.roster;
    // Bands run over the FULL pool, drafted or not: the benchmark is what an
    // average team ends up with across a whole draft, not what happens to be
    // left on the board at this moment.
    const full = {};
    for (const p of rows) (full[p.position] = full[p.position] || []).push(p);
    for (const k of Object.keys(full)) full[k].sort((a, b) => b.points - a.points);

    const bands = {};
    for (const pos of ALL_POS) {
      const n = slots[pos] || 0;
      const arr = full[pos] || [];
      bands[pos] = [];
      for (let i = 0; i < n; i++) {
        const band = arr.slice(i * teams, (i + 1) * teams);
        bands[pos].push(band.length ? band.reduce((a, p) => a + p.points, 0) / band.length : 0);
      }
    }
    // FLEX: the best flex-eligible players left once every dedicated slot in the
    // league has been filled.
    const flexSlots = slots.FLEX || 0;
    if (flexSlots > 0) {
      const rest = [];
      for (const pos of FLEX_ELIGIBLE) {
        rest.push(...(full[pos] || []).slice((slots[pos] || 0) * teams));
      }
      rest.sort((a, b) => b.points - a.points);
      bands.FLEX = [];
      for (let i = 0; i < flexSlots; i++) {
        const band = rest.slice(i * teams, (i + 1) * teams);
        bands.FLEX.push(band.length ? band.reduce((a, p) => a + p.points, 0) / band.length : 0);
      }
    }
    // SUPERFLEX: QB-inclusive, and what is left after the dedicated slots AND
    // the flex have taken their share. In a superflex league this band is
    // dominated by QB2s, which is exactly the point.
    const sfSlots = slots.SUPERFLEX || 0;
    if (sfSlots > 0) {
      const rest = [];
      for (const pos of SUPERFLEX_ELIGIBLE) {
        const consumed = (slots[pos] || 0) * teams
          + (FLEX_ELIGIBLE.indexOf(pos) !== -1 ? (slots.FLEX || 0) * teams : 0);
        rest.push(...(full[pos] || []).slice(consumed));
      }
      rest.sort((a, b) => b.points - a.points);
      bands.SUPERFLEX = [];
      for (let i = 0; i < sfSlots; i++) {
        const band = rest.slice(i * teams, (i + 1) * teams);
        bands.SUPERFLEX.push(band.length ? band.reduce((a, p) => a + p.points, 0) / band.length : 0);
      }
    }
    return bands;
  }

  /* Assign a roster to starting slots, best player first, flex last. */
  function optimalLineup(roster, league) {
    const slots = league.roster;
    const used = new Set();
    const lineup = {};
    for (const pos of ALL_POS) {
      const n = slots[pos] || 0;
      if (!n) continue;
      const cands = roster.filter(p => p.position === pos && !used.has(p.player_id))
                          .sort((a, b) => b.points - a.points);
      lineup[pos] = cands.slice(0, n);
      lineup[pos].forEach(p => used.add(p.player_id));
    }
    const flexSlots = slots.FLEX || 0;
    if (flexSlots > 0) {
      const cands = roster.filter(p => FLEX_ELIGIBLE.indexOf(p.position) !== -1
                                    && !used.has(p.player_id))
                          .sort((a, b) => b.points - a.points);
      lineup.FLEX = cands.slice(0, flexSlots);
      lineup.FLEX.forEach(p => used.add(p.player_id));
    }
    // SUPERFLEX is filled after FLEX and is QB-eligible. Skipped entirely when
    // the league has no superflex slot, so the one-QB path is untouched.
    const sfSlots = slots.SUPERFLEX || 0;
    if (sfSlots > 0) {
      const cands = roster.filter(p => SUPERFLEX_ELIGIBLE.indexOf(p.position) !== -1
                                    && !used.has(p.player_id))
                          .sort((a, b) => b.points - a.points);
      lineup.SUPERFLEX = cands.slice(0, sfSlots);
      lineup.SUPERFLEX.forEach(p => used.add(p.player_id));
    }
    const bench = roster.filter(p => !used.has(p.player_id))
                        .sort((a, b) => b.points - a.points);
    return { lineup, bench };
  }

  function gradeOf(score) {
    return score >= 90 ? 'A+' : score >= 80 ? 'A' : score >= 70 ? 'B+'
         : score >= 60 ? 'B' : score >= 50 ? 'C+' : score >= 40 ? 'C'
         : score >= 28 ? 'D' : 'F';
  }

  function analyseTeam(rows, roster, league) {
    const slots = league.roster;
    const bands = benchmarkBands(rows, league);
    const lu = optimalLineup(roster, league);
    const lineup = lu.lineup, bench = lu.bench;

    const positions = [];
    let yourTotal = 0, avgTotal = 0;

    for (const pos of ALL_POS.concat(['FLEX', 'SUPERFLEX'])) {
      const n = slots[pos] || 0;
      if (!n) continue;
      const mine = lineup[pos] || [];
      const band = bands[pos] || [];
      // An empty slot is worth REPLACEMENT level — what you could still stream
      // off waivers — not zero. Scoring it as zero would overstate the hole.
      const repl = (pos === 'FLEX' || pos === 'SUPERFLEX')
        ? (band[band.length - 1] || 0) * 0.75
        : ((rows.find(p => p.position === pos) || {}).replacement || 0);

      let mineSum = 0;
      for (let i = 0; i < n; i++) mineSum += mine[i] ? mine[i].points : repl;
      const bandSum = band.reduce((a, b) => a + b, 0);
      yourTotal += mineSum;
      avgTotal += bandSum;

      const pct = bandSum > 0 ? (mineSum - bandSum) / bandSum : 0;
      // +/-50% against the slot benchmark spans the scale. Kicker and defense
      // are compressed toward the middle, because their projections are barely
      // distinguishable from each other in the first place.
      const spread = (pos === 'K' || pos === 'DST') ? 220 : 100;
      const score = Math.max(0, Math.min(100, 50 + pct * spread));
      positions.push({
        pos: pos, slots: n, filled: mine.length, players: mine,
        yourPoints: mineSum, avgPoints: bandSum, surplus: mineSum - bandSum,
        pct: pct, score: score, grade: gradeOf(score), unfilled: n - mine.length,
      });
    }

    // Overall, calibrated against what real drafted teams actually look like.
    // Simulating a 12-team snake where EVERY team drafts off this same board
    // puts all twelve between 47 and 53 — which is correct by construction: if
    // everyone drafts well, everyone is average. So the scale has to be tight
    // enough that the differences which do exist are legible. +/-16.7% spans it;
    // a team a genuine 8% ahead of the room lands around 74.
    const totalPct = avgTotal > 0 ? (yourTotal - avgTotal) / avgTotal : 0;
    const overall = Math.max(0, Math.min(100, 50 + totalPct * 300));

    // Only positions that decide anything can be "strongest" or "weakest".
    // Nobody needs to be told their kicker is below average.
    const ranked = positions.filter(p => p.pos !== 'K' && p.pos !== 'DST' && p.filled > 0)
                            .sort((a, b) => b.pct - a.pct);
    const gaps = positions.filter(p => p.unfilled > 0);

    // A week where several starters are simultaneously idle.
    const byWeek = {};
    for (const pos of Object.keys(lineup)) {
      for (const p of lineup[pos]) {
        if (p.bye) (byWeek[p.bye] = byWeek[p.bye] || []).push(p);
      }
    }
    const byeTrouble = Object.keys(byWeek)
      .filter(w => byWeek[w].length >= 3)
      .map(w => ({ week: Number(w), count: byWeek[w].length,
                   names: byWeek[w].map(p => p.name) }))
      .sort((a, b) => b.count - a.count);

    // How much of your starting production rests on players flagged high risk.
    const starters = [];
    for (const pos of Object.keys(lineup)) starters.push(...lineup[pos]);
    const startPts = starters.reduce((a, p) => a + p.points, 0) || 1;
    const risky = starters.filter(p => p.injury_risk === 'high');
    const riskShare = risky.reduce((a, p) => a + p.points, 0) / startPts;

    // If your best player at a position went down, what actually replaces him?
    const depth = [];
    for (const pos of ['RB', 'WR', 'TE', 'QB']) {
      if (!(slots[pos] || 0)) continue;
      const mine = (lineup[pos] || []).slice().sort((a, b) => b.points - a.points);
      if (!mine.length) continue;
      const backup = bench.filter(p => p.position === pos)[0];
      const drop = backup ? mine[0].points - backup.points : mine[0].points;
      depth.push({
        pos: pos, starter: mine[0].name, backup: backup ? backup.name : null,
        dropoff: drop, dropPct: mine[0].points ? drop / mine[0].points : 1,
      });
    }
    depth.sort((a, b) => b.dropPct - a.dropPct);

    return {
      overall: overall, grade: gradeOf(overall),
      yourPoints: yourTotal, avgPoints: avgTotal, surplus: yourTotal - avgTotal,
      totalPct: totalPct, positions: positions, lineup: lineup, bench: bench,
      strongest: ranked.length ? ranked[0] : null,
      weakest: ranked.length ? ranked[ranked.length - 1] : null,
      gaps: gaps, byeTrouble: byeTrouble,
      riskShare: riskShare, riskyStarters: risky.map(p => p.name),
      depth: depth, rosterSize: roster.length, slotsTotal: rosterSize(league),
    };
  }

  const Engine = {
    normCdf, pickNumbers, availability, expectedBestAt, expectedBest,
    rosterSize, starterDemand, computeValues, recommend, rosterNeeds,
    planDraft, keeperAnalysis, auctionInflation,
    analyseTeam, optimalLineup, benchmarkBands, gradeOf,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = Engine;
  root.Engine = Engine;
})(typeof globalThis !== 'undefined' ? globalThis : this);
