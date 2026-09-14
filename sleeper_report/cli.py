"""CLI: `setup` (once), `report [--week N] [--bundle-only]`, and `serve` for the local web UI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analysis, pipeline
from .config import DEFAULT_CONFIG_PATH, Config
from .http import HTTPError


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- setup

def _pick_league(leagues: list[dict]) -> dict:
    if len(leagues) == 1:
        return leagues[0]
    print("\nMultiple leagues found:")
    for i, lg in enumerate(leagues, 1):
        print(f"  {i}. {lg['name']} ({lg['total_rosters']} teams, {lg.get('status', '?')})")
    while True:
        raw = input(f"Pick a league [1-{len(leagues)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(leagues):
            return leagues[int(raw) - 1]
        print("  invalid choice")


def cmd_setup(args: argparse.Namespace) -> int:
    username = args.username or input("Sleeper username: ").strip()
    info = pipeline.lookup_user(username, args.season)
    user, state = info["user"], info["state"]
    print(f"Found user {user['display_name']} (user_id {user['user_id']})")
    print(f"NFL state: season {state.get('season')}, week {state.get('week')}, leg {state.get('leg')}"
          + (f" (using season {info['season']})" if info["season"] != str(state.get("season")) else ""))
    league = _pick_league(info["leagues"])
    print(f"League: {league['name']} ({league['league_id']})")
    cfg = pipeline.finish_setup(user["user_id"], user["username"], league["league_id"], info["season"], args.config)
    print(f"Wrote {args.config}: roster_id {cfg.roster_id} in league {cfg.league_id}, season {cfg.season}")
    return 0


# --------------------------------------------------------------------------- report

def cmd_report(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    data_dir = args.config.resolve().parent
    out_dir: Path = args.out_dir or data_dir
    try:
        path, markdown = pipeline.generate(
            cfg, week=args.week, bundle_only=args.bundle_only, refresh_players=args.refresh_players,
            data_dir=data_dir, out_dir=out_dir, model=args.model, max_tokens=args.max_tokens,
        )
    except (analysis.AnalysisError, HTTPError) as exc:
        _err(f"analysis failed: {exc}")
        if args.week is not None:
            _err(f"the bundle is still available at {pipeline.report_path(out_dir, args.week)}")
        return 1
    sys.stdout.write(markdown)
    print(f"\nReport written to: {path}")
    return 0


# --------------------------------------------------------------------------- serve

def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve  # imported lazily so the CLI stays light

    data_dir = args.config.resolve().parent
    serve(host=args.host, port=args.port, config_path=args.config.resolve(),
          data_dir=data_dir, out_dir=args.out_dir or data_dir, open_browser=not args.no_browser)
    return 0


# --------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sleeper-report",
                                     description="Weekly fantasy football report for a Sleeper league.")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="path to config.json (default: ./config.json); caches live beside it")

    s = sub.add_parser("setup", parents=[common], help="one-time: resolve user, league, and roster ids into config.json")
    s.add_argument("--username", help="Sleeper username (prompted if omitted)")
    s.add_argument("--season", help="override season (default: current from /state/nfl)")
    s.set_defaults(func=cmd_setup)

    r = sub.add_parser("report", parents=[common], help="build this week's bundle and (by default) get recommendations")
    r.add_argument("--week", type=int, help="week number (default: current week from /state/nfl)")
    r.add_argument("--bundle-only", action="store_true", help="stop after writing the markdown bundle")
    r.add_argument("--refresh-players", action="store_true", help="force re-download of the player database")
    r.add_argument("--out-dir", type=Path, help="where to write report-week{N}.md (default: beside config)")
    r.add_argument("--model", default=analysis.DEFAULT_MODEL, help=f"Anthropic model (default: {analysis.DEFAULT_MODEL})")
    r.add_argument("--max-tokens", type=int, default=analysis.DEFAULT_MAX_TOKENS)
    r.set_defaults(func=cmd_report)

    w = sub.add_parser("serve", parents=[common], help="run the local web UI")
    w.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 to reach it from your LAN)")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--out-dir", type=Path, help="where reports live (default: beside config)")
    w.add_argument("--no-browser", action="store_true", help="don't open the UI in a browser on start")
    w.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except pipeline.PipelineError as exc:
        _err(str(exc))
        return 1
    except HTTPError as exc:
        _err(str(exc))
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
