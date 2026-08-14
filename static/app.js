/* Gridiron Edge - client.
   State (league settings, draft progress, keepers, auction) persists in
   localStorage so a browser refresh mid-draft never loses the board. */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const STORE = 'gridiron.v1';
const state = {
  league: { scoring: 'half_ppr', teams: 12, rounds: 16, budget: 200,
            roster: { QB:1, RB:2, WR:3, TE:1, FLEX:1, SUPERFLEX:0, K:1, DST:1, BENCH:6 } },
  slot: 1, reversal: false,
  taken: [], mine: [],          // draft order, and which of those are yours
  keepers: [],                  // [{player_id, round}]
  sold: [],                     // [{player_id, price, mine}]
  tab: 'board', sub: 'coaching',
  sort: { key: 'vorp', dir: -1 },
  pos: 'ALL', auctionPos: 'ALL', poolPos: 'ALL',
  hideDrafted: true,
  theme: 'dark',
};

let BOARD = [];        // current valued board
let BY_ID = {};
let LAST_RECS = [];    // for keyboard drafting
let busy = 0;

/* The daily build writes plain JSON; every league-dependent number is computed
   here by engine.js. Same files locally and on GitHub Pages, so there is one
   code path rather than a local variant and a hosted variant. */
const PROJ = {};       // scoring key -> raw projection rows (cached per session)
let META = null;
let HISTORY = null;    // lazy: only fetched when a player card is first opened
let TEAMS = null;

async function loadJSON(path) {
  spin(true);
  try {
    // no-cache, NOT no-store. Both revalidate on every load, so a stale board is
    // still impossible — but no-store makes an HTTP cache hit structurally
    // impossible too, so ~1MB of JSON was re-downloaded on every single load,
    // including a mid-draft reload on venue wifi. no-cache turns the unchanged
    // case into a 304.
    const r = await fetch(path, { cache: 'no-cache' });
    if (!r.ok) throw new Error(`${path} — HTTP ${r.status}`);
    return await r.json();
  } finally { spin(false); }
}

/* A failed fetch used to either brick the page or, worse, leave the previous
   numbers on screen with no indication they were now wrong. Mid-draft on bad
   wifi that is the difference between "retry" and "trust a stale board". */
let unloading = false;
addEventListener('pagehide', () => { unloading = true; });
addEventListener('beforeunload', () => { unloading = true; });

function dataError(err, what) {
  // A fetch aborted because the page is navigating away is not a failure the
  // user needs to see. Crying wolf on a reload would teach them to ignore the
  // bar on the one occasion it matters.
  if (unloading) return;
  console.error(what, err);
  const bar = $('#dataError') || (() => {
    const el = document.createElement('div');
    el.id = 'dataError'; el.className = 'data-error';
    document.body.appendChild(el);
    return el;
  })();
  bar.innerHTML = `<strong>Could not load ${esc(what)}.</strong>
    ${esc(String(err && err.message || err))} —
    the numbers on screen may be out of date.
    <button id="dataRetry">Retry</button>`;
  bar.hidden = false;
  const btn = $('#dataRetry');
  if (btn) btn.onclick = () => { bar.hidden = true; location.reload(); };
}

async function projections(scoring) {
  if (!PROJ[scoring]) PROJ[scoring] = await loadJSON(`data/proj_${scoring}.json`);
  return PROJ[scoring];
}

/* The board: raw projections priced for YOUR league settings. */
async function buildBoard() {
  const raw = await projections(state.league.scoring);
  const lg = { ...state.league, auction_budget: state.league.budget };
  return Engine.computeValues(raw, lg);
}
function lg() { return { ...state.league, auction_budget: state.league.budget }; }

/* ------------------------------------------------------------ persist -- */
function save() {
  const { league, slot, reversal, taken, mine, keepers, sold, tab, theme,
          pos, hideDrafted, poolPos, auctionPos } = state;
  localStorage.setItem(STORE, JSON.stringify({ league, slot, reversal, taken, mine,
    keepers, sold, tab, theme, pos, hideDrafted, poolPos, auctionPos }));
}
function load() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE) || '{}');
    Object.assign(state, raw);
    if (raw.league) state.league = { ...state.league, ...raw.league,
      roster: { ...state.league.roster, ...(raw.league.roster || {}) } };
  } catch (e) { /* first run */ }
}

/* -------------------------------------------------------------- utils -- */
const fmt  = (n, d = 0) => (n === null || n === undefined || Number.isNaN(n)) ? '–' : Number(n).toFixed(d);
const money = n => (n === null || n === undefined) ? '–' : '$' + Math.max(1, Math.round(n));
const posBadge = p => `<span class="pos-badge pos-${p.position}">${p.pos_label || p.position}</span>`;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));

/* Typing "jamarr" must find Ja'Marr Chase, "ajbrown" must find A.J. Brown, and
   "amonra" must find Amon-Ra St. Brown. Matching the raw string means the
   apostrophes and periods have to be typed exactly, which is the last thing you
   want with 90 seconds on the clock. Mirrors norm_name() in data/sources.py. */
const foldName = s => String(s ?? '').normalize('NFKD')
  .replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');

/* Precomputed once per board build; searching 1127 players per keystroke should
   not also be normalising 1127 strings. */
function indexSearch(rows) {
  for (const p of rows) p._s = foldName(p.name) + ' ' + String(p.team || '').toLowerCase();
  return rows;
}
const matches = (p, q) => !q || (p._s || '').includes(q) ||
  p.name.toLowerCase().includes(q) || String(p.team || '').toLowerCase().includes(q);
const queryOf = el => {
  const raw = (el && el.value || '').trim();
  return raw ? (/^[a-z0-9]+$/i.test(raw) ? raw.toLowerCase() : foldName(raw)) : '';
};

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._t); t._t = setTimeout(() => { t.hidden = true; }, 2200);
}

function spin(on) {
  busy += on ? 1 : -1;
  if (busy < 0) busy = 0;
  let el = $('#spinner');
  if (busy > 0 && !el) {
    el = document.createElement('div');
    el.id = 'spinner'; el.className = 'spinner'; el.textContent = 'updating…';
    document.body.appendChild(el);
  } else if (busy === 0 && el) el.remove();
}

function qs(extra = {}) {
  const l = state.league;
  const p = new URLSearchParams({
    scoring: l.scoring, teams: l.teams, rounds: l.rounds, budget: l.budget, ...extra,
  });
  Object.entries(l.roster).forEach(([k, v]) => p.set('slot_' + k, v));
  return p.toString();
}
function leaguePayload(extra = {}) {
  const l = state.league;
  const body = { scoring: l.scoring, teams: l.teams, rounds: l.rounds, budget: l.budget, ...extra };
  Object.entries(l.roster).forEach(([k, v]) => { body['slot_' + k] = v; });
  return body;
}

const rosterSize = () => Object.values(state.league.roster).reduce((a, b) => a + (+b || 0), 0);

/* --------------------------------------------------------------- boot -- */
async function boot() {
  load();
  applyTheme();
  let meta;
  try {
    meta = META = await loadJSON('data/meta.json');
  } catch (e) {
    dataError(e, 'the site data');
    return;
  }

  const sel = $('#scoring');
  sel.innerHTML = Object.entries(meta.scoring_presets)
    .map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('');
  sel.value = state.league.scoring;
  $('#teams').value  = state.league.teams;
  $('#rounds').value = state.league.rounds;
  $('#budget').value = state.league.budget;
  $('#hideDrafted').checked = state.hideDrafted;

  $('#rosterGrid').innerHTML = ['QB','RB','WR','TE','FLEX','SUPERFLEX','K','DST','BENCH']
    .map(k => `<label>${k === 'SUPERFLEX' ? 'SFLEX' : k}<input type="number" min="0" max="10"
       data-slot="${k}" value="${state.league.roster[k] ?? 0}"
       title="${k === 'SUPERFLEX' ? 'Second QB-eligible slot. Set to 1 for superflex/2QB — this is what actually re-prices quarterbacks.' : k}"></label>`).join('');

  renderFreshness(meta);
  wire();
  buildSlotButtons();
  $$('#posFilter button').forEach(b => b.classList.toggle('active', b.dataset.pos === state.pos));
  showTab(state.tab || 'board');
  await refreshBoard();
}

