/* Hosted (GitHub Pages) version of the Sleeper report UI. Everything runs in the browser:
   Sleeper is fetched directly (its API allows cross-origin reads), the player database is cached
   in IndexedDB for 24h, config/reports live in localStorage, and the optional Claude step calls
   the Anthropic API with a key that is stored only in this browser. Nothing is sent anywhere else. */
'use strict';

const SLEEPER = 'https://api.sleeper.app/v1';
const SCHEDULE = 'https://api.sleeper.app/schedule/nfl';
const ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages';
const MODEL = 'claude-sonnet-4-6', MAX_TOKENS = 4000;
const CACHE_MAX_AGE_MS = 24 * 3600 * 1000;
const RETRY_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504, 529]);
const LS = { config: 'sr.config', apiKey: 'sr.apiKey', reports: 'sr.reports', week: 'sr.week' };

const SYSTEM_PROMPT = `You are advising on a 12-team full-PPR Sleeper league. The manager's
stated preference is consistent weekly floor over boom-bust ceiling —
weight that heavily and say so explicitly when it changes a call.
Do not recommend a player you cannot name a concrete reason for.

Produce exactly three sections:

START/SIT — the optimal lineup for the given roster slots, then a short
list of the genuinely close calls with the reasoning for each. Do not
re-justify obvious starts.

WAIVERS — ranked pickups with a FAAB bid as a percentage of the
manager's REMAINING budget, and the specific drop candidate for each.
If nothing is worth a bid this week, say that plainly instead of
manufacturing a recommendation.

WATCH — players to monitor but not claim yet, with the trigger that
would change that (snap share, a starter's injury designation, a bye
coming up).

You do not have projections. Reason from role, snap share, target
volume, matchup, and injury designations. Never invent a stat line or
a projected point total. If you're uncertain, say so.`;

// ---------------------------------------------------------------- HTTP with retry/backoff
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function getJSON(url, { retries = 4, timeoutMs = 60000 } = {}) {
  let last;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), timeoutMs);
      const r = await fetch(url, { signal: ctl.signal }).finally(() => clearTimeout(t));
      if (r.ok) return r.json();
      last = new Error(`GET ${url} -> HTTP ${r.status}`);
      if (!RETRY_STATUSES.has(r.status)) throw last;
    } catch (e) {
      if (e.name !== 'AbortError' && e.name !== 'TypeError' && !RETRY_STATUSES.has(Number((/HTTP (\d+)/.exec(e.message) || [])[1]))) throw e;
      last = e;
    }
    if (attempt === retries) break;
    await sleep(Math.min(30000, 1000 * 2 ** attempt) + Math.random() * 500);
  }
  throw new Error(`giving up on ${url}: ${last && last.message}`);
}
const sleeper = {
  state: () => getJSON(`${SLEEPER}/state/nfl`),
  user: u => getJSON(`${SLEEPER}/user/${encodeURIComponent(u)}`),
  userLeagues: (uid, season) => getJSON(`${SLEEPER}/user/${uid}/leagues/nfl/${season}`),
  league: id => getJSON(`${SLEEPER}/league/${id}`),
  rosters: id => getJSON(`${SLEEPER}/league/${id}/rosters`),
  users: id => getJSON(`${SLEEPER}/league/${id}/users`),
  matchups: (id, w) => getJSON(`${SLEEPER}/league/${id}/matchups/${w}`),
  transactions: (id, w) => (w < 1 ? Promise.resolve([]) : getJSON(`${SLEEPER}/league/${id}/transactions/${w}`)),
  trending: (kind, hours, limit) => getJSON(`${SLEEPER}/players/nfl/trending/${kind}?lookback_hours=${hours}&limit=${limit}`),
  allPlayers: () => getJSON(`${SLEEPER}/players/nfl`, { retries: 2, timeoutMs: 180000 }),
  schedule: season => getJSON(`${SCHEDULE}/regular/${season}`),
};

