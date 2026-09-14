"""config.json: the only place league identity lives. Nothing downstream hardcodes it."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.json")


@dataclass
class Config:
    user_id: str
    username: str
    league_id: str
    roster_id: int
    season: str

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise SystemExit(f"{path} not found. Run `sleeper-report setup` first.")
        data = json.loads(path.read_text())
        missing = [k for k in cls.__dataclass_fields__ if k not in data]
        if missing:
            raise SystemExit(f"{path} is missing {missing}. Re-run `sleeper-report setup`.")
        return cls(
            user_id=str(data["user_id"]),
            username=str(data["username"]),
            league_id=str(data["league_id"]),
            roster_id=int(data["roster_id"]),
            season=str(data["season"]),
        )

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
