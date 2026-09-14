"""Smoke test for the web backend: serve fixture reports from a temp dir and hit the JSON API."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleeper_report import web  # noqa: E402
from test_bundle import make_week  # noqa: E402
from sleeper_report.bundle import build_bundle  # noqa: E402


def get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "report-week3.md").write_text(build_bundle(make_week()))
        (d / "report-week2.md").write_text(build_bundle(make_week()) + "\n## Recommendations\n\nStart everyone.\n")
        app = web.App(config_path=d / "config.json", data_dir=d, out_dir=d)
        app.nfl_state = lambda: {"season": "2026", "week": 3}  # no network in tests
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            code, body = get(base + "/")
            assert code == 200 and b"<title>Sleeper Report</title>" in body

            code, body = get(base + "/favicon.ico")
            assert code == 204

            code, body = get(base + "/api/status")
            st = json.loads(body)
            assert code == 200 and st["config"] is None and st["current_week"] == 3
            assert [r["week"] for r in st["reports"]] == [3, 2]
            assert st["reports"][1]["has_recommendations"] is True

            code, body = get(base + "/api/reports/2")
            rep = json.loads(body)
            assert code == 200 and rep["has_recommendations"] and "## 4. Waiver wire" in rep["markdown"]

            code, body = get(base + "/api/reports/2/raw")
            assert code == 200 and body.startswith(b"# Fantasy report")

            code, body = get(base + "/api/reports/9")
            assert code == 400 and b"no report for week 9" in body

            code, resp = post(base + "/api/report", {"week": 3})
            assert code == 400 and "run setup first" in resp["error"], resp

            code, resp = post(base + "/api/recommend", {"week": 9})
            assert code == 400 and "no bundle" in resp["error"]

            code, resp = post(base + "/api/setup/finish", {"username": "x"})
            assert code == 400 and "missing user_id" in resp["error"]

            code, body = get(base + "/api/jobs/nope")
            assert code == 404
        finally:
            httpd.shutdown()
    print("OK: web smoke test passed", file=sys.stderr)


if __name__ == "__main__":
    main()