// ---------------------------------------------------------------- IndexedDB key/value cache
function idb() {
  return new Promise((res, rej) => {
    const req = indexedDB.open('sleeper-report', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('kv');
    req.onsuccess = () => res(req.result); req.onerror = () => rej(req.error);
  });
}
async function idbGet(key) {
  try { const db = await idb(); return await new Promise((res, rej) => { const r = db.transaction('kv').objectStore('kv').get(key); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); }); }
  catch { return undefined; }
}
async function idbSet(key, value) {
  try { const db = await idb(); await new Promise((res, rej) => { const tx = db.transaction('kv', 'readwrite'); tx.objectStore('kv').put(value, key); tx.oncomplete = res; tx.onerror = () => rej(tx.error); }); }
  catch (e) { console.warn('IndexedDB write failed', e); }
}
async function idbDel(key) {
  try { const db = await idb(); await new Promise((res, rej) => { const tx = db.transaction('kv', 'readwrite'); tx.objectStore('kv').delete(key); tx.oncomplete = res; tx.onerror = () => rej(tx.error); }); } catch {}
}

// ---------------------------------------------------------------- local state
const store = {
  get(k) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : null; } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { toast('Could not save: ' + e.message); } },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};
const config = () => store.get(LS.config);
const apiKey = () => { try { return localStorage.getItem(LS.apiKey) || ''; } catch { return ''; } };
const reports = () => store.get(LS.reports) || {};
function saveReport(week, markdown) { const r = reports(); r[week] = { markdown, modified: Date.now() / 1000 }; store.set(LS.reports, r); }