function renderFreshness(meta) {
  const today = new Date().toISOString().slice(0, 10);
  const fresh = meta.last_fetch === today;
  const win = meta.adp_window ? `${meta.adp_window[0]} → ${meta.adp_window[1]}` : '';
  $('#freshness').innerHTML =
    `<div>Data <span class="${fresh ? 'fresh' : 'stale'}">${esc(meta.last_fetch || 'never')}</span>
       &middot; ${meta.players} players</div>
     <div>ADP from ${Number(meta.adp_drafts || 0).toLocaleString()} drafts ${esc(win)}</div>`;
}

function applyTheme() { document.documentElement.setAttribute('data-theme', state.theme); }

/* --------------------------------------------------------------- wire -- */
function wire() {
  $('#themeBtn').onclick = () => {
    state.theme = state.theme === 'dark' ? 'light' : 'dark'; applyTheme(); save();
  };
  $('#rosterBtn').onclick = () => { $('#rosterPanel').hidden = !$('#rosterPanel').hidden; };
  $('#helpBtn').onclick = showHelp;

  const LIMITS = { teams: [4, 20, 12], rounds: [1, 30, 16], budget: [10, 1000, 200] };
  ['scoring','teams','rounds','budget'].forEach(id => {
    $('#' + id).onchange = e => {
      if (id === 'scoring') {
        state.league.scoring = e.target.value;
      } else {
        // Clearing the field yields Number('') === 0, which used to price the
        // whole board at $1 and persist it. Clamp to the same bounds the server
        // model uses and write the corrected value back into the input.
        const [lo, hi, dflt] = LIMITS[id];
        const raw = Number(e.target.value);
        const val = Number.isFinite(raw) && raw > 0 ? Math.min(Math.max(Math.round(raw), lo), hi) : dflt;
        state.league[id] = val;
        e.target.value = val;
      }
      if (id === 'teams') buildSlotButtons();
      save(); refreshBoard();
    };
  });
  $('#rosterGrid').addEventListener('change', e => {
    if (!e.target.dataset.slot) return;
    const raw = Number(e.target.value);
    const val = Number.isFinite(raw) ? Math.min(Math.max(Math.round(raw), 0), 10) : 0;
    state.league.roster[e.target.dataset.slot] = val;
    e.target.value = val;
    save(); refreshBoard();
  });

  $$('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
  $$('#subtabs button').forEach(b => b.onclick = () => {
    state.sub = b.dataset.sub;
    $$('#subtabs button').forEach(x => x.classList.toggle('active', x === b));
    ['coaching','injuries','movers'].forEach(s => $('#sub-' + s).hidden = (s !== state.sub));
    if (state.sub === 'coaching') loadCoaching();
    if (state.sub === 'injuries') loadInjuries();
    if (state.sub === 'movers') loadMovers();
  });

  const posGroup = (sel, key, after) => $$(sel + ' button').forEach(b => b.onclick = () => {
    state[key] = b.dataset.pos;
    $$(sel + ' button').forEach(x => x.classList.toggle('active', x === b));
    save(); after();
  });
  posGroup('#posFilter', 'pos', () => { renderBoard(); syncExport(); });
  posGroup('#auctionPosFilter', 'auctionPos', renderAuction);
  posGroup('#poolPosFilter', 'poolPos', renderPool);

  // Trailing debounce: typing "jefferson" was 9 full 400-row renders (~450ms
   // each on a throttled phone). Now it is one, after you stop typing.
  let boardSearchTimer = null;
  $('#boardSearch').oninput = () => {
    clearTimeout(boardSearchTimer);
    boardSearchTimer = setTimeout(renderBoard, 120);
  };
  $('#hideRisk').onchange  = renderBoard;
  $('#hideDrafted').onchange = e => { state.hideDrafted = e.target.checked; save(); renderBoard(); };
  let poolSearchTimer = null;
  $('#poolSearch').oninput = () => {
    clearTimeout(poolSearchTimer);
    poolSearchTimer = setTimeout(renderPool, 120);
  };
  let auctionSearchTimer = null;
  $('#auctionSearch').oninput = () => {
    clearTimeout(auctionSearchTimer);
    auctionSearchTimer = setTimeout(renderAuction, 120);
  };

  $$('#boardTable thead th').forEach(th => th.onclick = () => {
    const k = th.dataset.sort; if (!k) return;
    const asc = ['name','adp','overall_rank','pos_rank','tier','bye'];
    state.sort = (state.sort.key === k) ? { key: k, dir: -state.sort.dir }
                                        : { key: k, dir: asc.includes(k) ? 1 : -1 };
    renderBoard();
  });

  $('#reversal').onchange = e => { state.reversal = e.target.checked; save(); refreshSnake(); };
  $('#undoPick').onclick = undoPick;
  $('#resetDraft').onclick = () => {
    if (!confirm('Clear all drafted players?')) return;
    state.taken = []; state.mine = []; save(); refreshSnake(); renderBoard();
  };
  $('#resetAuction').onclick = () => {
    if (!confirm('Clear all auction results?')) return;
    state.sold = []; save(); renderAuction();
  };

  $('#keeperSearch').oninput = keeperAutocomplete;
  $('#drawerClose').onclick = () => { $('#drawer').hidden = true; };
  $('#drawer').onclick = e => { if (e.target.id === 'drawer') $('#drawer').hidden = true; };

  document.addEventListener('keydown', onKey);
}

/* Draft day is a timed event. These make the common actions reachable without
   hunting for a button: focus search, and draft straight off the recommendation
   list by number. */
function onKey(e) {
  if (e.key === 'Escape') { $('#drawer').hidden = true; return; }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (e.key === '/' && !typing) {
    e.preventDefault();
    const box = state.tab === 'snake' ? $('#poolSearch')
              : state.tab === 'auction' ? $('#auctionSearch')
              : state.tab === 'keeper' ? $('#keeperSearch') : $('#boardSearch');
    if (box) { box.focus(); box.select(); }
    return;
  }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (state.tab === 'snake' && e.key >= '1' && e.key <= '6') {
    const p = LAST_RECS[Number(e.key) - 1];
    if (p) { e.preventDefault(); draftPlayer(p.player_id, true); }
    return;
  }
  if (state.tab === 'snake' && ['!','@','#','$','%','^'].includes(e.key)) {
    const p = LAST_RECS['!@#$%^'.indexOf(e.key)];
    if (p) { e.preventDefault(); draftPlayer(p.player_id, false); }
    return;
  }
  if (state.tab === 'snake' && e.key.toLowerCase() === 'u') { e.preventDefault(); undoPick(); }
}

function showTab(tab) {
  state.tab = tab; save();
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  ['board','snake','keeper','auction','myteam','intel'].forEach(t => $('#tab-' + t).hidden = (t !== tab));
  if (tab === 'board')   { renderBoard(); syncExport(); }
  if (tab === 'snake')   refreshSnake();
  if (tab === 'keeper')  refreshKeeper();
  if (tab === 'auction') renderAuction();
  if (tab === 'myteam')  renderMyTeam();
  if (tab === 'intel')   { if (state.sub === 'coaching') loadCoaching();
                           else if (state.sub === 'injuries') loadInjuries(); else loadMovers(); }
}

function buildSlotButtons() {
  const n = state.league.teams;
  if (state.slot > n) state.slot = n;
  $('#slotButtons').innerHTML = Array.from({ length: n }, (_, i) =>
    `<button data-slot="${i + 1}" class="${i + 1 === state.slot ? 'active' : ''}">${i + 1}</button>`).join('');
  $$('#slotButtons button').forEach(b => b.onclick = () => {
    state.slot = Number(b.dataset.slot);
    $$('#slotButtons button').forEach(x => x.classList.toggle('active', x === b));
    save(); refreshSnake();
  });
}

/* CSV is built in the browser: there is no server on GitHub Pages, and the
   board is already in memory anyway. */
function syncExport() {
  const a = $('#exportBtn');
  if (!a) return;
  a.onclick = e => {
    e.preventDefault();
    const rows = filteredBoard().slice(0, 300);
    const head = ['Rank','Player','Pos','Team','Tier','Proj','PPG','Games','VORP',
                  'Auction$','MaxBid','ADP','Edge','Bye','InjuryRisk','Availability','Notes'];
    const cell = v => {
      const t = v === null || v === undefined ? '' : String(v);
      return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
    };
    const body = rows.map(p => [
      p.overall_rank, p.name, p.pos_label || p.position, p.team, p.tier,
      (p.points ?? 0).toFixed(1), (p.ppg ?? 0).toFixed(2), (p.games ?? 0).toFixed(1),
      (p.vorp ?? 0).toFixed(1), Math.round(p.auction_value ?? 0), Math.round(p.max_bid ?? 0),
      p.adp ?? '', p.value_vs_adp ?? '', p.bye ?? '', p.injury_risk,
      Math.round((p.availability ?? 0) * 100) + '%', p.injury_note || '',
    ].map(cell).join(','));
    const csv = [head.join(','), ...body].join('\n');
    const label = (META && META.scoring_presets[state.league.scoring] || state.league.scoring)
      .replace(/\s+/g, '-');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `gridiron-edge-${label}-${state.league.teams}team.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
}

/* -------------------------------------------------------------- board -- */
async function refreshBoard() {
  try {
    BOARD = await buildBoard();
  } catch (e) {
    dataError(e, 'the projections');
    return;
  }
  indexSearch(BOARD);
  BY_ID = Object.fromEntries(BOARD.map(p => [p.player_id, p]));
  renderBoard();
  syncExport();
  if (state.tab === 'snake')   refreshSnake();
  if (state.tab === 'keeper')  refreshKeeper();
  if (state.tab === 'auction') renderAuction();
  if (state.tab === 'myteam')  renderMyTeam();
}

function filteredBoard() {
  const q = queryOf($('#boardSearch'));
  const hide = $('#hideRisk').checked;
  const taken = new Set(state.taken);
  let rows = BOARD.filter(p => state.pos === 'ALL' || p.position === state.pos);
  if (q) rows = rows.filter(p => matches(p, q));
  if (hide) rows = rows.filter(p => p.injury_risk !== 'high');
  if (state.hideDrafted) rows = rows.filter(p => !taken.has(p.player_id));
  const { key, dir } = state.sort;
  const rank = { low: 0, medium: 1, high: 2 };
  return rows.sort((a, b) => {
    let x = a[key], y = b[key];
    if (key === 'injury_risk') { x = rank[x] ?? 0; y = rank[y] ?? 0; }
    if (typeof x === 'string' || typeof y === 'string') return String(x).localeCompare(String(y)) * dir;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (x - y) * dir;
  });
}

function boardRow(p, taken, mineSet) {
  const e = p.value_vs_adp;
  const edge = e === null || e === undefined ? '<span class="pmeta">–</span>'
    : `<span class="edge ${e > 0 ? 'pos' : e < 0 ? 'neg' : ''}">${e > 0 ? '+' : ''}${e}</span>`;
  const bk = p.breakout || 0;
  const bkCls = bk >= 0.6 ? 'hi' : bk >= 0.35 ? 'mid' : '';
  const cls = [taken.has(p.player_id) ? 'is-drafted' : '', mineSet.has(p.player_id) ? 'is-mine' : '']
    .filter(Boolean).join(' ');
  return `<tr data-id="${p.player_id}" class="${cls}">
    <td class="pmeta c-rank">${p.overall_rank}</td>
    <td class="left"><div class="pname"><strong>${esc(p.name)}</strong>
      <span class="pmeta">${esc(p.team || '')}</span></div></td>
    <td>${posBadge(p)}</td>
    <td><span class="tier-chip">${p.tier ?? '–'}</span></td>
    <td><strong>${fmt(p.points)}</strong></td>
    <td class="c-mid">${fmt(p.ppg, 1)}</td>
    <td class="pmeta c-mid">${fmt(p.games, 1)}</td>
    <td>${fmt(p.vorp)}</td>
    <td class="money">${money(p.auction_value)}</td>
    <td class="pmeta c-mid">${p.adp ? fmt(p.adp, 1) : '–'}</td>
    <td>${edge}</td>
    <td class="pmeta c-low">${p.bye ?? '–'}</td>
    <td class="c-mid"><span class="risk ${p.injury_risk}">${p.injury_risk}</span></td>
    <td class="c-low"><span class="breakout-chip ${bkCls}">${Math.round(bk * 100)}</span></td>
  </tr>`;
}

function renderBoard() {
  $$('#boardTable thead th').forEach(th => th.classList.toggle('sorted', th.dataset.sort === state.sort.key));
  const all = filteredBoard();
  const rows = all.slice(0, 400);
  const taken = new Set(state.taken), mineSet = new Set(state.mine);

  // Tier separators only make sense within one position (tiers are per-position)
  // and only when the board is ordered by value. Sorted by ADP or name they
  // would be meaningless noise.
  const valueSorted = ['vorp','points','overall_rank','pos_rank','auction_value','tier']
    .includes(state.sort.key);
  const showTiers = state.pos !== 'ALL' && valueSorted;

  let html = '', lastTier = null;
  for (const p of rows) {
    if (showTiers && p.tier !== lastTier) {
      const left = all.filter(x => x.tier === p.tier && !taken.has(x.player_id)).length;
      const lastOne = left === 1;
      html += `<tr class="tier-break ${lastOne ? 'last-one' : ''}"><td colspan="14">
        <div class="tb"><span>Tier ${p.tier}</span>
          <span class="cnt">${lastOne ? 'last one — the drop after him is real'
                                      : `${left} players left at this grade`}</span>
        </div></td></tr>`;
      lastTier = p.tier;
    }
    html += boardRow(p, taken, mineSet);
  }
  $('#boardTable tbody').innerHTML = html ||
    '<tr><td colspan="14" class="empty">No players match.</td></tr>';

  const hidden = state.hideDrafted && state.taken.length
    ? ` · ${state.taken.length} drafted hidden` : '';
  $('#boardCount').textContent =
    `Showing ${rows.length} of ${all.length} players${hidden}. Click any row for the full breakdown.`;

  $$('#boardTable tbody tr[data-id]').forEach(tr => {
    tr.onclick = () => openPlayer(tr.dataset.id);
  });
  renderDraftStrip();
}

/* Mid-draft, the Board should know what the Snake tab knows. */
function renderDraftStrip() {
  const strip = $('#draftStrip');
  if (!state.taken.length) { strip.hidden = true; return; }
  strip.hidden = false;
  const mine = state.mine.map(id => BY_ID[id]).filter(Boolean);
  const pts = mine.reduce((a, p) => a + (p.points || 0), 0);
  strip.innerHTML =
    `<span><b>${state.taken.length}</b> players off the board</span>
     <span><b>${mine.length}</b> on your roster${mine.length ? ` · <b>${Math.round(pts)}</b> projected points` : ''}</span>
     <span class="sp"></span>
     <span class="pmeta">Draft on the Snake tab; this board stays in sync.</span>`;
}

/* -------------------------------------------------------------- snake -- */
async function refreshSnake() {
  const picks = Engine.pickNumbers(state.slot, state.league.teams, state.league.rounds, state.reversal);
  const nDone = state.taken.length;
  const myPick = picks.find(p => p > nDone) || null;
  const nextPick = myPick ? (picks.find(p => p > myPick) || null) : null;
  const takenSet = new Set(state.taken);
  const roster = state.mine.map(id => BY_ID[id]).filter(Boolean);
  const available = BOARD.filter(p => !takenSet.has(p.player_id));
  const r = {
    on_the_clock: myPick, next_pick: nextPick,
    picks_until: myPick ? (myPick - nDone - 1) : 0,
    recommendations: Engine.recommend(available, roster, myPick || (nDone + 1), nextPick, lg()),
    roster: roster, my_picks: picks,
  };

  $('#clock').innerHTML = r.on_the_clock
    ? `<div class="big">Pick ${r.on_the_clock}</div>
       <div class="sub">Round ${Math.ceil(r.on_the_clock / state.league.teams)} ·
         ${r.picks_until > 0 ? `${r.picks_until} pick${r.picks_until === 1 ? '' : 's'} away` : 'you are on the clock'}
         ${r.next_pick ? ` · next at ${r.next_pick}` : ''}</div>`
    : `<div class="big">Draft complete</div><div class="sub">${state.taken.length} players off the board</div>`;

  LAST_RECS = r.recommendations.slice(0, 6);
  $('#recList').innerHTML = LAST_RECS.map((p, i) => `
    <div class="rec">
      <div class="num">${i + 1}</div>
      <div class="who">
        <div class="top"><strong>${esc(p.name)}</strong> ${posBadge(p)}
          <span class="pmeta">${esc(p.team)} · tier ${p.tier ?? '–'} · bye ${p.bye ?? '–'}</span>
          ${p.injury_risk === 'high' ? '<span class="risk high">injury risk</span>' : ''}</div>
        <div class="why">${esc(p.reason)}</div>
      </div>
      <div class="acts" style="display:flex;align-items:center">
        <div class="nums">
          <div class="v">${fmt(p.vorp)}</div>
          <div class="s">VORP · ${money(p.auction_value)} · ${p.adp ? 'ADP ' + fmt(p.adp, 1) : 'no ADP'}</div>
          <div class="s">${Math.round((p.survives_to_next || 0) * 100)}% to last</div>
        </div>
        <button class="take" data-take="${p.player_id}" title="Draft to your roster (key ${i + 1})">Draft</button>
        <button class="gone" data-gone="${p.player_id}" title="Someone else took him (shift+${i + 1})">Gone</button>
      </div>
    </div>`).join('') || '<div class="empty">No players available.</div>';

  $$('[data-take]').forEach(b => b.onclick = () => draftPlayer(b.dataset.take, true));
  $$('[data-gone]').forEach(b => b.onclick = () => draftPlayer(b.dataset.gone, false));

  renderMyRoster(r.roster || []);
  renderScarcity();
  renderPool();
  loadPlan();
}

function renderMyRoster(roster) {
  if (!roster.length) {
    $('#myRoster').innerHTML =
      '<div class="roster-empty">No picks yet. Recommendations account for what you already have.</div>';
    return;
  }
  // Two starters at the same position sharing a bye week is a week you lose.
  const byes = {};
  roster.forEach(p => { if (p.bye) (byes[p.bye] = byes[p.bye] || []).push(p); });
  $('#myRoster').innerHTML = roster.map(p => {
    const clash = p.bye && byes[p.bye].length > 1 &&
      byes[p.bye].filter(q => q.position === p.position).length > 1;
    return `<div class="roster-row">${posBadge(p)}<span class="rl">${esc(p.name)}</span>
      ${clash ? `<span class="bye-warn" title="Another ${p.position} on your roster shares this bye">bye ${p.bye}</span>` : ''}
      <span class="pmeta">${fmt(p.points)}</span></div>`;
  }).join('');
}

/* How thin is each position right now? "3 left at this grade" is the number
   that decides whether to take a position now or wait a round. */
function renderScarcity() {
  const taken = new Set(state.taken);
  const avail = BOARD.filter(p => !taken.has(p.player_id));
  const html = ['QB','RB','WR','TE'].map(pos => {
    const arr = avail.filter(p => p.position === pos).sort((a, b) => b.vorp - a.vorp);
    if (!arr.length) return '';
    const best = arr[0];
    const sameTier = arr.filter(p => p.tier === best.tier).length;
    const cls = sameTier === 1 ? 'gone' : sameTier <= 3 ? 'thin' : '';
    return `<div class="scar ${cls}">
      <div class="hd">${posBadge({ position: pos, pos_label: pos })}
        <span class="v">tier ${best.tier ?? '–'}</span></div>
      <div class="s">${sameTier} left at this grade</div>
      <div class="s">best: ${esc(best.name.split(' ').slice(-1)[0])} · ${money(best.auction_value)}</div>
    </div>`;
  }).join('');
  $('#scarcity').innerHTML = html;
}

function renderPool() {
  const q = queryOf($('#poolSearch'));
  const taken = new Set(state.taken);
  let rows = BOARD.filter(p => !taken.has(p.player_id));
  if (state.poolPos !== 'ALL') rows = rows.filter(p => p.position === state.poolPos);
  if (q) rows = rows.filter(p => matches(p, q));
  $('#poolList').innerHTML = rows.slice(0, 160).map(p => `
    <div class="pool-row">
      <div><strong>${esc(p.name)}</strong> <span class="pmeta">${esc(p.team)} · ${fmt(p.points)} pts · ${money(p.auction_value)} · tier ${p.tier ?? '–'}</span></div>
      ${posBadge(p)}
      <div class="acts">
        <button class="mine" data-take="${p.player_id}">Mine</button>
        <button data-gone="${p.player_id}">Gone</button>
      </div>
    </div>`).join('') || '<div class="empty">Nobody left.</div>';
  $$('#poolList [data-take]').forEach(b => b.onclick = () => draftPlayer(b.dataset.take, true));
  $$('#poolList [data-gone]').forEach(b => b.onclick = () => draftPlayer(b.dataset.gone, false));
}

function draftPlayer(id, isMine) {
  if (state.taken.includes(id)) return;
  state.taken.push(id);
  if (isMine) state.mine.push(id);
  save();
  const p = BY_ID[id];
  toast(`${p ? p.name : 'Player'} ${isMine ? '→ your roster' : 'off the board'}`);
  refreshSnake();
  if (state.tab === 'myteam') renderMyTeam();
}

function undoPick() {
  const last = state.taken.pop();
  if (!last) return;
  state.mine = state.mine.filter(x => x !== last);
  save();
  const p = BY_ID[last];
  toast(`Undid ${p ? p.name : 'last pick'}`);
  refreshSnake();
}

async function loadPlan() {
  const r = { plan: Engine.planDraft(BOARD, state.slot, lg(),
                state.keepers.map(k => k.player_id), state.reversal) };
  const taken = new Set(state.taken);
  const done = state.taken.length;
  // Rounds already played become a log of what you actually took; rounds still
  // ahead stay a forecast. Showing a spent pick as an empty forecast ("Round 1
  // — ") is just confusing once the draft is under way.
  $('#planList').innerHTML = r.plan.map((row, i) => {
    if (row.pick <= done) {
      const got = BY_ID[state.mine[i]];
      return `<div class="plan-row past">
        <div class="hd"><span>Round ${row.round}</span><span>pick ${row.pick} · done</span></div>
        <div class="cands">${got
          ? `${posBadge(got)} <strong>${esc(got.name)}</strong> <span class="pmeta">${fmt(got.points)} pts</span>`
          : '<span class="pmeta">no pick recorded</span>'}</div>
      </div>`;
    }
    const cands = row.candidates.filter(c => {
      const hit = BOARD.find(p => p.name === c.name && p.position === c.position);
      return !hit || !taken.has(hit.player_id);
    }).slice(0, 4);
    return `<div class="plan-row">
      <div class="hd"><span>Round ${row.round}</span><span>pick ${row.pick}</span></div>
      <div class="cands">${cands.map(c =>
        `${esc(c.name)} <span class="pmeta">(${c.pos_label}, ${Math.round(c.prob * 100)}%)</span>`).join(' · ') || '–'}</div>
    </div>`;
  }).join('');
}

/* ------------------------------------------------------------- keeper -- */
function keeperAutocomplete() {
  const q = queryOf($('#keeperSearch'));
  const box = $('#keeperResults');
  if (q.length < 2) { box.innerHTML = ''; return; }
  const have = new Set(state.keepers.map(k => k.player_id));
  const hits = BOARD.filter(p => !have.has(p.player_id) && matches(p, q)).slice(0, 10);
  box.innerHTML = hits.map(p =>
    `<div data-add="${p.player_id}">${posBadge(p)} <strong>${esc(p.name)}</strong>
       <span class="pmeta">${esc(p.team)} · ${money(p.auction_value)} · ADP ${p.adp ? fmt(p.adp, 1) : '–'}</span></div>`).join('');
  $$('#keeperResults [data-add]').forEach(d => d.onclick = () => {
    const p = BY_ID[d.dataset.add];
    const rnd = p && p.adp ? Math.max(1, Math.ceil(p.adp / state.league.teams)) : 10;
    state.keepers.push({ player_id: d.dataset.add, round: rnd });
    $('#keeperSearch').value = ''; box.innerHTML = '';
    save(); refreshKeeper();
  });
}

async function refreshKeeper() {
  if (!state.keepers.length) {
    $('#keeperTable tbody').innerHTML =
      '<tr><td colspan="8" class="empty">Add the players you are allowed to keep, then set the round each one costs you.</td></tr>';
    $('#keeperHint').textContent = '';
    return;
  }
  const r = {
    keepers: Engine.keeperAnalysis(BOARD,
      state.keepers.map(k => ({ ...k, slot: state.slot })), lg()),
    max_keepers: state.league.keeper_max || 3,
  };

  $('#keeperTable tbody').innerHTML = r.keepers.map((k, i) => {
    const cls = k.surplus_dollars >= 5 ? 'keep' : k.surplus_dollars >= -3 ? 'marginal' : 'drop';
    const over = i >= r.max_keepers;
    return `<tr class="${over ? 'is-drafted' : ''}">
      <td class="left"><strong>${esc(k.name)}</strong> <span class="pmeta">${esc(k.team)}</span></td>
      <td>${posBadge(k)}</td>
      <td>Round <input class="rnd-input" type="number" min="1" max="${state.league.rounds}"
            value="${k.keeper_round}" data-rnd="${k.player_id}"></td>
      <td class="money">${money(k.auction_value)}</td>
      <td><span class="pmeta">${esc(k.baseline_name)}</span> ${money(k.baseline_value)}</td>
      <td class="money ${k.surplus_dollars >= 0 ? 'edge pos' : 'edge neg'}">
        ${k.surplus_dollars >= 0 ? '+' : '−'}$${Math.abs(Math.round(k.surplus_dollars))}</td>
      <td><span class="verdict ${cls}">${esc(k.verdict)}</span></td>
      <td><button class="xbtn" data-del="${k.player_id}">×</button></td>
    </tr>`;
  }).join('');

  $$('[data-rnd]').forEach(inp => inp.onchange = () => {
    const k = state.keepers.find(x => x.player_id === inp.dataset.rnd);
    if (k) { k.round = Number(inp.value); save(); refreshKeeper(); }
  });
  $$('[data-del]').forEach(b => b.onclick = () => {
    state.keepers = state.keepers.filter(x => x.player_id !== b.dataset.del);
    save(); refreshKeeper();
  });

  const n = state.keepers.length, max = r.max_keepers;
  $('#keeperHint').textContent = n > max
    ? `Your league allows ${max} keepers and you have listed ${n}. Ranked by surplus, keep the top ${max} — the greyed rows are the ones to release.`
    : 'Ranked by surplus value. Positive means the keeper beats what that pick would otherwise buy you.';
}

/* ------------------------------------------------------------ auction -- */
async function renderAuction() {
  const inf = Engine.auctionInflation(BOARD, state.sold, lg());
  const goneSet = new Set(state.sold.map(s2 => s2.player_id));
  // Sort the RAW rows and apply inflation in the row template instead of
  // spreading 1127 copies per keystroke. Inflation is a single positive scalar
  // (clamped to [0.4, 2.5] in engine.js), so multiplying by it cannot change the
  // ordering — sorting by auction_value is equivalent to sorting by the
  // inflated value.
  const r = { players: BOARD.filter(p => !goneSet.has(p.player_id))
                            .sort((a, b) => b.auction_value - a.auction_value) };

  // Your own budget is a different question from the room's, and it is the one
  // that constrains your next bid.
  const mySold = state.sold.filter(s => s.mine);
  const mySpent = mySold.reduce((a, s) => a + (+s.price || 0), 0);
  const myLeft = state.league.budget - mySpent;
  const slotsLeft = Math.max(rosterSize() - mySold.length, 0);
  const maxBid = Math.max(myLeft - Math.max(slotsLeft - 1, 0), slotsLeft > 0 ? 1 : 0);

  $('#auctionStats').innerHTML = `
    <div class="stat"><div class="k">Your budget</div><div class="v">$${myLeft}</div></div>
    <div class="stat" title="The most you can bid on one player and still fill every remaining roster spot at $1">
      <div class="k">Your max bid</div><div class="v">$${maxBid}</div></div>
    <div class="stat"><div class="k">Your spots</div><div class="v">${slotsLeft}</div></div>
    <div class="stat ${inf.inflation < 0.98 ? 'good' : inf.inflation > 1.02 ? 'bad' : ''}"
         title="Money left per dollar of value left. Above zero means the players left will be bid up; below zero means they should go cheap.">
      <div class="k">Room inflation</div><div class="v">${inf.inflation_pct >= 0 ? '+' : ''}${fmt(inf.inflation_pct, 1)}%</div></div>
    <div class="stat"><div class="k">Sold</div><div class="v">${state.sold.length}</div></div>`;
  $('#inflationNote').textContent = inf.note + '.';

  $('#myAuction').innerHTML = `<h4>Your roster (${mySold.length}/${rosterSize()}) — $${mySpent} spent</h4>
    <div class="row">${mySold.map((s, i) => {
      const p = BY_ID[s.player_id];
      return `<span class="my-chip">${p ? posBadge(p) : ''} ${esc(p ? p.name : s.player_id)}
        <strong>$${s.price}</strong><button data-unmine="${state.sold.indexOf(s)}">×</button></span>`;
    }).join('') || '<span class="pmeta">Tick “mine” when you win a player and your budget tracks here.</span>'}</div>`;
  $$('[data-unmine]').forEach(b => b.onclick = () => {
    state.sold.splice(Number(b.dataset.unmine), 1); save(); renderAuction();
  });

  const q = queryOf($('#auctionSearch'));
  let rows = r.players.filter(p => state.auctionPos === 'ALL' || p.position === state.auctionPos);
  if (q) rows = rows.filter(p => matches(p, q));

  $('#auctionTable tbody').innerHTML = rows.slice(0, 250).map(p => {
    const adjValue = p.auction_value * inf.inflation;
    const adjMax = p.max_bid * inf.inflation;
    const unaffordable = adjValue > maxBid;
    return `<tr data-id="${p.player_id}" class="${unaffordable ? 'is-drafted' : ''}"
      ${unaffordable ? 'title="Above your max bid — you cannot fill your roster if you win him at value"' : ''}>
      <td class="left"><strong>${esc(p.name)}</strong> <span class="pmeta">${esc(p.team)}</span></td>
      <td>${posBadge(p)}</td>
      <td><span class="tier-chip">${p.tier ?? '–'}</span></td>
      <td class="c-mid">${fmt(p.points)}</td>
      <td class="c-mid">${fmt(p.vorp)}</td>
      <td class="money"><strong>${money(adjValue)}</strong></td>
      <td class="money edge pos">${money(adjMax)}</td>
      <td><span class="mine-tick">
        <input class="sold-input" type="number" min="1" placeholder="$" data-sold="${p.player_id}">
        <label title="I won him"><input type="checkbox" data-mine="${p.player_id}"> mine</label>
      </span></td>
    </tr>`;
  }).join('') || '<tr><td colspan="8" class="empty">No players match.</td></tr>';

  const atbody = $('#auctionTable tbody');
  $$('[data-sold]', atbody).forEach(inp => {
    inp.onclick = e => e.stopPropagation();
    inp.onchange = () => {
      const price = Number(inp.value);
      if (!price || price < 1) return;
      const mineBox = $(`[data-mine="${CSS.escape(inp.dataset.sold)}"]`);
      state.sold.push({ player_id: inp.dataset.sold, price, mine: !!(mineBox && mineBox.checked) });
      save(); renderAuction();
    };
  });
  $$('[data-mine]', atbody).forEach(cb => {
    cb.onclick = e => e.stopPropagation();
    // Recording the price re-renders the table, which wiped the checkbox — so
    // ticking "mine" after typing the price silently did nothing and your budget
    // never moved. Now it retro-fits the sale that was just recorded.
    cb.onchange = e => {
      e.stopPropagation();
      const sale = state.sold.find(x => x.player_id === cb.dataset.mine);
      if (sale) { sale.mine = cb.checked; save(); renderAuction(); }
    };
  });
  $$('#auctionTable tbody tr[data-id]').forEach(tr => {
    tr.onclick = () => openPlayer(tr.dataset.id);
  });

  $('#soldList').innerHTML = state.sold.map((s, i) => {
    const p = BY_ID[s.player_id];
    return `<span class="sold-chip">${esc(p ? p.name : s.player_id)} <strong>$${s.price}</strong>
      ${s.mine ? '<span class="edge pos">you</span>' : ''}
      <button data-unsold="${i}">×</button></span>`;
  }).join('') || '<span class="pmeta">Record what players actually sell for and every value on this page re-prices itself.</span>';
  $$('[data-unsold]').forEach(b => b.onclick = () => {
    state.sold.splice(Number(b.dataset.unsold), 1); save(); renderAuction();
  });
}

/* ------------------------------------------------------------ my team -- */
/* Your roster can come from three places depending on how you draft, so this
   reads all of them rather than making you pick a mode. */
function myRoster() {
  const ids = new Set([
    ...state.mine,
    ...state.sold.filter(s2 => s2.mine).map(s2 => s2.player_id),
    ...state.keepers.map(k => k.player_id),
  ]);
  return [...ids].map(id => BY_ID[id]).filter(Boolean);
}

function renderMyTeam() {
  const roster = myRoster();
  $('#teamEmpty').hidden = roster.length > 0;
  $('#teamBody').hidden = roster.length === 0;
  if (!roster.length) return;

  const a = Engine.analyseTeam(BOARD, roster, lg());
  const band = a.overall >= 65 ? 'good' : a.overall >= 42 ? 'mid' : 'bad';
  const sign = n => (n >= 0 ? '+' : '') + Math.round(n);

  $('#teamScore').className = `score-card ${band}`;
  $('#teamScore').innerHTML = `
    <div class="big">${Math.round(a.overall)}</div>
    <div class="gr">${a.grade}</div>
    <div class="sub">${Math.round(a.yourPoints)} projected starter points<br>
      vs ${Math.round(a.avgPoints)} for an average team<br>
      <strong>${sign(a.surplus)}</strong> (${(a.totalPct * 100).toFixed(1)}%)</div>`;

  // The verdict is the part worth reading: what is good, what is not, what next.
  const rows = [];
  if (a.strongest) {
    rows.push(`<div class="verdict-row"><span class="tag strong">strongest</span>
      <span><b>${a.strongest.pos}</b> — ${(a.strongest.pct * 100).toFixed(0)}% above an
      average team's, worth <b>${sign(a.strongest.surplus)}</b> points.
      ${esc(a.strongest.players.map(p => p.name).join(', '))}</span></div>`);
  }
  if (a.weakest && a.weakest !== a.strongest) {
    // Your weakest position is not necessarily a BAD one — a strong team can be
    // above average everywhere. Say which of those two situations it is rather
    // than asserting a deficit that does not exist.
    const w = a.weakest, wp = (w.pct * 100).toFixed(0);
    const body = w.pct < 0
      ? `${Math.abs(wp)}% below an average team's, costing you
         <b>${Math.round(Math.abs(w.surplus))}</b> points`
      : `your thinnest spot, though still <b>+${wp}%</b> on an average team's`;
    rows.push(`<div class="verdict-row"><span class="tag weak">${w.pct < 0 ? 'needs work' : 'thinnest'}</span>
      <span><b>${w.pos}</b> — ${body}.
      ${esc(w.players.map(p => p.name).join(', ')) || 'nobody rostered'}</span></div>`);
  }
  if (a.gaps.length) {
    rows.push(`<div class="verdict-row"><span class="tag gap">unfilled</span>
      <span>${a.gaps.map(g => `<b>${g.unfilled}&times; ${g.pos}</b>`).join(', ')}
      still to fill. Empty slots are scored at replacement level, not zero.</span></div>`);
  }
  // What to do next: the position where the board can help you most.
  const taken = new Set([...state.taken, ...state.sold.map(s2 => s2.player_id),
                         ...roster.map(p => p.player_id)]);
  const target = a.gaps.length ? a.gaps[0].pos : (a.weakest ? a.weakest.pos : null);
  if (target) {
    const best = BOARD.filter(p => p.position === target && !taken.has(p.player_id))
                      .sort((x, y) => y.vorp - x.vorp)[0];
    if (best) {
      rows.push(`<div class="verdict-row"><span class="tag next">draft next</span>
        <span>Best available <b>${target}</b> is <b>${esc(best.name)}</b>
        (${fmt(best.points)} pts, ${money(best.auction_value)}${best.adp ? ', ADP ' + fmt(best.adp, 1) : ''}).</span></div>`);
    }
  }
  $('#teamVerdict').innerHTML = rows.join('') ||
    '<div class="verdict-row">Draft a few more players and this will have something to say.</div>';

  $('#gradeGrid').innerHTML = a.positions.map(p => {
    const cls = p.score >= 75 ? 'a' : p.score >= 55 ? 'b' : p.score >= 38 ? 'c' : 'd';
    return `<div class="grade-cell ${cls} ${p.filled ? '' : 'empty'}">
      <div class="hd"><span class="pos">${p.pos}${p.slots > 1 ? ' &times;' + p.slots : ''}</span>
        <span class="g">${p.grade}</span></div>
      <div class="pts">${Math.round(p.yourPoints)} vs ${Math.round(p.avgPoints)}
        &middot; ${sign(p.surplus)}</div>
      <span class="bar"><i style="width:${Math.round(p.score)}%"></i></span>
    </div>`;
  }).join('');

  // Starting lineup, each slot against its own benchmark.
  const bands = Engine.benchmarkBands(BOARD, lg());
  const lines = [];
  for (const p of a.positions) {
    const band = bands[p.pos] || [];
    for (let i = 0; i < p.slots; i++) {
      const pl = p.players[i];
      const bm = band[i] || 0;
      if (pl) {
        const d = pl.points - bm;
        lines.push(`<div class="lineup-row">
          <span class="slot">${p.pos}${p.slots > 1 ? (i + 1) : ''}</span>
          <span class="nm">${esc(pl.name)} <span class="pmeta">${esc(pl.team)}</span></span>
          <span class="vs ${d >= 0 ? 'up' : 'down'}">${fmt(pl.points)} &middot; ${sign(d)}</span>
        </div>`);
      } else {
        lines.push(`<div class="lineup-row vacant">
          <span class="slot">${p.pos}${p.slots > 1 ? (i + 1) : ''}</span>
          <span class="nm">not drafted</span>
          <span class="vs">avg ${fmt(bm)}</span>
        </div>`);
      }
    }
  }
  $('#teamLineup').innerHTML = lines.join('');

  const risks = [];
  if (a.riskShare > 0.18) {
    risks.push(`<div class="risk-item bad"><b>${Math.round(a.riskShare * 100)}% of your starting
      production</b> comes from players flagged high injury risk:
      ${esc(a.riskyStarters.join(', '))}.</div>`);
  } else if (a.riskyStarters.length) {
    risks.push(`<div class="risk-item warn">One high-risk starter:
      <b>${esc(a.riskyStarters.join(', '))}</b> — ${Math.round(a.riskShare * 100)}% of your
      starting points.</div>`);
  } else {
    risks.push('<div class="risk-item ok">No starter is flagged high injury risk.</div>');
  }
  a.byeTrouble.forEach(b => {
    risks.push(`<div class="risk-item warn"><b>Week ${b.week}:</b> ${b.count} starters on bye
      (${esc(b.names.join(', '))}). You will be starting replacements that week.</div>`);
  });
  const thin = a.depth.filter(d => d.dropPct > 0.55).slice(0, 2);
  thin.forEach(d => {
    risks.push(`<div class="risk-item warn">Thin behind <b>${esc(d.starter)}</b> at ${d.pos}:
      ${d.backup ? `next man is ${esc(d.backup)}, a ${Math.round(d.dropPct * 100)}% drop`
                 : 'you have no other ' + d.pos}.</div>`);
  });
  if (a.bench.length) {
    risks.push(`<div class="risk-item"><b>Bench:</b>
      ${esc(a.bench.slice(0, 6).map(p => p.name).join(', '))}${a.bench.length > 6 ? ` +${a.bench.length - 6} more` : ''}.</div>`);
  }
  $('#teamRisks').innerHTML = risks.join('');
}

/* -------------------------------------------------------------- intel -- */
async function loadCoaching() {
  let r;
  try { r = TEAMS || (TEAMS = await loadJSON('data/teams.json')); }
  catch (e) { dataError(e, 'team and coaching data'); return; }
  $('#coachGrid').innerHTML = r.teams.map(t => {
    const changed = t.hc_changed || t.oc_changed;
    const prof = t.caller_profile;
    return `<div class="coach-card ${changed ? 'changed' : ''}">
      <div class="hd"><span class="tm">${esc(t.team)}</span>
        ${t.hc_changed ? '<span class="badge">new head coach</span>'
          : t.oc_changed ? '<span class="badge">new coordinator</span>' : ''}</div>
      <div class="staff">
        <b>HC</b> ${esc(t.hc)}${t.hc_changed ? ` <span class="pmeta">(was ${esc(t.prev_hc)})</span>` : ''}<br>
        <b>OC</b> ${esc(t.oc)}${t.oc_changed ? ` <span class="pmeta">(was ${esc(t.prev_oc)})</span>` : ''}<br>
        <b>Calls plays</b> ${esc(t.caller)} <span class="pmeta">· ${esc(t.tree)}</span>
      </div>
      <div class="ident">${esc(t.identity || '')}</div>
      <div class="metrics">
        <div class="metric"><div class="k">Plays/g</div><div class="v">${fmt(t.plays_pg, 1)}</div></div>
        <div class="metric"><div class="k">Pass rate</div><div class="v">${fmt(t.pass_rate * 100, 1)}%</div></div>
        <div class="metric"><div class="k">Pts/g</div><div class="v">${fmt(t.exp_ppg, 1)}</div></div>
        <div class="metric"><div class="k">Bye</div><div class="v">${t.bye ?? '–'}</div></div>
        <div class="metric"><div class="k">Vacated tgt</div><div class="v">${fmt(t.vacated_target_pct * 100, 0)}%</div></div>
        <div class="metric"><div class="k">Vacated car</div><div class="v">${fmt(t.vacated_carry_pct * 100, 0)}%</div></div>
      </div>
      ${prof ? `<div class="note">${esc(t.caller)} career: ${fmt(prof.plays_per_game, 1)} plays/g,
         ${fmt(prof.pass_rate * 100, 1)}% pass, ${fmt(prof.points_per_game, 1)} pts/g
         across ${prof.seasons} season(s) as a head coach.</div>` : ''}
      <div class="note">${esc(t.note)}</div>
    </div>`;
  }).join('');
}

async function loadInjuries() {
  const flagged = BOARD.filter(p => p.injury_status || p.injury_risk === 'high'
                                 || p.injury_risk === 'medium')
                       .sort((a, b) => b.vorp - a.vorp).slice(0, 250);
  $('#injuryTable tbody').innerHTML = flagged.map(p => `
    <tr data-id="${p.player_id}">
      <td class="left"><strong>${esc(p.name)}</strong> <span class="pmeta">${esc(p.team)}</span></td>
      <td>${posBadge(p)}</td>
      <td>${p.injury_status ? `<span class="risk high">${esc(p.injury_status)}</span>` : '<span class="pmeta">–</span>'}</td>
      <td>${fmt(p.games, 1)}</td>
      <td>${fmt((p.availability || 0) * 100, 0)}%</td>
      <td><span class="risk ${p.injury_risk}">${p.injury_risk}</span></td>
      <td class="left pmeta">${esc(p.injury_note || '')}</td>
    </tr>`).join('') || '<tr><td colspan="7" class="empty">Nothing flagged.</td></tr>';
  $$('#injuryTable tbody tr[data-id]').forEach(tr => {
    tr.onclick = () => openPlayer(tr.dataset.id);
  });
}

async function loadMovers() {
  let all;
  try { all = await loadJSON('data/movers.json'); }
  catch (e) { dataError(e, 'ADP movers'); return; }
  const want = { ppr: 'ppr', half_ppr: 'half-ppr', standard: 'standard',
                 superflex: 'ppr', te_premium: 'ppr' }[state.league.scoring] || 'ppr';
  const r = (all.formats || []).find(f => f.fmt === want)
            || { movers: [], note: 'No ADP history yet.' };
  if (!r.movers.length) {
    $('#moversList').innerHTML = `<div class="empty">${esc(r.note || 'No movement yet.')}
      Snapshots accumulate once the daily refresh has run more than once.</div>`;
    return;
  }
  $('#moversList').innerHTML = r.movers.map(m => `
    <div class="mover">
      <div><strong>${esc(m.name)}</strong> <span class="pmeta">${esc(m.position)} · ${esc(m.team)}</span><br>
        <span class="pmeta">ADP ${fmt(m.prev, 1)} → ${fmt(m.adp, 1)}</span></div>
      <div class="d ${m.delta > 0 ? 'up' : 'down'}">${m.delta > 0 ? '▲' : '▼'} ${fmt(Math.abs(m.delta), 1)}</div>
    </div>`).join('');
}

/* --------------------------------------------------------------- help -- */
function showHelp() {
  $('#drawerBody').innerHTML = `
    <div class="dh"><h2>Reading the board</h2></div>
    <div class="dsub">What each column means and which ones should drive a pick.</div>
    <div class="dsec"><h4>VORP — the one that matters</h4>
      <p>Value Over Replacement: projected points minus what the best freely available
         player at that position scores. 300 points from a quarterback is worth far less
         than 300 from a tight end, because the twelfth-best quarterback also scores a lot
         and the twelfth-best tight end does not. Rank by this, not by Proj.</p></div>
    <div class="dsec"><h4>Tier</h4>
      <p>Players close enough in value to be interchangeable. The gap <em>between</em>
         tiers is where drafts are won: if four players share a tier you can wait a round,
         and if you are looking at the last one, waiting costs you a whole grade. Filter to
         a single position to see the tier breaks drawn on the board.</p></div>
    <div class="dsec"><h4>Edge</h4>
      <p>Market rank minus model rank. <span class="edge pos">+24</span> means the room
         lets him fall 24 spots past where the model says he belongs — that is the value.
         <span class="edge neg">−18</span> means he is going earlier than justified.</p></div>
    <div class="dsec"><h4>$ and Max bid</h4>
      <p>What a player is worth in your league's auction budget, and the most you can pay
         and still profit. Both re-price live on the Auction tab as the room over- or
         under-spends.</p></div>
    <div class="dsec"><h4>G, Risk and Break</h4>
      <p><strong>G</strong> is projected games played — availability is half of fantasy
         scoring and usually ignored. <strong>Risk</strong> comes from five seasons of
         injury reports plus the live designation. <strong>Break</strong> (0–100) flags
         young players with an ascending role on a team with vacated volume.</p></div>
    <div class="dsec"><h4>Keyboard</h4>
      <p><kbd>/</kbd> focus search · on the Snake tab <kbd>1</kbd>–<kbd>6</kbd> drafts that
         recommendation to you, <kbd>shift</kbd>+number marks him gone to someone else,
         and <kbd>u</kbd> undoes the last pick.</p></div>`;
  $('#drawer').hidden = false;
}

/* ------------------------------------------------------------- drawer -- */
async function openPlayer(id) {
  if (!HISTORY) {
    try { HISTORY = await loadJSON('data/history.json'); }
    catch (e) { HISTORY = {}; dataError(e, 'player history'); }
  }
  const p = BY_ID[id];
  if (!p) return;
  const r = HISTORY[id] || { history: [], injuries: [] };
  const lo = p.floor, hi = p.ceiling, mid = p.points;
  const span = Math.max(hi - lo, 1);
  const drafted = state.taken.includes(id);

  $('#drawerBody').innerHTML = `
    <div class="dh"><h2>${esc(p.name)}</h2> ${posBadge(p)}</div>
    <div class="dsub">${esc(p.team)} · age ${fmt(p.age, 0)} · ${p.years_exp ?? '?'} yrs exp ·
      depth ${p.depth ?? '–'} · bye ${p.bye ?? '–'} · overall #${p.overall_rank}</div>

    <div class="dstats">
      <div class="dstat"><div class="k">Projected</div><div class="v">${fmt(p.points)}</div></div>
      <div class="dstat"><div class="k">Per game</div><div class="v">${fmt(p.ppg, 1)}</div></div>
      <div class="dstat"><div class="k">Games</div><div class="v">${fmt(p.games, 1)}</div></div>
      <div class="dstat"><div class="k">VORP</div><div class="v">${fmt(p.vorp)}</div></div>
      <div class="dstat"><div class="k">Auction</div><div class="v">${money(p.auction_value)}</div></div>
      <div class="dstat"><div class="k">Max bid</div><div class="v">${money(p.max_bid)}</div></div>
      <div class="dstat"><div class="k">ADP</div><div class="v">${p.adp ? fmt(p.adp, 1) : '–'}</div></div>
      <div class="dstat"><div class="k">Tier</div><div class="v">${p.tier ?? '–'}</div></div>
    </div>

    <div class="dsec">
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="ghost" id="dTake" ${drafted ? 'disabled' : ''}>${drafted ? 'Already off the board' : 'Draft to my roster'}</button>
        <button class="ghost" id="dGone" ${drafted ? 'disabled' : ''}>Mark as gone</button>
      </div>
    </div>

    <div class="dsec">
      <h4>Range of outcomes</h4>
      <div class="rangebar"><i style="left:0;right:0"></i>
        <b style="left:${((mid - lo) / span) * 100}%"></b></div>
      <div class="rangelbl"><span>floor ${fmt(lo)}</span>
        <span>proj ${fmt(mid)}</span><span>ceiling ${fmt(hi)}</span></div>
      <p style="margin-top:8px">Model confidence ${fmt((p.confidence || 0) * 100, 0)}%.
        ${p.confidence < 0.5 ? 'Wide range: limited evidence for this role.' : ''}</p>
    </div>

    <div class="dsec">
      <h4>2026 projection detail</h4>
      <table><tbody>
        ${p.position === 'QB' ? `
          <tr><td>Pass attempts</td><td>${fmt(p.pass_att)}</td></tr>
          <tr><td>Pass yards / TD / INT</td><td>${fmt(p.pass_yd)} · ${fmt(p.pass_td, 1)} · ${fmt(p.ints, 1)}</td></tr>` : ''}
        ${p.carries > 0.5 ? `
          <tr><td>Carries</td><td>${fmt(p.carries)}</td></tr>
          <tr><td>Rush yards / TD</td><td>${fmt(p.rush_yd)} · ${fmt(p.rush_td, 1)}</td></tr>
          <tr><td>Share of team carries</td><td>${fmt((p.car_share || 0) * 100, 1)}%</td></tr>` : ''}
        ${p.targets > 0.5 ? `
          <tr><td>Targets / receptions</td><td>${fmt(p.targets)} · ${fmt(p.receptions)}</td></tr>
          <tr><td>Rec yards / TD</td><td>${fmt(p.rec_yd)} · ${fmt(p.rec_td, 1)}</td></tr>
          <tr><td>Share of team targets</td><td>${fmt((p.tgt_share || 0) * 100, 1)}%</td></tr>` : ''}
        <tr><td>Replacement level at ${p.position}</td><td>${fmt(p.replacement)}</td></tr>
      </tbody></table>
    </div>

    <div class="dsec"><h4>Context — not in the projection</h4>
      <p>Strength of schedule <strong>${fmt((p.sos || 1) * 100 - 100, 1)}%</strong> vs average.
         Shown because it is worth knowing, but deliberately not applied: across three
         backtested seasons it changed rank accuracy by 0.0000, so folding it in would add
         a number without adding information.</p></div>

    <div class="dsec"><h4>Availability</h4>
      <p>${fmt((p.availability || 0) * 100, 0)}% of the season projected ·
         risk <strong>${p.injury_risk}</strong>. ${esc(p.injury_note || '')}</p></div>

    ${p.notes ? `<div class="dsec"><h4>Model notes</h4><p>${esc(p.notes)}</p></div>` : ''}
    ${p.coach_note ? `<div class="dsec"><h4>Coaching context</h4><p>${esc(p.coach_note)}</p></div>` : ''}

    ${r.history && r.history.length ? `<div class="dsec"><h4>Production history</h4>
      <table><thead><tr><th>Season</th><th>Tm</th><th>G</th><th>Car</th><th>RuYd</th>
        <th>Tgt</th><th>Rec</th><th>ReYd</th><th>TD</th></tr></thead><tbody>
        ${r.history.map(h => `<tr><td>${h.season}</td><td>${esc(h.team)}</td><td>${h.games}</td>
          <td>${fmt(h.carries)}</td><td>${fmt(h.rushing_yards)}</td><td>${fmt(h.targets)}</td>
          <td>${fmt(h.receptions)}</td><td>${fmt(h.receiving_yards)}</td>
          <td>${fmt((h.rushing_tds || 0) + (h.receiving_tds || 0) + (h.passing_tds || 0))}</td></tr>`).join('')}
      </tbody></table></div>` : ''}

    ${r.injuries && r.injuries.length ? `<div class="dsec"><h4>Injury report history</h4>
      <table><thead><tr><th>Season</th><th>Wks out</th><th>DNP</th><th>Q</th><th>Body parts</th></tr></thead><tbody>
        ${r.injuries.map(h => `<tr><td>${h.season}</td><td>${h.weeks_out}</td><td>${h.weeks_dnp}</td>
          <td>${h.weeks_questionable}</td><td style="text-align:left">${esc(h.body_parts || '–')}</td></tr>`).join('')}
      </tbody></table></div>` : ''}
  `;
  const take = $('#dTake'), gone = $('#dGone');
  if (take) take.onclick = () => { draftPlayer(id, true); $('#drawer').hidden = true; renderBoard(); };
  if (gone) gone.onclick = () => { draftPlayer(id, false); $('#drawer').hidden = true; renderBoard(); };
  $('#drawer').hidden = false;
}

/* On an encrypted build the gate resolves once the passphrase is accepted;
   on a plain build __gateReady is undefined and this runs immediately. */
(window.__gateReady || Promise.resolve()).then(boot);
