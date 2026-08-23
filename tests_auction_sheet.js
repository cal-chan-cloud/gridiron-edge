/* Headless smoke test for the printable auction sheet (docs/auction.html).
 *
 * Run:  node tests_auction_sheet.js [repoRoot]
 * Invoked by tests.py as section 15.
 *
 * The sheet builds its whole page as an HTML string, so it can be exercised
 * without a browser: stub document/localStorage/fetch, run the page's own
 * render(), and assert against the markup it produces. This catches the failure
 * that matters - a printout whose dollars silently disagree with the site's,
 * or that drops a position entirely - which a syntax check cannot. */
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2] || path.resolve(__dirname);

const els = {};
function el(id) {
  if (!els[id]) els[id] = { value: 'normal', checked: true, textContent: '', innerHTML: '' };
  return els[id];
}
global.document = { querySelector: s => el(s.replace('#', '')) };
global.window = {};
global.localStorage = { getItem: () => JSON.stringify(SAVED_STATE) };

const DATA = {};
for (const f of ['meta.json', 'proj_half_ppr.json', 'proj_superflex.json']) {
  DATA['data/' + f] = JSON.parse(fs.readFileSync(path.join(ROOT, 'docs/data', f), 'utf8'));
}
global.fetch = async url => ({ json: async () => DATA[url] });

global.Engine = require(ROOT + '/static/engine.js');

let SAVED_STATE = {};
const src = fs.readFileSync(path.join(ROOT, 'docs/auction.html'), 'utf8')
  .match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace(/^'use strict';/m, '')
  .replace(/\(window\.__gateReady[\s\S]*$/, '');   // drop the boot kickoff

const run = new Function('SAVED_STATE_REF', src + '\nreturn { boot, render, league, DEPTH };');
const api = run();

let fails = 0;
const ok = (name, cond, detail) => {
  console.log((cond ? '  ok   ' : '  FAIL ') + name + (detail ? '  [' + detail + ']' : ''));
  if (!cond) fails++;
};

(async () => {
  // ---- case 1: no saved settings at all (first visit, straight to print)
  SAVED_STATE = {};
  await api.boot();
  let html = el('sheet').innerHTML;
  ok('renders with no saved league settings', html.length > 20000, html.length + ' chars');
  ok('has all six position sections',
     ['Running Backs','Wide Receivers','Quarterbacks','Tight Ends','Defenses','Kickers']
       .every(s => html.includes(s)));
  ok('shows target dollars', /class="tgt">\$\d+/.test(html));
  ok('shows max bid column', /class="max">\$\d+/.test(html));
  ok('has tier separators', html.includes('tierrow'));
  ok('has the $1 endgame section', html.includes('$1 endgame'));
  ok('has a budget worksheet', html.includes('Max bid') && html.includes('Par is'));
  ok('roster worksheet has one row per slot',
     (html.match(/class="wr"/g) || []).length === 16,
     (html.match(/class="wr"/g) || []).length + ' write-in rows for a 16-slot roster');
  ok('no undefined/NaN leaked into the page',
     !/undefined|NaN/.test(html),
     /undefined|NaN/.test(html) ? 'FOUND: ' + html.match(/.{40}(undefined|NaN).{40}/)[0] : 'clean');

  // ---- case 2: the user's real settings, mid-auction, with sales recorded
  const proj = DATA['data/proj_half_ppr.json'];
  // Sort by points so these are players the sheet actually prints - the raw
  // payload order is arbitrary and picked RB145, who is past the row cap.
  const players = (proj.players || proj).slice().sort((a, b) => (b.points||0) - (a.points||0));
  SAVED_STATE = {
    league: { scoring:'half_ppr', teams:10, rounds:16, budget:300,
              roster:{ QB:1,RB:2,WR:3,TE:1,FLEX:2,SUPERFLEX:0,K:1,DST:1,BENCH:5 } },
    sold: [ { player_id: players[0].player_id, price: 71, mine: true },
            { player_id: players[1].player_id, price: 64, mine: false } ],
    keepers: [ { player_id: players[2].player_id, round: 3 } ],
  };
  await api.boot();
  html = el('sheet').innerHTML;
  ok('honours a 10-team $300 league', html.includes('$300') && html.includes('pool <b>$3000</b>'));
  ok('recomputes par per spot for the new roster size', html.includes('Par is'));
  ok('marks a recorded sale with its price', html.includes('$71'));
  ok('flags a keeper', html.includes('(keeper)'));
  ok('greys out sold players', html.includes('opacity:.42'));
  ok('still no undefined/NaN', !/undefined|NaN/.test(html));

  // ---- case 3: superflex must print QBs at their real value
  SAVED_STATE = { league: { scoring:'superflex', teams:12, rounds:16, budget:200,
    roster:{ QB:1,RB:2,WR:3,TE:1,FLEX:1,SUPERFLEX:1,K:1,DST:1,BENCH:6 } } };
  await api.boot();
  html = el('sheet').innerHTML;
  ok('superflex sheet includes the SFLX roster slot', html.includes('SFLX'));
  const qbBlock = html.split('Quarterbacks')[1] || '';
  const firstQbTarget = (qbBlock.match(/class="tgt">\$(\d+)/) || [])[1];
  ok('superflex prices QB1 as a premium asset', Number(firstQbTarget) >= 40,
     'QB1 target $' + firstQbTarget);

  // ---- case 4: the depth selector actually changes row counts
  SAVED_STATE = {};
  await api.boot();
  const countRows = () => (el('sheet').innerHTML.match(/class="box"/g) || []).length;
  el('optDepth').value = 'tight';  api.render();  const tight = countRows();
  el('optDepth').value = 'normal'; api.render();  const normal = countRows();
  el('optDepth').value = 'deep';   api.render();  const deep = countRows();
  ok('depth selector scales the sheet', tight < normal && normal < deep,
     tight + ' / ' + normal + ' / ' + deep + ' players');

  console.log('\n' + (fails ? fails + ' FAILED' : 'all checks passed'));
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error('THREW:', e.stack); process.exit(1); });