// ---------------------------------------------------------------- pipeline (mirrors sleeper_report/pipeline.py)
async function loadPlayers(force, log) {
  const cached = await idbGet('players');
  if (!force && cached && Date.now() - cached.ts < CACHE_MAX_AGE_MS) return cached.players;
  log(`Fetching player database from Sleeper (~10MB, ${force ? 'forced' : 'cache missing or older than 24h'})...`);
  const raw = await sleeper.allPlayers();
  const players = {};
  for (const [pid, rec] of Object.entries(raw)) if (rec && typeof rec === 'object') players[pid] = SleeperReport.trimPlayer(pid, rec);
  await idbSet('players', { ts: Date.now(), players });
  return players;
}
async function playersCacheStatus() {
  const c = await idbGet('players');
  if (!c) return { exists: false, age_hours: null, stale: true };
  const age = (Date.now() - c.ts) / 3600000;
  return { exists: true, age_hours: Math.round(age * 10) / 10, stale: age >= 24 };
}
async function loadSchedule(season, force) {
  const key = `schedule-${season}`;
  const cached = await idbGet(key);
  if (!force && cached && Date.now() - cached.ts < CACHE_MAX_AGE_MS) return cached.games;
  try {
    const games = await sleeper.schedule(season);
    if (!Array.isArray(games) || !games.length) return null;
    await idbSet(key, { ts: Date.now(), games });
    return games;
  } catch (e) { console.warn('schedule unavailable', e); return cached ? cached.games : null; }
}
async function fetchWeek(cfg, week, refresh, log) {
  const notes = [];
  const state = await sleeper.state();
  const season = cfg.season;
  if (week === null) {
    week = Number(state.week || state.leg || 1);
    if (String(state.season) !== season) notes.push(`config season is ${season} but Sleeper's current season is ${state.season}; pick a week explicitly for past seasons.`);
  }
  log(`Season ${season}, week ${week}`);
  log('Loading player database' + (refresh ? ' (forced refresh)' : ''));
  const players = await loadPlayers(refresh, log);
  log(`Player database: ${Object.keys(players).length.toLocaleString('en-US')} players`);
  log('Fetching league, rosters, users, matchups');
  const [league, rosters, users, matchups] = await Promise.all([
    sleeper.league(cfg.league_id), sleeper.rosters(cfg.league_id), sleeper.users(cfg.league_id), sleeper.matchups(cfg.league_id, week)]);
  if (!league) throw new Error(`league ${cfg.league_id} not found`);
  log('Fetching transactions and trending players');
  const [tx1, tx0, trendingAdd, trendingDrop] = await Promise.all([
    sleeper.transactions(cfg.league_id, week), sleeper.transactions(cfg.league_id, week - 1),
    sleeper.trending('add', 24, 50), sleeper.trending('drop', 24, 25)]);
  const transactions = [...(tx1 || []).map(t => [week, t]), ...(tx0 || []).map(t => [week - 1, t])];
  const schedule = await loadSchedule(season, refresh);
  const byes = SleeperReport.byeTeams(schedule, week, SleeperReport.teamCodes(players));
  if (byes === null) notes.push('Bye weeks could not be determined (schedule endpoint unavailable).');
  return { config: cfg, season, week, league, rosters: rosters || [], users: users || [], matchups: matchups || [],
    transactions, trending_add: trendingAdd || [], trending_drop: trendingDrop || [], players, bye_teams: byes, notes };
}
async function recommend(markdown, log) {
  const key = apiKey();
  if (!key) throw new Error('No Anthropic API key saved (see Settings), or use bundle-only.');
  log(`Requesting recommendations from ${MODEL}...`);
  const r = await fetch(ANTHROPIC_URL, {
    method: 'POST',
    headers: { 'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json',
      'anthropic-dangerous-direct-browser-access': 'true' },
    body: JSON.stringify({ model: MODEL, max_tokens: MAX_TOKENS, system: SYSTEM_PROMPT, messages: [{ role: 'user', content: markdown }] }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`Anthropic API: HTTP ${r.status} ${(data.error && data.error.message) || ''}`);
  if (data.stop_reason === 'refusal') throw new Error('model refused the request');
  let text = (data.content || []).filter(b => b.type === 'text').map(b => b.text).join('\n').trim();
  if (!text) throw new Error(`empty response from model (stop_reason=${data.stop_reason})`);
  if (data.stop_reason === 'max_tokens') text += `\n\n_(Response was cut off at max_tokens=${MAX_TOKENS}.)_`;
  return text;
}
const RECS = '## Recommendations';
async function generate({ week, bundleOnly, refresh }, log) {
  const cfg = config();
  if (!cfg) throw new Error('no configuration yet; run setup first');
  const data = await fetchWeek(cfg, week, refresh, log);
  let markdown = SleeperReport.buildBundle(data);
  saveReport(data.week, markdown);
  log(`Saved bundle for week ${data.week}`);
  if (!bundleOnly) {
    try { markdown = await addRecommendations(data.week, log); }
    catch (e) { throw new Error(`${e.message} (bundle for week ${data.week} was still saved)`); }
  }
  return { week: data.week };
}
async function addRecommendations(week, log) {
  const rep = reports()[week];
  if (!rep) throw new Error(`no bundle for week ${week}`);
  let markdown = rep.markdown;
  if (markdown.includes(RECS)) markdown = markdown.split(RECS)[0].trimEnd() + '\n';
  const rec = await recommend(markdown, log);
  markdown += `\n${RECS}\n\n` + rec.trim() + '\n';
  saveReport(week, markdown);
  log('Recommendations appended');
  return markdown;
}

// ---------------------------------------------------------------- UI
const $ = (s, el = document) => el.querySelector(s);
const state = { status: null, week: null, running: false };
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 2400); }
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function ago(ts) {
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  if (m < 1) return 'just now'; if (m < 60) return m + ' min ago';
  const h = Math.round(m / 60); if (h < 48) return h + ' h ago';
  return Math.round(h / 24) + ' d ago';
}
let stateCache = { ts: 0, value: null };
async function nflState() {
  if (stateCache.value && Date.now() - stateCache.ts < 600000) return stateCache.value;
  try { stateCache = { ts: Date.now(), value: await getJSON(`${SLEEPER}/state/nfl`, { retries: 0, timeoutMs: 8000 }) }; } catch {}
  return stateCache.value;
}
async function buildStatus() {
  const cfg = config();
  const st = await nflState();
  const list = Object.entries(reports()).map(([w, r]) => ({ week: +w, has_recommendations: r.markdown.includes(RECS), modified: r.modified }))
    .sort((a, b) => b.week - a.week);
  return { config: cfg, state: st, current_week: st ? Number(st.week || st.leg || 1) : null,
    players_cache: await playersCacheStatus(), api_key_set: !!apiKey(), reports: list };
}
async function refreshStatus(openLatest) {
  state.status = await buildStatus();
  renderHeader(); renderReports();
  const s = state.status;
  if (!s.config) { showSetup(); return; }
  if (openLatest || state.week === null) {
    if (s.reports.length) openReport(s.reports[0].week);
    else renderContent('<div class="cta">No reports yet. Pick a week on the left and click <strong>Generate report</strong>.</div>');
  }
}
function renderHeader() {
  const s = state.status, cfg = s.config;
  $('#hdr-league').textContent = cfg ? `${cfg.league_name || cfg.league_id} · ${cfg.season}` + (s.current_week ? ` · week ${s.current_week}` : '') : 'not configured';
  const chips = [];
  chips.push(cfg ? `<span class="chip ok">✓ ${esc(cfg.username)} <button id="resetup" title="Re-run setup">change</button></span>`
                 : '<span class="chip warn"><button id="resetup">setup needed →</button></span>');
  chips.push(s.api_key_set ? '<span class="chip ok">✓ API key</span>' : '<span class="chip warn" title="Add your Anthropic key under Settings">no API key</span>');
  const pc = s.players_cache;
  chips.push(pc.exists ? `<span class="chip ${pc.stale ? 'warn' : ''}" title="cached player database age">players ${pc.age_hours}h old</span>` : '<span class="chip warn">no player cache</span>');
  $('#hdr-chips').innerHTML = chips.join('');
  $('#resetup').onclick = showSetup;
  $('#week').placeholder = s.current_week ? `current (${s.current_week})` : 'current';
  $('#week-hint').textContent = s.current_week ? `Leave blank for the current week (${s.current_week}).` : '';
  if (!s.api_key_set) $('#bundle-only').checked = true;
  $('#run').disabled = !cfg || state.running;
  $('#api-key').value = apiKey();
}
function renderReports() {
  const list = state.status.reports;
  if (!list.length) { $('#reports').innerHTML = '<li class="empty">none yet</li>'; return; }
  $('#reports').innerHTML = list.map(r => `
    <li data-week="${r.week}" class="${r.week === state.week ? 'active' : ''}">
      <span class="wk">Week ${r.week}</span>
      <span class="badge ${r.has_recommendations ? 'recs' : ''}">${r.has_recommendations ? 'recs' : 'bundle'}</span>
      <span class="when">${ago(r.modified)}</span>
    </li>`).join('');
  document.querySelectorAll('#reports li[data-week]').forEach(li => li.onclick = () => openReport(+li.dataset.week));
}
function renderContent(html) { $('#content').innerHTML = html; }
function log(line, isErr) {
  const el = $('#log'); el.classList.add('show');
  const div = document.createElement('div'); if (isErr) div.className = 'err'; div.textContent = line; el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}
