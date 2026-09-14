"""Shared pipeline used by both the CLI and the web UI: setup, weekly fetch, report generation."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Callable

from . import analysis, players, sleeper
from .bundle import WeekData, build_bundle
from .config import Config

Log = Callable[[str], None]
REPORT_RE = re.compile(r"^report-week(\d+)\.md$")
RECOMMENDATIONS_HEADING = "## Recommendations"


def stderr_log(msg: str) -> None:
    print(msg, file=sys.stderr)


class PipelineError(RuntimeError):
    """A user-facing failure (bad username, no leagues, missing config, ...)."""


# --------------------------------------------------------------------------- setup

def lookup_user(username: str, season: str | None = None) -> dict:
    """Resolve a Sleeper username to a user, the NFL state, and that season's leagues."""
    username = username.strip()
    if not username:
        raise PipelineError("username is required")
    user = sleeper.user(username)
    if not user or not user.get("user_id"):
        raise PipelineError(f"Sleeper user '{username}' not found")
    state = sleeper.nfl_state()
    season = season or str(state["season"])
    leagues = sleeper.user_leagues(str(user["user_id"]), season)
    if not leagues:
        raise PipelineError(f"no NFL leagues found for {username} in {season}")
    return {
        "user": {"user_id": str(user["user_id"]), "username": username,
                 "display_name": user.get("display_name") or username, "avatar": user.get("avatar")},
        "state": state,
        "season": season,
        "leagues": [
            {"league_id": str(lg["league_id"]), "name": lg.get("name"), "total_rosters": lg.get("total_rosters"),
             "status": lg.get("status"), "avatar": lg.get("avatar")}
            for lg in leagues
        ],
    }


def finish_setup(user_id: str, username: str, league_id: str, season: str, config_path: Path) -> Config:
    """Find the roster owned by user_id in league_id and write config.json."""
    rosters = sleeper.rosters(league_id)
    mine = next((r for r in rosters if str(r.get("owner_id")) == user_id), None)
    if mine is None:
        mine = next((r for r in rosters if user_id in [str(x) for x in (r.get("co_owners") or [])]), None)
    if mine is None:
        raise PipelineError(f"no roster owned by user_id {user_id} in league {league_id}")
    cfg = Config(user_id=user_id, username=username, league_id=league_id,
                 roster_id=int(mine["roster_id"]), season=season)
    cfg.save(config_path)
    return cfg


# --------------------------------------------------------------------------- weekly report

def fetch_week(cfg: Config, week: int | None, refresh_players: bool, data_dir: Path, log: Log = stderr_log) -> WeekData:
    notes: list[str] = []
    state = sleeper.nfl_state()
    season = cfg.season
    if week is None:
        week = int(state.get("week") or state.get("leg") or 1)
        if str(state.get("season")) != season:
            notes.append(f"config season is {season} but Sleeper's current season is {state.get('season')}; "
                         f"pass --week explicitly for past seasons.")
    log(f"Season {season}, week {week}")

    log("Loading player database" + (" (forced refresh)" if refresh_players else ""))
    player_db = players.load_players(data_dir / "players.json", refresh=refresh_players)
    log(f"Player database: {len(player_db):,} players")

    log("Fetching league, rosters, users, matchups")
    league = sleeper.league(cfg.league_id)
    rosters = sleeper.rosters(cfg.league_id)
    users = sleeper.users(cfg.league_id)
    matchups = sleeper.matchups(cfg.league_id, week)
    log("Fetching transactions and trending players")
    transactions: list[tuple[int, dict]] = []
    for w in (week, week - 1):
        if w >= 1:
            transactions += [(w, tx) for tx in sleeper.transactions(cfg.league_id, w)]
    trending_add = sleeper.trending("add", lookback_hours=24, limit=50)
    trending_drop = sleeper.trending("drop", lookback_hours=24, limit=25)

    schedule = players.load_schedule(data_dir / f"schedule-{season}.json", season, refresh=refresh_players)
    byes = players.bye_teams(schedule, week, players.team_codes(player_db))
    if byes is None:
        notes.append("Bye weeks could not be determined (schedule endpoint unavailable).")

    return WeekData(
        config=cfg, season=season, week=week, league=league, rosters=rosters, users=users,
        matchups=matchups, transactions=transactions, trending_add=trending_add,
        trending_drop=trending_drop, players=player_db, bye_teams=byes, notes=notes,
    )


def report_path(out_dir: Path, week: int) -> Path:
    return out_dir / f"report-week{week}.md"


def generate(
    cfg: Config,
    *,
    week: int | None,
    bundle_only: bool,
    refresh_players: bool,
    data_dir: Path,
    out_dir: Path,
    model: str = analysis.DEFAULT_MODEL,
    max_tokens: int = analysis.DEFAULT_MAX_TOKENS,
    log: Log = stderr_log,
) -> tuple[Path, str]:
    """Build the bundle (and recommendations unless bundle_only). Returns (path, markdown).

    If the recommendation step fails the bundle is still written; the error is re-raised.
    """
    data = fetch_week(cfg, week, refresh_players, data_dir, log)
    markdown = build_bundle(data)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = report_path(out_dir, data.week)
    path.write_text(markdown)
    log(f"Wrote bundle: {path}")
    if not bundle_only:
        markdown = add_recommendations(path, model=model, max_tokens=max_tokens, log=log)
    return path, markdown


def add_recommendations(path: Path, *, model: str = analysis.DEFAULT_MODEL,
                        max_tokens: int = analysis.DEFAULT_MAX_TOKENS, log: Log = stderr_log) -> str:
    """Append (or replace) the Recommendations section of an existing bundle file."""
    markdown = path.read_text()
    if RECOMMENDATIONS_HEADING in markdown:
        markdown = markdown.split(RECOMMENDATIONS_HEADING)[0].rstrip() + "\n"
    log(f"Requesting recommendations from {model}...")
    rec = analysis.recommend(markdown, model=model, max_tokens=max_tokens)
    markdown += f"\n{RECOMMENDATIONS_HEADING}\n\n" + rec.strip() + "\n"
    path.write_text(markdown)
    log("Recommendations appended")
    return markdown


def list_reports(out_dir: Path) -> list[dict]:
    out: list[dict] = []
    if not out_dir.exists():
        return out
    for p in out_dir.iterdir():
        m = REPORT_RE.match(p.name)
        if not m:
            continue
        text = p.read_text()
        out.append({
            "week": int(m.group(1)),
            "path": str(p),
            "has_recommendations": RECOMMENDATIONS_HEADING in text,
            "modified": p.stat().st_mtime,
        })
    return sorted(out, key=lambda r: r["week"], reverse=True)


def cache_status(data_dir: Path) -> dict:
    p = data_dir / "players.json"
    if not p.exists():
        return {"exists": False, "age_hours": None, "stale": True}
    age = (time.time() - p.stat().st_mtime) / 3600
    return {"exists": True, "age_hours": round(age, 1), "stale": age >= players.CACHE_MAX_AGE_SECONDS / 3600}
