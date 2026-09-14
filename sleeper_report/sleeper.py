"""Sleeper API endpoints. Unauthenticated; Sleeper asks for < 1000 calls/min."""

from __future__ import annotations

from typing import Any

from .http import get_json

BASE = "https://api.sleeper.app/v1"
# Undocumented but stable; used only to derive bye weeks (player records carry no bye_week field).
SCHEDULE_BASE = "https://api.sleeper.app/schedule/nfl"


def nfl_state(**kwargs: Any) -> dict:
    """Extra kwargs (timeout, max_retries) are passed to the HTTP layer."""
    return get_json(f"{BASE}/state/nfl", **kwargs)


def user(username_or_id: str) -> dict | None:
    return get_json(f"{BASE}/user/{username_or_id}")


def user_leagues(user_id: str, season: str, sport: str = "nfl") -> list[dict]:
    return get_json(f"{BASE}/user/{user_id}/leagues/{sport}/{season}") or []


def league(league_id: str) -> dict:
    return get_json(f"{BASE}/league/{league_id}")


def rosters(league_id: str) -> list[dict]:
    return get_json(f"{BASE}/league/{league_id}/rosters") or []


def users(league_id: str) -> list[dict]:
    return get_json(f"{BASE}/league/{league_id}/users") or []


def matchups(league_id: str, week: int) -> list[dict]:
    return get_json(f"{BASE}/league/{league_id}/matchups/{week}") or []


def transactions(league_id: str, week: int) -> list[dict]:
    if week < 1:
        return []
    return get_json(f"{BASE}/league/{league_id}/transactions/{week}") or []


def all_players(sport: str = "nfl") -> dict[str, dict]:
    """~10MB. Sleeper asks that this be fetched at most once per day — see players.load_players."""
    return get_json(f"{BASE}/players/{sport}", timeout=180.0)


def trending(kind: str, lookback_hours: int = 24, limit: int = 50) -> list[dict[str, Any]]:
    """kind is 'add' or 'drop'. Returns [{'player_id': ..., 'count': ...}, ...]."""
    return get_json(
        f"{BASE}/players/nfl/trending/{kind}",
        params={"lookback_hours": lookback_hours, "limit": limit},
    ) or []


def schedule(season: str, season_type: str = "regular") -> list[dict]:
    """Regular-season schedule: [{'week': 1, 'home': 'KC', 'away': 'DEN', ...}, ...]."""
    return get_json(f"{SCHEDULE_BASE}/{season_type}/{season}") or []
