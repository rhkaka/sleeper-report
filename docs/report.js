/* Browser port of sleeper_report/bundle.py. Kept line-for-line equivalent so the
   Python and hosted versions produce the same markdown (tests/test_pages_parity.sh). */
(function (root) {
  'use strict';

  const FA_CAP = 40, FA_TRENDING_MAX = 28, ACTIVITY_CAP = 40, TRENDING_DROP_SHOWN = 10;
  const NON_STARTER_SLOTS = new Set(['BN', 'IR', 'TAXI']);
  const WAIVER_TYPES = { 0: 'rolling waivers', 1: 'reverse standings', 2: 'FAAB' };
  const FANTASY_POSITIONS = ['QB', 'RB', 'WR', 'TE', 'K', 'DEF'];
  const PLAYER_FIELDS = ['player_id', 'full_name', 'position', 'team', 'injury_status', 'injury_body_part',
    'practice_participation', 'status', 'years_exp', 'age', 'number'];

  // ---------------------------------------------------------------- players.py equivalents
  function trimPlayer(pid, rec) {
    const out = {};
    for (const k of PLAYER_FIELDS) out[k] = rec[k] === undefined ? null : rec[k];
    out.player_id = pid;
    if (!out.full_name) out.full_name = [rec.first_name, rec.last_name].filter(Boolean).join(' ') || pid;
    out.search_rank = rec.search_rank || 9999999;
    out.active = !!rec.active;
    return out;
  }
  function isFantasyRelevant(p) {
    if (!FANTASY_POSITIONS.includes(p.position)) return false;
    if (p.position === 'DEF') return !!p.active;
    return p.status === 'Active';
  }
  function teamCodes(players) {
    const s = new Set();
    for (const p of Object.values(players)) if (p.position === 'DEF' && p.active) s.add(p.player_id);
    return s;
  }
  function byeTeams(schedule, week, allTeams) {
    if (!schedule || !schedule.length) return null;
    const playing = new Set();
    for (const g of schedule) if (g.week === week) { if (g.home) playing.add(g.home); if (g.away) playing.add(g.away); }
    if (!playing.size) return null;
    return new Set([...allTeams].filter(t => !playing.has(t)));
  }

  // ---------------------------------------------------------------- formatting helpers
  const num = v => (v === null || v === undefined || v === '' ? 0 : Number(v)) || 0;
  const fpts = (settings, key) => num(settings[key]) + num(settings[key + '_decimal']) / 100;
  const record = s => `${s.wins || 0}-${s.losses || 0}` + (s.ties ? `-${s.ties}` : '');
  const dash = v => (v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length)) ? '—' : String(v);
  const g = v => { const n = Number(v); return Number(n.toPrecision(6)).toString(); };      // Python :g
  const gsigned = v => (Number(v) >= 0 ? '+' : '') + g(v);                                  // Python :+g
  // Python's f"{x:.1f}" rounds exact ties half-to-even (107.25 -> 107.2); toFixed rounds them up.
  const fixed = (v, d) => {
    const x = Number(v), scaled = x * 10 ** d;
    if (!Number.isInteger(scaled) && Number.isInteger(scaled * 2)) {           // exact .5 tie
      const lo = Math.floor(scaled), n = lo % 2 === 0 ? lo : lo + 1;
      return (n / 10 ** d).toFixed(d);
    }
    return x.toFixed(d);
  };
  const thousands = v => Number(v || 0).toLocaleString('en-US');
  function median(xs) {
    const a = [...xs].sort((x, y) => x - y); const n = a.length;
    if (!n) throw new Error('median of empty list');
    return n % 2 ? a[(n - 1) / 2] : (a[n / 2 - 1] + a[n / 2]) / 2;
  }
  const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  function injury(p) {
    if (!p.injury_status) return '—';
    return p.injury_body_part ? `${p.injury_status} (${p.injury_body_part})` : p.injury_status;
  }
  function scoringSummary(scoring) {
    const rec = num(scoring.rec);
    let fmt;
    if (rec >= 1) fmt = 'Full PPR'; else if (rec >= 0.5) fmt = 'Half PPR';
    else if (rec > 0) fmt = `${g(rec)}-pt PPR`; else fmt = 'Standard (no PPR)';
    const bits = [fmt];
    const add = (label, key, per = '') => {
      if (key in scoring && scoring[key] !== null && scoring[key] !== 0) bits.push(`${label} ${gsigned(scoring[key])}${per}`);
    };
    add('pass TD', 'pass_td'); add('pass yd', 'pass_yd', '/yd'); add('INT', 'pass_int');
    add('rush TD', 'rush_td'); add('rush yd', 'rush_yd', '/yd'); add('rec TD', 'rec_td');
    add('rec yd', 'rec_yd', '/yd'); add('TE rec bonus', 'bonus_rec_te'); add('fum lost', 'fum_lost');
    return bits.join(', ');
  }
  function slotSummary(rosterPositions) {
    const counts = {}; const order = [];
    for (const s of rosterPositions) { if (!(s in counts)) { order.push(s); counts[s] = 0; } counts[s]++; }
    return order.map(s => counts[s] > 1 ? `${counts[s]} ${s}` : s).join(', ');
  }
  function ts(ms) {
    if (!ms) return '?';
    return new Date(ms).toLocaleString('en-US', { month: 'short', day: '2-digit', timeZone: 'UTC' });
  }

  // ---------------------------------------------------------------- league lookups
  class League {
    constructor(d) {
      this.d = d;
      this.display = {}; this.teamName = {};
      for (const u of d.users) { this.display[u.user_id] = u.display_name || u.user_id; this.teamName[u.user_id] = (u.metadata || {}).team_name || null; }
      this.byRosterId = {}; for (const r of d.rosters) this.byRosterId[r.roster_id] = r;
      this.rostered = {};
      for (const r of d.rosters) for (const pid of (r.players || [])) this.rostered[pid] = r.roster_id;
    }
    ownerName(rid) {
      const r = rid === null || rid === undefined ? undefined : this.byRosterId[rid];
      const ridText = rid === null || rid === undefined ? 'None' : rid;
      if (!r) return `roster ${ridText}`;
      const name = r.owner_id in this.display ? this.display[r.owner_id] : `roster ${ridText}`;
      const team = this.teamName[r.owner_id];
      return team ? `${name} (${team})` : name;
    }
    player(pid) {
      return this.d.players[pid] || { player_id: pid, full_name: `Unknown player ${pid}`, position: '?', team: null };
    }
    short(pid) { const p = this.player(pid); return `${p.full_name} (${p.position || '?'}, ${p.team || 'FA'})`; }
    onBye(p) { return !!(this.d.bye_teams && this.d.bye_teams.size) && this.d.bye_teams.has(p.team); }
  }

  // ---------------------------------------------------------------- sections
  function sectionLeagueContext(L) {
    const d = L.d, cfg = d.config, league = d.league;
    const settings = league.settings || {}, scoring = league.scoring_settings || {};
    const my = L.byRosterId[cfg.roster_id];
    if (!my) throw new Error(`roster_id ${cfg.roster_id} not found in league ${cfg.league_id}; re-run setup`);
    const ms = my.settings || {};
    const waiverType = settings.waiver_type in WAIVER_TYPES ? WAIVER_TYPES[settings.waiver_type] : `type ${settings.waiver_type}`;
    const budget = settings.waiver_budget;
    const used = ms.waiver_budget_used || 0;
    const faab = budget ? `$${budget - used} of $${budget} remaining` : 'n/a (not a FAAB league)';
    const pf = d.rosters.map(r => fpts(r.settings || {}, 'fpts'));
    const pa = d.rosters.map(r => fpts(r.settings || {}, 'fpts_against'));
    const myPf = fpts(ms, 'fpts'), myPa = fpts(ms, 'fpts_against');
    const standings = [...d.rosters].sort((a, b) => {
      const sa = a.settings || {}, sb = b.settings || {};
      return ((sb.wins || 0) - (sa.wins || 0)) || (fpts(sb, 'fpts') - fpts(sa, 'fpts'));
    });
    const idx = standings.findIndex(r => r.roster_id === cfg.roster_id);
    const rank = idx >= 0 ? idx + 1 : '?';
    const lines = [
      '## 1. League context', '',
      `- **League:** ${league.name ?? cfg.league_id} — ${league.total_rosters ?? d.rosters.length} teams, season ${d.season}, week ${d.week}`
        + (settings.playoff_week_start ? `, playoffs start week ${settings.playoff_week_start}` : ''),
      `- **Scoring:** ${scoringSummary(scoring)}`,
      `- **Roster slots:** ${slotSummary(league.roster_positions || [])}`,
      `- **Waivers:** ${waiverType}` + (budget ? `, budget $${budget}` : ''),
      `- **My FAAB:** ${faab}`,
      `- **My record:** ${record(ms)} (rank ${rank} of ${d.rosters.length}), PF ${fixed(myPf, 1)} vs league median ${fixed(median(pf), 1)}, PA ${fixed(myPa, 1)} vs league median ${fixed(median(pa), 1)}`,
      `- **Manager:** ${L.ownerName(cfg.roster_id)} (Sleeper user \`${cfg.username}\`)`,
    ];
    if (d.bye_teams === null || d.bye_teams === undefined) lines.push('- **Bye weeks:** schedule unavailable; bye flags below are not shown');
    else if (d.bye_teams.size) lines.push(`- **Teams on bye this week:** ${[...d.bye_teams].sort().join(', ')}`);
    else lines.push('- **Teams on bye this week:** none');
    return lines.join('\n');
  }

  function rosterTable(L, roster) {
    const slots = (L.d.league.roster_positions || []).filter(s => !NON_STARTER_SLOTS.has(s));
    const starters = roster.starters || [], players = roster.players || [];
    const reserve = roster.reserve || [], taxi = roster.taxi || [];
    const starterSet = new Set(starters.filter(pid => pid && pid !== '0'));
    const rows = ['| Slot | Player | Pos | Team | Injury | Practice | Bye | Starting |', '|---|---|---|---|---|---|---|---|'];
    const row = (slot, pid) => {
      const p = L.player(pid);
      const bye = L.onBye(p) ? '**BYE**' : '';
      const starting = starterSet.has(pid) ? 'yes' : 'no';
      return `| ${slot} | ${p.full_name} | ${dash(p.position)} | ${dash(p.team)} | ${injury(p)} | ${dash(p.practice_participation)} | ${bye} | ${starting} |`;
    };
    slots.forEach((slot, i) => {
      const pid = i < starters.length ? starters[i] : null;
      rows.push(!pid || pid === '0' ? `| ${slot} | *(empty)* | | | | | | — |` : row(slot, pid));
    });
    for (const pid of players) if (!starterSet.has(pid) && !reserve.includes(pid) && !taxi.includes(pid)) rows.push(row('BN', pid));
    for (const pid of reserve) rows.push(row('IR', pid));
    for (const pid of taxi) rows.push(row('TAXI', pid));
    return rows.join('\n');
  }

  function sectionMyRoster(L) {
    const my = L.byRosterId[L.d.config.roster_id];
    const lines = ['## 2. My roster', '', rosterTable(L, my)];
    const byes = (my.players || []).filter(pid => L.onBye(L.player(pid))).map(pid => L.player(pid).full_name);
    if (byes.length) lines.push('', `**On bye this week:** ${byes.join(', ')}`);
    return lines.join('\n');
  }

  function sectionMatchup(L) {
    const d = L.d;
    const mine = d.matchups.find(m => m.roster_id === d.config.roster_id);
    const lines = ["## 3. This week's matchup", ''];
    if (!mine || mine.matchup_id === null || mine.matchup_id === undefined) {
      lines.push(`No matchup found for week ${d.week} (bye week, playoffs not started, or eliminated).`); return lines.join('\n');
    }
    const opp = d.matchups.find(m => m.matchup_id === mine.matchup_id && m.roster_id !== d.config.roster_id);
    if (!opp) { lines.push(`Matchup ${mine.matchup_id} has no opponent entry (median matchup or bye).`); return lines.join('\n'); }
    const oppRoster = L.byRosterId[opp.roster_id];
    if (!oppRoster) { lines.push(`Opponent roster ${opp.roster_id} not found.`); return lines.join('\n'); }
    const os = oppRoster.settings || {};
    lines.push(`**Opponent:** ${L.ownerName(opp.roster_id)} — record ${record(os)}, PF ${fixed(fpts(os, 'fpts'), 1)}, PA ${fixed(fpts(os, 'fpts_against'), 1)}`);
    if ((mine.points || 0) > 0 || (opp.points || 0) > 0) lines.push(`**Score so far:** me ${fixed(mine.points || 0, 2)} — opponent ${fixed(opp.points || 0, 2)}`);
    lines.push('', "Opponent's roster (their current starters per Sleeper):", '', rosterTable(L, oppRoster));
    const byes = (oppRoster.players || []).filter(pid => L.onBye(L.player(pid))).map(pid => L.player(pid).full_name);
    if (byes.length) lines.push('', `**Opponent players on bye:** ${byes.join(', ')}`);
    return lines.join('\n');
  }

  function recentDrops(L) {
    const out = {};
    const txs = [...L.d.transactions].sort((a, b) => (a[1].created || 0) - (b[1].created || 0));
    for (const [week, tx] of txs) {
      if (tx.status !== 'complete') continue;
      for (const [pid, rid] of Object.entries(tx.drops || {})) out[pid] = [week, L.ownerName(rid)];
    }
    return out;
  }

  function sectionWaivers(L) {
    const d = L.d, players = d.players;
    const freeAgents = {};
    for (const [pid, p] of Object.entries(players)) if (isFantasyRelevant(p) && !(pid in L.rostered)) freeAgents[pid] = p;
    const dropped = recentDrops(L);
    const note = p => {
      const bits = [];
      if (p.player_id in dropped) { const [wk, who] = dropped[p.player_id]; bits.push(`dropped wk ${wk} by ${who}`); }
      if (L.onBye(p)) bits.push('BYE');
      return bits.join('; ');
    };
    const lines = ['## 4. Waiver wire', ''];
    const trendingFa = d.trending_add.filter(t => t.player_id in freeAgents).slice(0, FA_TRENDING_MAX);
    const trendingRostered = d.trending_add.filter(t => t.player_id in L.rostered).slice(0, 10);
    lines.push(`### Trending adds (Sleeper-wide, last 24h) that are free agents in this league — ${trendingFa.length}`, '');
    if (trendingFa.length) {
      lines.push('| # | Player | Pos | Team | Adds (24h) | Injury | Practice | Note |', '|---|---|---|---|---|---|---|---|');
      trendingFa.forEach((t, i) => {
        const p = freeAgents[t.player_id];
        lines.push(`| ${i + 1} | ${p.full_name} | ${dash(p.position)} | ${dash(p.team)} | ${thousands(t.count)} | ${injury(p)} | ${dash(p.practice_participation)} | ${note(p)} |`);
      });
    } else lines.push('None of the top trending adds are available.');
    if (trendingRostered.length) {
      lines.push('', 'Trending adds already rostered in this league: '
        + trendingRostered.map(t => `${L.short(t.player_id)} → ${L.ownerName(L.rostered[t.player_id])}`).join('; '));
    }
    const remainingSlots = Math.max(FA_CAP - trendingFa.length, 0);
    const trendingIds = new Set(trendingFa.map(t => t.player_id));
    const byPos = {}; for (const pos of FANTASY_POSITIONS) byPos[pos] = [];
    for (const [pid, p] of Object.entries(freeAgents)) if (!trendingIds.has(pid)) byPos[p.position].push(p);
    for (const pos of FANTASY_POSITIONS) byPos[pos].sort((a, b) => (a.search_rank - b.search_rank) || cmpStr(a.full_name, b.full_name));
    const chosen = {}; for (const pos of FANTASY_POSITIONS) chosen[pos] = [];
    let picked = 0;
    while (picked < remainingSlots) {
      let progressed = false;
      for (const pos of FANTASY_POSITIONS) {
        if (picked >= remainingSlots) break;
        const idx = chosen[pos].length;
        if (idx < byPos[pos].length) { chosen[pos].push(byPos[pos][idx]); picked++; progressed = true; }
      }
      if (!progressed) break;
    }
    lines.push('', `### Other available players by position — ${picked} shown of ${Object.keys(freeAgents).length - trendingFa.length} eligible`, '');
    for (const pos of FANTASY_POSITIONS) {
      if (!chosen[pos].length) continue;
      lines.push(`**${pos}:** ` + chosen[pos].map(p =>
        `${p.full_name} (${p.team || 'FA'}` + (p.injury_status ? `, ${p.injury_status}` : '') + (note(p) ? `, ${note(p)}` : '') + ')').join('; '));
    }
    const drops = d.trending_drop.slice(0, TRENDING_DROP_SHOWN);
    if (drops.length) {
      lines.push('', `### Trending drops (Sleeper-wide, last 24h, top ${drops.length})`, '');
      for (const t of drops) {
        const pid = t.player_id;
        const where = pid in L.rostered ? `rostered here by ${L.ownerName(L.rostered[pid])}` : 'free agent here';
        lines.push(`- ${L.short(pid)} — ${thousands(t.count)} drops; ${where}`);
      }
    }
    return lines.join('\n');
  }

  function describeTransaction(L, week, tx) {
    const kind = tx.type ?? '?', status = tx.status ?? '?';
    const adds = tx.adds || {}, drops = tx.drops || {}, settings = tx.settings || {};
    const prefix = `- **Wk ${week} · ${ts(tx.created)} · ${kind}**`;
    if (kind === 'trade') {
      const parts = [];
      for (const rid of (tx.roster_ids || [])) {
        const got = Object.entries(adds).filter(([, to]) => to === rid).map(([pid]) => L.short(pid));
        const picks = (tx.draft_picks || []).filter(pk => pk.owner_id === rid).map(pk => `${pk.season} R${pk.round} pick`);
        const faab = (tx.waiver_budget || []).filter(wb => wb.receiver === rid).map(wb => `$${wb.amount} FAAB`);
        const received = [...got, ...picks, ...faab].join(', ') || 'nothing';
        parts.push(`${L.ownerName(rid)} receives ${received}`);
      }
      const tail = status === 'complete' ? '' : ` [${status}]`;
      return `${prefix}: ` + parts.join(' | ') + tail;
    }
    const who = L.ownerName((tx.roster_ids && tx.roster_ids.length) ? tx.roster_ids[0] : null);
    const added = Object.keys(adds).map(pid => L.short(pid)).join(', ');
    const droppedS = Object.keys(drops).map(pid => L.short(pid)).join(', ');
    const bid = settings.waiver_bid;
    const bits = [];
    if (added) bits.push(`added ${added}` + (bid !== null && bid !== undefined ? ` for $${bid} FAAB` : ''));
    if (droppedS) bits.push(`dropped ${droppedS}`);
    const body = bits.join('; ') || 'no players moved';
    const tail = status === 'complete' ? '' : ` [${status} claim]`;
    return `${prefix} ${who}: ${body}${tail}`;
  }

  function sectionActivity(L) {
    const d = L.d;
    const weeks = [...new Set(d.transactions.map(([w]) => w))].sort((a, b) => b - a);
    const lines = ['## 5. Recent league activity', ''];
    lines.push(`Transactions for weeks ${weeks.join(', ') || d.week}. Failed waiver claims are included (they show what the room bid on).`, '');
    const txs = [...d.transactions].sort((a, b) => (b[1].created || 0) - (a[1].created || 0));
    let shown = 0;
    for (const [week, tx] of txs) {
      if (tx.status !== 'complete' && tx.status !== 'failed') continue;
      lines.push(describeTransaction(L, week, tx));
      shown++;
      if (shown >= ACTIVITY_CAP) { lines.push(`- … ${txs.length - shown} more not shown`); break; }
    }
    if (!shown) lines.push('- No transactions in this window.');
    return lines.join('\n');
  }

  function buildBundle(data, generatedAt) {
    const L = new League(data);
    const dt = generatedAt || new Date();
    const pad = n => String(n).padStart(2, '0');
    const generated = `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())} ${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())} UTC`;
    const header = [
      `# Fantasy report — ${data.league.name ?? data.config.league_id} — Week ${data.week}, ${data.season}`, '',
      `_Generated ${generated} from Sleeper data._`,
    ];
    if (data.notes && data.notes.length) header.push('', ...data.notes.map(n => `> ${n}`));
    return [header.join('\n'), sectionLeagueContext(L), sectionMyRoster(L), sectionMatchup(L), sectionWaivers(L), sectionActivity(L)].join('\n\n') + '\n';
  }

  const api = { buildBundle, trimPlayer, isFantasyRelevant, teamCodes, byeTeams, FANTASY_POSITIONS };
  root.SleeperReport = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