async function runJob(label, fn) {
  if (state.running) { toast('A job is already running'); return; }
  state.running = true; $('#run').disabled = true; $('#run').innerHTML = '<span class="spinner"></span>Working…';
  $('#log').innerHTML = ''; log(`▶ ${label}`);
  let wk = null;
  try { const r = await fn(log); wk = r && r.week; log('✓ done'); }
  catch (e) {
    log('✗ ' + e.message, true); toast('Failed: ' + e.message.slice(0, 80));
    wk = (/week (\d+)/.exec(e.message) || [])[1] ? +(/week (\d+)/.exec(e.message) || [])[1] : null;
  }
  state.running = false; $('#run').disabled = false; $('#run').textContent = 'Generate report';
  state.status = await buildStatus(); renderHeader(); renderReports();
  if (wk && reports()[wk]) openReport(wk);
}
function startReport() {
  const week = $('#week').value ? +$('#week').value : null;
  const bundleOnly = $('#bundle-only').checked, refresh = $('#refresh').checked;
  runJob(week ? `Week ${week} report` : 'Current week report', l => generate({ week, bundleOnly, refresh }, l));
}

// ---------------------------------------------------------------- report view + markdown
function openReport(week) {
  const rep = reports()[week];
  if (!rep) { toast(`no report for week ${week}`); return; }
  state.week = week; renderReports();
  const [bundle, recs] = rep.markdown.split(/^## Recommendations\s*$/m);
  const title = (/^#\s+(.*)$/m.exec(bundle) || [, `Week ${week}`])[1];
  const keyOk = !!apiKey();
  renderContent(`
    <div class="card">
      <div class="toolbar">
        <h1>${esc(title)}</h1>
        <div class="actions">
        ${recs === undefined ? `<button class="primary" id="btn-recs" ${keyOk ? '' : 'disabled title="Add your Anthropic API key under Settings"'}>Get recommendations</button>`
                             : '<button id="btn-recs" title="Re-run the analysis">Regenerate recommendations</button>'}
        <button id="btn-copy">Copy markdown</button>
        <button id="btn-dl">Download</button>
        <button id="btn-del" title="Remove this report from this browser">Delete</button>
        </div>
        <div class="meta">Updated ${ago(rep.modified)} · stored in this browser</div>
      </div>
      <nav class="sections" id="secnav"></nav>
      <div class="md" id="md-bundle">${renderMarkdown(bundle)}</div>
      ${recs !== undefined
        ? `<div class="recs-wrap"><div class="md" id="md-recs"><h2 id="sec-recommendations">Recommendations</h2>${renderMarkdown(recs)}</div></div>`
        : `<div class="cta">No recommendations yet for this week.${keyOk ? ' Click <strong>Get recommendations</strong> to send this bundle to Claude.' : ' Add your Anthropic API key under Settings to enable the analysis step.'}</div>`}
    </div>`);
  decorateTables();
  const nav = $('#secnav');
  document.querySelectorAll('#content .md h2').forEach(h => {
    const a = document.createElement('a'); a.href = '#' + h.id; a.textContent = h.textContent.replace(/^\d+\.\s*/, '');
    if (h.id === 'sec-recommendations') a.className = 'recs';
    a.onclick = ev => { ev.preventDefault(); h.scrollIntoView({ behavior: 'smooth', block: 'start' }); };
    nav.appendChild(a);
  });
  $('#btn-recs').onclick = () => runJob(`Week ${week} recommendations`, l => addRecommendations(week, l).then(() => ({ week })));
  $('#btn-copy').onclick = async () => { await navigator.clipboard.writeText(rep.markdown); toast('Markdown copied'); };
  $('#btn-dl').onclick = () => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([rep.markdown], { type: 'text/markdown' })); a.download = `report-week${week}.md`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  $('#btn-del').onclick = () => {
    if (!confirm(`Delete the week ${week} report from this browser?`)) return;
    const r = reports(); delete r[week]; store.set(LS.reports, r); state.week = null; refreshStatus(true);
  };
}
function inline(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
  s = s.replace(/(^|[\s(])_([^_\s][^_]*?)_(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}
function slug(s) { return 'sec-' + s.toLowerCase().replace(/^\d+\.\s*/, '').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }
function renderMarkdown(md) {
  const lines = md.replace(/\r/g, '').split('\n'); const out = []; let i = 0;
  const isBlock = l => /^(#{1,6}\s|\s*[-*]\s|\s*\d+[.)]\s|>|\||---)/.test(l);
  while (i < lines.length) {
    const line = lines[i]; let m;
    if (!line.trim()) { i++; continue; }
    if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) { const lvl = m[1].length; out.push(`<h${lvl} id="${slug(m[2])}">${inline(m[2])}</h${lvl}>`); i++; continue; }
    if (/^---+\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
    if (line.trim().startsWith('|')) {
      const rows = []; while (i < lines.length && lines[i].trim().startsWith('|')) rows.push(lines[i++]);
      const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      const body = rows.filter(r => !/^\|?[\s:|-]+\|?$/.test(r.trim()));
      if (!body.length) continue;
      let t = '<div class="tablewrap"><table><thead><tr>' + cells(body[0]).map(c => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>';
      for (const r of body.slice(1)) t += '<tr>' + cells(r).map(c => `<td>${inline(c)}</td>`).join('') + '</tr>';
      out.push(t + '</tbody></table></div>'); continue;
    }
    if (/^\s*[-*]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line); const items = [];
      const re = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*]\s+(.*)$/;
      while (i < lines.length && (m = re.exec(lines[i]))) {
        let item = m[1]; i++;
        while (i < lines.length && lines[i].trim() && !isBlock(lines[i])) item += ' ' + lines[i++].trim();
        items.push(item);
      }
      out.push(`<${ordered ? 'ol' : 'ul'}>` + items.map(x => `<li>${inline(x)}</li>`).join('') + `</${ordered ? 'ol' : 'ul'}>`); continue;
    }
    if (line.startsWith('>')) {
      const q = []; while (i < lines.length && lines[i].startsWith('>')) q.push(lines[i++].replace(/^>\s?/, ''));
      out.push('<blockquote>' + inline(q.join(' ')) + '</blockquote>'); continue;
    }
    const p = []; while (i < lines.length && lines[i].trim() && !isBlock(lines[i])) p.push(lines[i++].trim());
    out.push('<p>' + inline(p.join(' ')) + '</p>');
  }
  return out.join('\n');
}
function decorateTables() {
  document.querySelectorAll('#content table').forEach(table => {
    const heads = [...table.querySelectorAll('th')].map(th => th.textContent.trim().toLowerCase());
    const col = name => heads.indexOf(name);
    const iInj = col('injury'), iBye = col('bye'), iStart = col('starting'), iPractice = col('practice');
    table.querySelectorAll('tbody tr').forEach(tr => {
      const tds = tr.children;
      if (iInj >= 0 && tds[iInj]) {
        const t = tds[iInj].textContent.trim();
        if (/^(out|ir|pup|sus|dnr|doubtful|cov|na)\b/i.test(t)) tds[iInj].innerHTML = `<span class="pill bad">${esc(t)}</span>`;
        else if (/^questionable/i.test(t)) tds[iInj].innerHTML = `<span class="pill warn">${esc(t)}</span>`;
      }
      if (iPractice >= 0 && tds[iPractice] && /^(limited|dnp|out)/i.test(tds[iPractice].textContent.trim()))
        tds[iPractice].innerHTML = `<span class="pill warn">${esc(tds[iPractice].textContent.trim())}</span>`;
      if (iBye >= 0 && tds[iBye] && /bye/i.test(tds[iBye].textContent)) tds[iBye].innerHTML = '<span class="pill bye">BYE</span>';
      if (iStart >= 0 && tds[iStart]) {
        const v = tds[iStart].textContent.trim();
        tr.classList.add(v === 'yes' ? 'starter' : 'bench');
        if (v === 'yes') tds[iStart].innerHTML = '<span class="pill ok">starter</span>';
        else if (v === 'no') tds[iStart].textContent = 'bench';
      }
    });
  });
}

// ---------------------------------------------------------------- setup wizard
let wiz = { info: null, league: null };
function showSetup() {
  wiz = { info: null, league: null };
  const cfg = config();
  renderContent(`
    <div class="card wizard">
      <h1>Connect your Sleeper league</h1>
      <p>Enter your Sleeper username. This page looks up your leagues for the current season and the roster you own, and saves the ids in this browser only. Nothing is sent anywhere except Sleeper.</p>
      <div class="field"><label for="su-name">Sleeper username</label>
        <div class="row"><input type="text" id="su-name" value="${cfg ? esc(cfg.username) : ''}" autocomplete="off" spellcheck="false">
        <button class="primary" id="su-lookup">Find leagues</button></div></div>
      <div id="su-out"></div>
      ${cfg ? '<p style="margin-top:14px"><a href="#" id="su-cancel">Cancel, keep current setup</a></p>' : ''}
    </div>`);
  $('#su-lookup').onclick = lookup;
  $('#su-name').onkeydown = e => { if (e.key === 'Enter') lookup(); };
  $('#su-name').focus();
  const c = $('#su-cancel'); if (c) c.onclick = e => { e.preventDefault(); refreshStatus(true); };
}
async function lookup() {
  const out = $('#su-out'); const btn = $('#su-lookup');
  const username = $('#su-name').value.trim();
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Looking up…'; out.innerHTML = '';
  try {
    if (!username) throw new Error('username is required');
    const user = await sleeper.user(username);
    if (!user || !user.user_id) throw new Error(`Sleeper user '${username}' not found`);
    const st = await sleeper.state();
    const season = String(st.season);
    const leagues = (await sleeper.userLeagues(user.user_id, season)) || [];
    if (!leagues.length) throw new Error(`no NFL leagues found for ${username} in ${season}`);
    wiz.info = { user: { user_id: String(user.user_id), username, display_name: user.display_name || username }, state: st, season, leagues };
    out.innerHTML = `
      <p>Found <strong>${esc(wiz.info.user.display_name)}</strong> (user id ${esc(wiz.info.user.user_id)}). Sleeper says season ${esc(season)}, week ${esc(String(st.week))}.</p>
      <label>${leagues.length === 1 ? 'Your league' : 'Pick a league'}</label>
      <ul class="leagues">${leagues.map((l, i) => `
        <li data-i="${i}" class="${leagues.length === 1 ? 'sel' : ''}"><span class="name">${esc(l.name || l.league_id)}</span>
        <span class="badge">${l.total_rosters} teams</span><span class="badge">${esc(l.status || '')}</span></li>`).join('')}</ul>
      <button class="primary" id="su-finish" ${leagues.length === 1 ? '' : 'disabled'}>Save configuration</button>`;
    if (leagues.length === 1) wiz.league = leagues[0];
    out.querySelectorAll('.leagues li').forEach(li => li.onclick = () => {
      out.querySelectorAll('.leagues li').forEach(x => x.classList.remove('sel')); li.classList.add('sel');
      wiz.league = leagues[+li.dataset.i]; $('#su-finish').disabled = false;
    });
    $('#su-finish').onclick = finish;
  } catch (e) { out.innerHTML = `<div class="error">${esc(e.message)}</div>`; }
  btn.disabled = false; btn.textContent = 'Find leagues';
}
async function finish() {
  const btn = $('#su-finish'); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Saving…';
  try {
    const u = wiz.info.user, lg = wiz.league;
    const rosters = (await sleeper.rosters(lg.league_id)) || [];
    let mine = rosters.find(r => String(r.owner_id) === u.user_id);
    if (!mine) mine = rosters.find(r => (r.co_owners || []).map(String).includes(u.user_id));
    if (!mine) throw new Error(`no roster owned by user_id ${u.user_id} in league ${lg.league_id}`);
    store.set(LS.config, { user_id: u.user_id, username: u.username, league_id: String(lg.league_id), roster_id: Number(mine.roster_id),
      season: wiz.info.season, league_name: lg.name || null });
    toast('Configuration saved'); state.week = null; await refreshStatus(true);
  } catch (e) { $('#su-out').insertAdjacentHTML('beforeend', `<div class="error">${esc(e.message)}</div>`); btn.disabled = false; btn.textContent = 'Save configuration'; }
}

// ---------------------------------------------------------------- settings
function wireSettings() {
  $('#api-key-save').onclick = () => {
    const v = $('#api-key').value.trim();
    try { if (v) localStorage.setItem(LS.apiKey, v); else localStorage.removeItem(LS.apiKey); } catch {}
    toast(v ? 'API key saved in this browser' : 'API key cleared');
    refreshStatus(false).then(() => { if (state.week !== null) openReport(state.week); });
  };
  $('#clear-players').onclick = async () => { await idbDel('players'); toast('Player cache cleared'); refreshStatus(false); };
  $('#reset-all').onclick = async () => {
    if (!confirm('Remove the saved league config, API key, and all reports from this browser?')) return;
    for (const k of Object.values(LS)) store.del(k);
    await idbDel('players'); state.week = null; refreshStatus(true);
  };
}

$('#run').onclick = startReport;
wireSettings();
refreshStatus(true).catch(e => renderContent(`<div class="error">${esc(e.message)}</div>`));
