"""Offline test: build a bundle from a synthetic 12-team league and check every section renders."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleeper_report.bundle import WeekData, build_bundle  # noqa: E402
from sleeper_report.config import Config  # noqa: E402
from sleeper_report.players import bye_teams, is_fantasy_relevant  # noqa: E402

TEAMS = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB",
         "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
         "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS"]


def _player(pid, name, pos, team, **kw):
    p = {"player_id": pid, "full_name": name, "position": pos, "team": team, "injury_status": None,
         "injury_body_part": None, "practice_participation": None, "status": "Active",
         "years_exp": 3, "age": 26, "number": 10, "search_rank": int(pid) if pid.isdigit() else 9_999_999,
         "active": True}
    p.update(kw)
    return p


def make_players():
    players = {}
    n = 1
    for pos, per_team in (("QB", 2), ("RB", 3), ("WR", 4), ("TE", 2), ("K", 1)):
        for team in TEAMS:
            for i in range(per_team):
                pid = str(n); n += 1
                players[pid] = _player(pid, f"{pos}{i + 1} {team}", pos, team)
    for team in TEAMS:
        players[team] = _player(team, f"{team} Defense", "DEF", team, status=None)
    # a few annotated players
    players["1"]["injury_status"] = "Questionable"
    players["1"]["injury_body_part"] = "Ankle"
    players["1"]["practice_participation"] = "Limited"
    players["70"]["injury_status"] = "Out"
    players["9999"] = _player("9999", "Retired Guy", "RB", None, status="Inactive")
    return players


def make_week():
    players = make_players()
    # Interleave positions so every roster looks like a real one (QB, RB, WR, TE, K, DEF, QB, ...).
    by_position = {}
    for pid, p in players.items():
        if is_fantasy_relevant(p):
            by_position.setdefault(p["position"], []).append(pid)
    pool = []
    while any(by_position.values()):
        for pos in ("QB", "RB", "WR", "WR", "RB", "TE", "K", "DEF"):
            if by_position.get(pos):
                pool.append(by_position[pos].pop(0))
    roster_positions = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN", "BN", "BN", "BN", "BN"]
    rosters, users = [], []
    for rid in range(1, 13):
        uid = f"u{rid}"
        users.append({"user_id": uid, "display_name": f"Manager{rid}", "metadata": {"team_name": f"Team {rid}"}})
        # deterministic: slice the pool so every roster gets 15 distinct players
        mine = pool[(rid - 1) * 15:(rid - 1) * 15 + 15]
        by_pos = {}
        for pid in mine:
            by_pos.setdefault(players[pid]["position"], []).append(pid)
        starters = []
        for slot in roster_positions:
            if slot == "BN":
                continue
            want = ["RB", "WR", "TE"] if slot == "FLEX" else [slot]
            pick = next((by_pos[w].pop(0) for w in want if by_pos.get(w)), "0")
            starters.append(pick)
        rosters.append({
            "roster_id": rid, "owner_id": uid, "players": mine, "starters": starters, "reserve": [],
            "settings": {"wins": rid % 3, "losses": 2 - rid % 3, "ties": 0, "fpts": 100 + rid * 7,
                         "fpts_decimal": 25, "fpts_against": 110, "fpts_against_decimal": 0,
                         "waiver_budget_used": rid * 3},
        })
    # make sure "my" roster has a bye player and a QB starter
    matchups = []
    for rid in range(1, 13):
        matchups.append({"roster_id": rid, "matchup_id": (rid + 1) // 2, "points": 0})
    fa_ids = pool[12 * 15:]
    transactions = [
        (3, {"type": "waiver", "status": "complete", "created": 1_700_000_000_000, "roster_ids": [2],
             "adds": {fa_ids[0]: 2}, "drops": {fa_ids[1]: 2}, "settings": {"waiver_bid": 17}}),
        (3, {"type": "waiver", "status": "failed", "created": 1_700_000_100_000, "roster_ids": [5],
             "adds": {fa_ids[0]: 5}, "drops": {}, "settings": {"waiver_bid": 9}}),
        (2, {"type": "free_agent", "status": "complete", "created": 1_699_000_000_000, "roster_ids": [4],
             "adds": {fa_ids[2]: 4}, "drops": {fa_ids[3]: 4}, "settings": None}),
        (2, {"type": "trade", "status": "complete", "created": 1_699_500_000_000, "roster_ids": [1, 6],
             "adds": {rosters[0]["players"][3]: 6, rosters[5]["players"][2]: 1}, "drops": {},
             "draft_picks": [{"season": "2027", "round": 2, "owner_id": 1, "previous_owner_id": 6}],
             "waiver_budget": [{"sender": 6, "receiver": 1, "amount": 10}]}),
    ]
    trending_add = [{"player_id": fa_ids[1], "count": 12345}, {"player_id": fa_ids[5], "count": 999},
                    {"player_id": rosters[3]["players"][0], "count": 500}]
    trending_drop = [{"player_id": fa_ids[3], "count": 4321}, {"player_id": rosters[0]["players"][1], "count": 100}]
    league = {
        "name": "Test League", "total_rosters": 12, "roster_positions": roster_positions,
        "scoring_settings": {"rec": 1.0, "pass_td": 4, "pass_yd": 0.04, "pass_int": -2, "rush_td": 6,
                             "rec_td": 6, "fum_lost": -2, "bonus_rec_te": 0.5},
        "settings": {"waiver_type": 2, "waiver_budget": 100, "playoff_week_start": 15},
    }
    # Week 3 schedule omits the first four teams, so ARI/ATL/BAL/BUF are on bye.
    schedule = [{"week": 3, "home": t, "away": t2} for t, t2 in zip(TEAMS[4::2], TEAMS[5::2])]
    byes = bye_teams(schedule, 3, set(TEAMS))
    cfg = Config(user_id="u1", username="tester", league_id="L1", roster_id=1, season="2026")
    return WeekData(config=cfg, season="2026", week=3, league=league, rosters=rosters, users=users,
                    matchups=matchups, transactions=transactions, trending_add=trending_add,
                    trending_drop=trending_drop, players=players, bye_teams=byes)


def test_bundle_renders():
    md = build_bundle(make_week())
    for heading in ("## 1. League context", "## 2. My roster", "## 3. This week's matchup",
                    "## 4. Waiver wire", "## 5. Recent league activity"):
        assert heading in md, heading
    assert "Full PPR" in md
    assert "$97 of $100 remaining" in md          # roster 1 used 3
    assert "Questionable (Ankle)" in md
    assert "**BYE**" in md                         # ARI..BUF have no game in week 3
    assert "Manager2 (Team 2): added" in md and "$17 FAAB" in md
    assert "[failed claim]" in md
    assert "receives" in md and "2027 R2 pick" in md and "$10 FAAB" in md
    assert "12,345" in md                          # trending count formatting
    assert "dropped wk 3 by Manager2" in md        # recently-dropped FA is flagged
    assert "Retired Guy" not in md                 # inactive players excluded from FA pool
    fa_section = md.split("## 4. Waiver wire")[1].split("## 5.")[0]
    assert "Trending adds already rostered" in fa_section
    return md


if __name__ == "__main__":
    out = test_bundle_renders()
    print(out)
    print("\nOK: all assertions passed", file=sys.stderr)
