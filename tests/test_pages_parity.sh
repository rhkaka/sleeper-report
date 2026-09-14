#!/usr/bin/env bash
# The hosted (docs/) version ports bundle.py to JavaScript. This checks the two produce
# identical markdown for the synthetic league in tests/test_bundle.py. Needs node + uv.
set -eu
cd "$(dirname "$0")/.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
uv run python - "$tmp" <<'PY'
import dataclasses, json, sys
sys.path.insert(0, "tests")
from test_bundle import make_week
from sleeper_report.bundle import build_bundle
d = make_week(); out = dataclasses.asdict(d); out["bye_teams"] = sorted(d.bye_teams)
json.dump(out, open(sys.argv[1] + "/fixture.json", "w"))
open(sys.argv[1] + "/py.md", "w").write(build_bundle(d))
PY
node tests/pages_parity.js "$tmp/fixture.json" > "$tmp/js.md"
strip() { sed 's/^_Generated .* from Sleeper data\._$/_Generated X from Sleeper data._/' "$1"; }
if strip "$tmp/py.md" | diff - <(strip "$tmp/js.md") ; then
  echo "OK: docs/report.js matches bundle.py" >&2
else
  echo "FAIL: docs/report.js output differs from bundle.py" >&2; exit 1
fi
