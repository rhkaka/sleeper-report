"""Assemble the weekly markdown bundle from raw Sleeper payloads."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median

from .config import Config
from .players import FANTASY_POSITIONS, is_fantasy_relevant

FA_CAP = 40                  # total free agents listed
FA_TRENDING_MAX = 28         # of which at most this many come from the trending list
ACTIVITY_CAP = 40            # transactions listed
TRENDING_DROP_SHOWN = 10
NON_STARTER_SLOTS = {"BN", "IR", "TAXI"}
WAIVER_TYPES = {0: "rolling waivers", 1: "reverse standings", 2: "FAAB"}


@dataclass
class WeekData:
    config: Config
    season: str
    week: int
    league: dict
    rosters: list[dict]
    users: list[dict]
    matchups: list[dict]
    transactions: list[tuple[int, dict]]     # (week, transaction), both weeks merged
    trending_add: list[dict]
    trending_drop: list[dict]
    players: dict[str, dict]
    bye_teams: set[str] | None               # None => schedule unavailable
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

def _fpts(settings: dict, key: str) -> float:
    return float(settings.get(key, 0) or 0) + float(settings.get(f"{key}_decimal", 0) or 0) / 100.0


def _record(settings: dict) -> str:
    w, l, t = settings.get("wins", 0), settings.get("losses", 0), settings.get("ties", 0)
    return f"{w}-{l}" + (f"-{t}" if t else "")


def _dash(value) -> str:
    return "—" if value in (None, "", []) else str(value)


def _injury(p: dict) -> str:
    status = p.get("injury_status")
    if not status:
        return "—"
    part = p.get("injury_body_part")
    return f"{status} ({part})" if part else status


def _scoring_summary(scoring: dict) -> str:
    rec = float(scoring.get("rec", 0) or 0)
    if rec >= 1:
        fmt = "Full PPR"
    elif rec >= 0.5:
        fmt = "Half PPR"
    elif rec > 0:
        fmt = f"{rec:g}-pt PPR"
    else:
        fmt = "Standard (no PPR)"
    bits = [fmt]

    def add(label: str, key: str, per: str = "") -> None:
        if key in scoring and scoring[key] not in (None, 0):
            bits.append(f"{label} {float(scoring[key]):+g}{per}")

    add("pass TD", "pass_td")
    add("pass yd", "pass_yd", "/yd")
    add("INT", "pass_int")
    add("rush TD", "rush_td")
    add("rush yd", "rush_yd", "/yd")
    add("rec TD", "rec_td")
    add("rec yd", "rec_yd", "/yd")
    add("TE rec bonus", "bonus_rec_te")
    add("fum lost", "fum_lost")
    return ", ".join(bits)


def _slot_summary(roster_positions: list[str]) -> str:
    counts: dict[str, int] = defaultdict(int)
    order: list[str] = []
    for slot in roster_positions:
        if slot not in counts:
            order.append(slot)
        counts[slot] += 1
    return ", ".join(f"{counts[s]} {s}" if counts[s] > 1 else s for s in order)


def _ts(ms: int | None) -> str:
    if not ms:
        return "?"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%b %d")


class _League:
    """Lookups shared by every section."""

    def __init__(self, data: WeekData):
        self.d = data
        self.display = {u["user_id"]: (u.get("display_name") or u["user_id"]) for u in data.users}
        self.team_name = {
            u["user_id"]: (u.get("metadata") or {}).get("team_name") for u in data.users
        }
        self.by_roster_id = {r["roster_id"]: r for r in data.rosters}
        self.rostered: dict[str, int] = {}          # player_id -> roster_id
        for r in data.rosters:
            for pid in r.get("players") or []:
                self.rostered[pid] = r["roster_id"]

    def owner_name(self, roster_id: int | None) -> str:
        r = self.by_roster_id.get(roster_id) if roster_id is not None else None
        if not r:
            return f"roster {roster_id}"
        name = self.display.get(r.get("owner_id"), f"roster {roster_id}")
        team = self.team_name.get(r.get("owner_id"))
        return f"{name} ({team})" if team else name

    def player(self, pid: str) -> dict:
        p = self.d.players.get(pid)
        if p:
            return p
        return {"player_id": pid, "full_name": f"Unknown player {pid}", "position": "?", "team": None}

    def short(self, pid: str) -> str:
        p = self.player(pid)
        return f"{p['full_name']} ({p.get('position') or '?'}, {p.get('team') or 'FA'})"

    def on_bye(self, p: dict) -> bool:
        return bool(self.d.bye_teams) and p.get("team") in self.d.bye_teams


# --------------------------------------------------------------------------- sections

def _section_league_context(L: _League) -> str:
    d, cfg = L.d, L.d.config
    league = d.league
    settings = league.get("settings") or {}
    scoring = league.get("scoring_settings") or {}
    my = L.by_roster_id.get(cfg.roster_id)
    if my is None:
        raise SystemExit(f"roster_id {cfg.roster_id} not found in league {cfg.league_id}; re-run setup")
    ms = my.get("settings") or {}

    waiver_type = WAIVER_TYPES.get(settings.get("waiver_type"), f"type {settings.get('waiver_type')}")
    budget = settings.get("waiver_budget")
    used = ms.get("waiver_budget_used", 0) or 0
    faab = f"${budget - used} of ${budget} remaining" if budget else "n/a (not a FAAB league)"

    pf = [_fpts(r.get("settings") or {}, "fpts") for r in d.rosters]
    pa = [_fpts(r.get("settings") or {}, "fpts_against") for r in d.rosters]
    my_pf, my_pa = _fpts(ms, "fpts"), _fpts(ms, "fpts_against")
    standings = sorted(
        d.rosters,
        key=lambda r: ((r.get("settings") or {}).get("wins", 0), _fpts(r.get("settings") or {}, "fpts")),
        reverse=True,
    )
    rank = next((i + 1 for i, r in enumerate(standings) if r["roster_id"] == cfg.roster_id), "?")

    lines = [
        "## 1. League context",
        "",
        f"- **League:** {league.get('name', cfg.league_id)} — {league.get('total_rosters', len(d.rosters))} teams, "
        f"season {d.season}, week {d.week}"
        + (f", playoffs start week {settings['playoff_week_start']}" if settings.get("playoff_week_start") else ""),
        f"- **Scoring:** {_scoring_summary(scoring)}",
        f"- **Roster slots:** {_slot_summary(league.get('roster_positions') or [])}",
        f"- **Waivers:** {waiver_type}" + (f", budget ${budget}" if budget else ""),
        f"- **My FAAB:** {faab}",
        f"- **My record:** {_record(ms)} (rank {rank} of {len(d.rosters)}), "
        f"PF {my_pf:.1f} vs league median {median(pf):.1f}, "
        f"PA {my_pa:.1f} vs league median {median(pa):.1f}",
        f"- **Manager:** {L.owner_name(cfg.roster_id)} (Sleeper user `{cfg.username}`)",
    ]
    if d.bye_teams is None:
        lines.append("- **Bye weeks:** schedule unavailable; bye flags below are not shown")
    elif d.bye_teams:
        lines.append(f"- **Teams on bye this week:** {', '.join(sorted(d.bye_teams))}")
    else:
        lines.append("- **Teams on bye this week:** none")
    return "\n".join(lines)


def _roster_table(L: _League, roster: dict) -> str:
    slots = [s for s in (L.d.league.get("roster_positions") or []) if s not in NON_STARTER_SLOTS]
    starters = list(roster.get("starters") or [])
    players = list(roster.get("players") or [])
    reserve = list(roster.get("reserve") or [])
    taxi = list(roster.get("taxi") or [])
    starter_set = {pid for pid in starters if pid and pid != "0"}

    rows = ["| Slot | Player | Pos | Team | Injury | Practice | Bye | Starting |",
            "|---|---|---|---|---|---|---|---|"]

    def row(slot: str, pid: str) -> str:
        p = L.player(pid)
        bye = "**BYE**" if L.on_bye(p) else ""
        starting = "yes" if pid in starter_set else "no"
        return (f"| {slot} | {p['full_name']} | {_dash(p.get('position'))} | {_dash(p.get('team'))} | "
                f"{_injury(p)} | {_dash(p.get('practice_participation'))} | {bye} | {starting} |")

    for i, slot in enumerate(slots):
        pid = starters[i] if i < len(starters) else None
        if not pid or pid == "0":
            rows.append(f"| {slot} | *(empty)* | | | | | | — |")
        else:
            rows.append(row(slot, pid))
    bench = [pid for pid in players if pid not in starter_set and pid not in reserve and pid not in taxi]
    for pid in bench:
        rows.append(row("BN", pid))
    for pid in reserve:
        rows.append(row("IR", pid))
    for pid in taxi:
        rows.append(row("TAXI", pid))
    return "\n".join(rows)


def _section_my_roster(L: _League) -> str:
    my = L.by_roster_id[L.d.config.roster_id]
    lines = ["## 2. My roster", "", _roster_table(L, my)]
    byes = [L.player(pid)["full_name"] for pid in (my.get("players") or []) if L.on_bye(L.player(pid))]
    if byes:
        lines += ["", f"**On bye this week:** {', '.join(byes)}"]
    return "\n".join(lines)


def _section_matchup(L: _League) -> str:
    d = L.d
    mine = next((m for m in d.matchups if m.get("roster_id") == d.config.roster_id), None)
    lines = ["## 3. This week's matchup", ""]
    if mine is None or mine.get("matchup_id") is None:
        lines.append(f"No matchup found for week {d.week} (bye week, playoffs not started, or eliminated).")
        return "\n".join(lines)
    opp = next(
        (m for m in d.matchups if m.get("matchup_id") == mine["matchup_id"] and m.get("roster_id") != d.config.roster_id),
        None,
    )
    if opp is None:
        lines.append(f"Matchup {mine['matchup_id']} has no opponent entry (median matchup or bye).")
        return "\n".join(lines)
    opp_roster = L.by_roster_id.get(opp["roster_id"])
    if opp_roster is None:
        lines.append(f"Opponent roster {opp['roster_id']} not found.")
        return "\n".join(lines)
    os_ = opp_roster.get("settings") or {}
    lines.append(
        f"**Opponent:** {L.owner_name(opp['roster_id'])} — record {_record(os_)}, "
        f"PF {_fpts(os_, 'fpts'):.1f}, PA {_fpts(os_, 'fpts_against'):.1f}"
    )
    if (mine.get("points") or 0) > 0 or (opp.get("points") or 0) > 0:
        lines.append(f"**Score so far:** me {mine.get('points', 0):.2f} — opponent {opp.get('points', 0):.2f}")
    lines += ["", "Opponent's roster (their current starters per Sleeper):", "", _roster_table(L, opp_roster)]
    byes = [L.player(pid)["full_name"] for pid in (opp_roster.get("players") or []) if L.on_bye(L.player(pid))]
    if byes:
        lines += ["", f"**Opponent players on bye:** {', '.join(byes)}"]
    return "\n".join(lines)


def _recent_drops(L: _League) -> dict[str, tuple[int, str]]:
    """player_id -> (week, dropping manager) for completed transactions in the window."""
    out: dict[str, tuple[int, str]] = {}
    for week, tx in sorted(L.d.transactions, key=lambda wt: wt[1].get("created") or 0):
        if tx.get("status") != "complete":
            continue
        for pid, rid in (tx.get("drops") or {}).items():
            out[pid] = (week, L.owner_name(rid))
    return out


def _section_waivers(L: _League) -> str:
    d = L.d
    players = d.players
    free_agents = {pid: p for pid, p in players.items() if is_fantasy_relevant(p) and pid not in L.rostered}
    dropped = _recent_drops(L)

    def note(p: dict) -> str:
        bits = []
        if p["player_id"] in dropped:
            wk, who = dropped[p["player_id"]]
            bits.append(f"dropped wk {wk} by {who}")
        if L.on_bye(p):
            bits.append("BYE")
        return "; ".join(bits)

    lines = ["## 4. Waiver wire", ""]

    trending_fa = [t for t in d.trending_add if t.get("player_id") in free_agents][:FA_TRENDING_MAX]
    trending_rostered = [t for t in d.trending_add if t.get("player_id") in L.rostered][:10]
    lines.append(f"### Trending adds (Sleeper-wide, last 24h) that are free agents in this league — {len(trending_fa)}")
    lines.append("")
    if trending_fa:
        lines += ["| # | Player | Pos | Team | Adds (24h) | Injury | Practice | Note |",
                  "|---|---|---|---|---|---|---|---|"]
        for i, t in enumerate(trending_fa, 1):
            p = free_agents[t["player_id"]]
            lines.append(
                f"| {i} | {p['full_name']} | {_dash(p.get('position'))} | {_dash(p.get('team'))} | "
                f"{t.get('count', 0):,} | {_injury(p)} | {_dash(p.get('practice_participation'))} | {note(p)} |"
            )
    else:
        lines.append("None of the top trending adds are available.")

    if trending_rostered:
        lines += ["", "Trending adds already rostered in this league: "
                  + "; ".join(f"{L.short(t['player_id'])} → {L.owner_name(L.rostered[t['player_id']])}"
                              for t in trending_rostered)]

    # Remainder: fill the cap evenly across positions, ordered by Sleeper search_rank.
    remaining_slots = max(FA_CAP - len(trending_fa), 0)
    trending_ids = {t["player_id"] for t in trending_fa}
    by_pos: dict[str, list[dict]] = {pos: [] for pos in FANTASY_POSITIONS}
    for pid, p in free_agents.items():
        if pid not in trending_ids:
            by_pos[p["position"]].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: (p["search_rank"], p["full_name"]))
    chosen: dict[str, list[dict]] = {pos: [] for pos in FANTASY_POSITIONS}
    picked = 0
    while picked < remaining_slots:
        progressed = False
        for pos in FANTASY_POSITIONS:
            if picked >= remaining_slots:
                break
            idx = len(chosen[pos])
            if idx < len(by_pos[pos]):
                chosen[pos].append(by_pos[pos][idx])
                picked += 1
                progressed = True
        if not progressed:
            break

    lines += ["", f"### Other available players by position — {picked} shown of {len(free_agents) - len(trending_fa)} eligible", ""]
    for pos in FANTASY_POSITIONS:
        if not chosen[pos]:
            continue
        lines.append(f"**{pos}:** " + "; ".join(
            f"{p['full_name']} ({p.get('team') or 'FA'}"
            + (f", {p['injury_status']}" if p.get("injury_status") else "")
            + (f", {note(p)}" if note(p) else "")
            + ")"
            for p in chosen[pos]
        ))

    drops = d.trending_drop[:TRENDING_DROP_SHOWN]
    if drops:
        lines += ["", f"### Trending drops (Sleeper-wide, last 24h, top {len(drops)})", ""]
        for t in drops:
            pid = t["player_id"]
            where = f"rostered here by {L.owner_name(L.rostered[pid])}" if pid in L.rostered else "free agent here"
            lines.append(f"- {L.short(pid)} — {t.get('count', 0):,} drops; {where}")
    return "\n".join(lines)


def _describe_transaction(L: _League, week: int, tx: dict) -> str:
    kind = tx.get("type", "?")
    status = tx.get("status", "?")
    adds = tx.get("adds") or {}
    drops = tx.get("drops") or {}
    settings = tx.get("settings") or {}
    when = _ts(tx.get("created"))
    prefix = f"- **Wk {week} · {when} · {kind}**"

    if kind == "trade":
        parts = []
        for rid in tx.get("roster_ids") or []:
            got = [L.short(pid) for pid, to in adds.items() if to == rid]
            picks = [
                f"{pk.get('season')} R{pk.get('round')} pick"
                for pk in (tx.get("draft_picks") or []) if pk.get("owner_id") == rid
            ]
            faab = [f"${wb.get('amount')} FAAB" for wb in (tx.get("waiver_budget") or []) if wb.get("receiver") == rid]
            received = ", ".join(got + picks + faab) or "nothing"
            parts.append(f"{L.owner_name(rid)} receives {received}")
        tail = "" if status == "complete" else f" [{status}]"
        return f"{prefix}: " + " | ".join(parts) + tail

    who = L.owner_name((tx.get("roster_ids") or [None])[0])
    added = ", ".join(L.short(pid) for pid in adds)
    dropped = ", ".join(L.short(pid) for pid in drops)
    bid = settings.get("waiver_bid")
    bits = []
    if added:
        bits.append(f"added {added}" + (f" for ${bid} FAAB" if bid is not None else ""))
    if dropped:
        bits.append(f"dropped {dropped}")
    body = "; ".join(bits) or "no players moved"
    tail = "" if status == "complete" else f" [{status} claim]"
    return f"{prefix} {who}: {body}{tail}"


def _section_activity(L: _League) -> str:
    d = L.d
    weeks = sorted({w for w, _ in d.transactions}, reverse=True)
    lines = ["## 5. Recent league activity", ""]
    lines.append(f"Transactions for weeks {', '.join(str(w) for w in weeks) or d.week}. "
                 "Failed waiver claims are included (they show what the room bid on).")
    lines.append("")
    txs = sorted(d.transactions, key=lambda wt: wt[1].get("created") or 0, reverse=True)
    shown = 0
    for week, tx in txs:
        if tx.get("status") not in ("complete", "failed"):
            continue
        lines.append(_describe_transaction(L, week, tx))
        shown += 1
        if shown >= ACTIVITY_CAP:
            lines.append(f"- … {len(txs) - shown} more not shown")
            break
    if shown == 0:
        lines.append("- No transactions in this window.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- entry point

def build_bundle(data: WeekData) -> str:
    L = _League(data)
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = [
        f"# Fantasy report — {data.league.get('name', data.config.league_id)} — Week {data.week}, {data.season}",
        "",
        f"_Generated {generated} from Sleeper data._",
    ]
    if data.notes:
        header += [""] + [f"> {n}" for n in data.notes]
    sections = [
        "\n".join(header),
        _section_league_context(L),
        _section_my_roster(L),
        _section_matchup(L),
        _section_waivers(L),
        _section_activity(L),
    ]
    return "\n\n".join(sections) + "\n"
