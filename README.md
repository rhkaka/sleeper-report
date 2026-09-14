# sleeper-report

Weekly fantasy football report for a Sleeper league. Pulls your league, roster,
matchup, waiver wire, and recent transactions from the public Sleeper API,
writes them as one markdown bundle, and (optionally) asks Claude for
start/sit, waiver, and watch-list recommendations.

Python 3.11+, [uv](https://docs.astral.sh/uv/) for dependencies, `httpx` + stdlib only.

## Setup (one time)

```bash
brew install uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
cd sleeper-report
make install             # uv sync — creates .venv and downloads Python if needed
make setup               # prompts for your Sleeper username
```

`setup` resolves your `user_id`, finds your leagues for the current season
(from `/state/nfl`), lets you pick one if you have several, finds the roster
you own, and writes everything to `config.json`:

```json
{ "user_id": "...", "username": "...", "league_id": "...", "roster_id": 4, "season": "2026" }
```

Nothing is hardcoded; every later run reads `config.json`. Re-run `setup` each
new season (or after joining a different league). `--username` and `--season`
can be passed instead of being prompted.

For the recommendations step, export your Anthropic key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Hosted version (GitHub Pages)

**https://rhkaka.github.io/sleeper-report/** — no install, runs entirely in
your browser. The `docs/` folder is a JavaScript port of the same report
builder (`docs/report.js` is checked against `bundle.py` by `make test`):

- Sleeper's API allows cross-origin reads, so the page fetches your league
  directly. The player database is cached in IndexedDB for 24 hours.
- Your league config and generated reports are saved in that browser's
  localStorage. Nothing is uploaded anywhere.
- For the Claude step, paste an Anthropic API key under **Settings**. It is
  stored only in that browser and sent only to api.anthropic.com. Leave it
  blank for bundle-only mode. Don't do this on a shared computer.

`make pages` serves the same files locally on port 8790.

## Local web UI

```bash
make ui                  # http://127.0.0.1:8765, opens in your browser
make ui-lan              # bind 0.0.0.0 so your phone can reach it on the same Wi-Fi
```

The UI is served by the tool itself (stdlib `http.server`, no extra
dependencies) and covers the whole workflow:

- **Setup wizard** — if there's no `config.json` yet, enter your Sleeper
  username, pick a league, done. The "change" link in the header re-runs it.
- **Generate** — pick a week (or leave blank for the current one), optionally
  bundle-only or a forced player refresh, and watch the progress log. Only one
  job runs at a time.
- **Reports** — every `report-week{N}.md` in the data directory, rendered with
  a section jump bar, injury/bye/starter highlighting, and the recommendations
  in a callout. "Get recommendations" sends an existing bundle to Claude
  without regenerating it; "Copy markdown" and "Download" give you the raw file.

Start the server with `ANTHROPIC_API_KEY` exported if you want the analysis
step; the header shows whether it's set. `sleeper-report serve --help` lists
`--host`, `--port`, `--config`, `--out-dir`, and `--no-browser`.

## Weekly use (CLI)

```bash
make tuesday             # full report for the current week → report-week{N}.md
make bundle              # markdown only, no Anthropic call
make refresh             # force a fresh player-database download, then full report
```

Or call the CLI directly:

```bash
uv run sleeper-report report                  # current week, bundle + recommendations
uv run sleeper-report report --bundle-only    # stop after the markdown bundle
uv run sleeper-report report --week 3         # re-run a past week
uv run sleeper-report report --refresh-players
uv run sleeper-report report --model claude-sonnet-4-6 --max-tokens 4000
```

The report is written to `report-week{N}.md` next to `config.json` (override
with `--out-dir`), dumped to stdout, and the path is printed last.

### What's in the bundle

1. **League context** — scoring format (PPR/half/standard detected from `rec`),
   roster slots, waiver type, your FAAB remaining, your record and points
   for/against vs. the league median, teams on bye.
2. **My roster** — every player with position, team, injury status, practice
   participation, bye flag, and whether they're in your current starters.
3. **This week's matchup** — opponent, their record, and their roster with the
   same annotations.
4. **Waiver wire** — free agents (fantasy positions, active, unrostered in your
   league) ranked by Sleeper's 24h trending-add counts, then a position-sorted
   remainder (capped at ~40 total). Anyone dropped in a recent transaction is
   marked. Trending drops are listed with who rosters them.
5. **Recent league activity** — the last two weeks of adds, drops, trades,
   and FAAB bids, including failed claims so you can see what the room bid on.

With the default (non-`--bundle-only`) mode, the bundle is sent to the
Anthropic Messages API (`claude-sonnet-4-6`, `max_tokens` 4000) and the
response is appended under `## Recommendations`. If the call fails, the bundle
is still on disk.

## Caches and files

| File | What | Refresh |
|---|---|---|
| `config.json` | league identity from `setup` | re-run `setup` |
| `players.json` | Sleeper's full player DB (~14 MB) | automatically if older than 24h, or `--refresh-players` |
| `schedule-{season}.json` | regular-season schedule, used only to derive bye weeks | same as above |
| `report-week{N}.md` | the output | every run |

Sleeper asks that the player DB be fetched at most once a day; the mtime check
enforces that. All Sleeper calls retry with exponential backoff on 429/5xx and
connection errors.

### Bye weeks

Sleeper's player records do not include a bye week, so byes are derived from
the league schedule endpoint: a team with no game in week N is on bye. If that
endpoint is unavailable the report says so and simply omits bye flags.

## Sample Makefile target

```make
tuesday:
	uv run sleeper-report report
```

## Notes

- **macOS + iCloud-synced folders (e.g. `~/Desktop`):** files uv writes into
  `.venv` there get the macOS *hidden* flag, and Python 3.13+ silently skips
  hidden `.pth` files, which breaks the default editable install
  (`ModuleNotFoundError: sleeper_report`). The Makefile sets
  `UV_NO_EDITABLE=1` so the package is copied instead. If you call `uv`
  directly, do the same: `UV_NO_EDITABLE=1 uv run sleeper-report ...`, or
  move the project out of an iCloud-synced folder.
- Tests (offline, no network): `make test` runs the bundle-builder fixture test and a
  web-server smoke test.
