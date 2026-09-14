"""Player database cache (24h mtime check) and bye-week derivation."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import sleeper

PLAYER_FIELDS = (
    "player_id", "full_name", "position", "team", "injury_status", "injury_body_part",
    "practice_participation", "status", "years_exp", "age", "number",
)
FANTASY_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
CACHE_MAX_AGE_SECONDS = 24 * 3600


def _is_fresh(path: Path, max_age: float) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age


def _write_atomic(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _trim(pid: str, rec: dict) -> dict:
    out = {k: rec.get(k) for k in PLAYER_FIELDS}
    out["player_id"] = pid
    if not out["full_name"]:
        # Team defenses have no full_name; build "Houston Texans" from first/last.
        out["full_name"] = " ".join(x for x in (rec.get("first_name"), rec.get("last_name")) if x) or pid
    # Sleeper's own relevance rank (lower = more relevant). Used only to order the
    # non-trending free-agent remainder so the cap keeps the useful names.
    out["search_rank"] = rec.get("search_rank") or 9_999_999
    out["active"] = bool(rec.get("active"))
    return out


def load_players(cache_path: Path, refresh: bool = False) -> dict[str, dict]:
    """Return {player_id: trimmed record}. Refetches only if cache is missing/older than 24h or refresh=True."""
    if refresh or not _is_fresh(cache_path, CACHE_MAX_AGE_SECONDS):
        why = "forced" if refresh else "cache missing or older than 24h"
        print(f"Fetching player database from Sleeper (~10MB, {why})...", file=sys.stderr)
        raw = sleeper.all_players()
        _write_atomic(cache_path, raw)
    else:
        raw = json.loads(cache_path.read_text())
    return {pid: _trim(pid, rec) for pid, rec in raw.items() if isinstance(rec, dict)}


def is_fantasy_relevant(p: dict) -> bool:
    if p.get("position") not in FANTASY_POSITIONS:
        return False
    if p["position"] == "DEF":
        return p.get("active", False)  # team defenses carry status=None
    return p.get("status") == "Active"


def team_codes(players: dict[str, dict]) -> set[str]:
    """The canonical 32 team codes are the DEF player_ids (e.g. 'KC', 'SF')."""
    return {p["player_id"] for p in players.values() if p.get("position") == "DEF" and p.get("active")}


def load_schedule(cache_path: Path, season: str, refresh: bool = False) -> list[dict] | None:
    """Best-effort regular-season schedule, cached 24h. Returns None if unavailable."""
    if not refresh and _is_fresh(cache_path, CACHE_MAX_AGE_SECONDS):
        try:
            return json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            pass
    try:
        games = sleeper.schedule(season)
    except Exception as exc:  # noqa: BLE001 - byes are a nice-to-have, never fatal
        print(f"warning: could not fetch schedule for bye weeks: {exc}", file=sys.stderr)
        return None
    if not isinstance(games, list) or not games:
        return None
    _write_atomic(cache_path, games)
    return games


def bye_teams(schedule: list[dict] | None, week: int, all_teams: set[str]) -> set[str] | None:
    """Teams with no game in `week`. None if the schedule is unavailable or has no games that week."""
    if not schedule:
        return None
    playing: set[str] = set()
    for game in schedule:
        if game.get("week") == week:
            playing.update(x for x in (game.get("home"), game.get("away")) if x)
    if not playing:
        return None
    return all_teams - playing
