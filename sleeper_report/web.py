"""Local web UI: a stdlib http.server backend and a single-page frontend (static/index.html)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import analysis, pipeline, sleeper
from .config import Config
from .http import HTTPError

STATIC_DIR = Path(__file__).parent / "static"
STATE_TTL = 600  # seconds to cache /state/nfl


class Busy(RuntimeError):
    pass


class Job:
    def __init__(self, kind: str, label: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.label = label
        self.status = "running"
        self.log: list[str] = []
        self.result: dict | None = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "log": self.log, "result": self.result, "error": self.error,
            "started": self.started, "finished": self.finished,
        }


class App:
    def __init__(self, config_path: Path, data_dir: Path, out_dir: Path):
        self.config_path = config_path
        self.data_dir = data_dir
        self.out_dir = out_dir
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.lock = threading.Lock()
        self._state: tuple[float, dict | None] = (0.0, None)
        self._league_names: dict[str, str] = {}

    # ---- data

    def config(self) -> Config | None:
        if not self.config_path.exists():
            return None
        try:
            return Config.load(self.config_path)
        except SystemExit:
            return None

    def nfl_state(self) -> dict | None:
        ts, cached = self._state
        if cached is not None and time.time() - ts < STATE_TTL:
            return cached
        try:
            state = sleeper.nfl_state()
        except HTTPError:
            return cached
        self._state = (time.time(), state)
        return state

    def league_name(self, league_id: str) -> str | None:
        if league_id not in self._league_names:
            try:
                self._league_names[league_id] = (sleeper.league(league_id) or {}).get("name") or league_id
            except HTTPError:
                return None
        return self._league_names[league_id]

    def status(self) -> dict:
        cfg = self.config()
        state = self.nfl_state()
        return {
            "config": ({**cfg.__dict__, "league_name": self.league_name(cfg.league_id)} if cfg else None),
            "config_path": str(self.config_path),
            "state": state,
            "current_week": int(state.get("week") or state.get("leg") or 1) if state else None,
            "players_cache": pipeline.cache_status(self.data_dir),
            "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "model": analysis.DEFAULT_MODEL,
            "reports": pipeline.list_reports(self.out_dir),
            "current_job": self.current.to_dict() if self.current and self.current.status == "running" else None,
        }

    # ---- jobs

    def start_job(self, kind: str, label: str, fn: Callable[[Job], dict]) -> Job:
        with self.lock:
            if self.current and self.current.status == "running":
                raise Busy(f"a job is already running: {self.current.label}")
            job = Job(kind, label)
            self.jobs[job.id] = job
            self.current = job

        def run() -> None:
            try:
                job.result = fn(job)
                job.status = "done"
            except (pipeline.PipelineError, HTTPError, analysis.AnalysisError) as exc:
                job.error = str(exc)
                job.status = "error"
            except Exception as exc:  # noqa: BLE001 - surface unexpected failures in the UI
                job.error = f"{type(exc).__name__}: {exc}"
                job.log.append(traceback.format_exc())
                job.status = "error"
            finally:
                job.finished = time.time()

        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return job

    def start_report(self, week: int | None, bundle_only: bool, refresh_players: bool) -> Job:
        cfg = self.config()
        if cfg is None:
            raise pipeline.PipelineError("no config.json yet; run setup first")
        label = f"Week {week} report" if week else "Current week report"

        def fn(job: Job) -> dict:
            log = job.log.append
            try:
                path, _ = pipeline.generate(
                    cfg, week=week, bundle_only=bundle_only, refresh_players=refresh_players,
                    data_dir=self.data_dir, out_dir=self.out_dir, log=log,
                )
            except analysis.AnalysisError as exc:
                # The bundle was written; tell the UI which week so it can still open it.
                wk = week if week is not None else self._latest_week()
                raise analysis.AnalysisError(f"{exc} (bundle for week {wk} was still written)") from exc
            return {"week": int(path.stem.removeprefix("report-week")), "path": str(path)}

        return self.start_job("report", label, fn)

    def start_recommend(self, week: int) -> Job:
        path = pipeline.report_path(self.out_dir, week)
        if not path.exists():
            raise pipeline.PipelineError(f"no bundle for week {week}")

        def fn(job: Job) -> dict:
            pipeline.add_recommendations(path, log=job.log.append)
            return {"week": week, "path": str(path)}

        return self.start_job("recommend", f"Week {week} recommendations", fn)

    def _latest_week(self) -> int | None:
        reports = pipeline.list_reports(self.out_dir)
        return max((r["week"] for r in reports), default=None)

    def report(self, week: int) -> dict:
        path = pipeline.report_path(self.out_dir, week)
        if not path.exists():
            raise pipeline.PipelineError(f"no report for week {week}")
        text = path.read_text()
        return {
            "week": week, "markdown": text, "path": str(path),
            "has_recommendations": pipeline.RECOMMENDATIONS_HEADING in text,
            "modified": path.stat().st_mtime,
        }


# --------------------------------------------------------------------------- HTTP

def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "sleeper-report/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            if args and "/api/jobs/" in str(args[0]):
                return  # don't spam the terminal with 1s polling
            print(f"  {self.address_string()} {fmt % args}", file=sys.stderr)

        # ---- helpers

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                raise pipeline.PipelineError("request body is not valid JSON") from None
            return data if isinstance(data, dict) else {}

        def _route(self, method: str) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            parts = path.strip("/").split("/")
            try:
                if method == "GET":
                    self._get(path, parts)
                else:
                    self._post(path, parts)
            except pipeline.PipelineError as exc:
                self._json(400, {"error": str(exc)})
            except Busy as exc:
                self._json(409, {"error": str(exc)})
            except (HTTPError, analysis.AnalysisError) as exc:
                self._json(502, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        # ---- routes

        def _get(self, path: str, parts: list[str]) -> None:
            if path == "/":
                self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif path == "/api/status":
                self._json(200, app.status())
            elif path == "/api/reports":
                self._json(200, pipeline.list_reports(app.out_dir))
            elif parts[:2] == ["api", "reports"] and len(parts) >= 3 and parts[2].isdigit():
                rep = app.report(int(parts[2]))
                if len(parts) == 4 and parts[3] == "raw":
                    self._send(200, rep["markdown"].encode(), "text/markdown; charset=utf-8")
                else:
                    self._json(200, rep)
            elif parts[:2] == ["api", "jobs"] and len(parts) == 3:
                job = app.jobs.get(parts[2])
                self._json(200, job.to_dict()) if job else self._json(404, {"error": "no such job"})
            else:
                self._json(404, {"error": "not found"})

        def _post(self, path: str, parts: list[str]) -> None:
            body = self._body()
            if path == "/api/setup/lookup":
                self._json(200, pipeline.lookup_user(str(body.get("username", "")), body.get("season") or None))
            elif path == "/api/setup/finish":
                for key in ("user_id", "username", "league_id", "season"):
                    if not body.get(key):
                        raise pipeline.PipelineError(f"missing {key}")
                cfg = pipeline.finish_setup(str(body["user_id"]), str(body["username"]),
                                            str(body["league_id"]), str(body["season"]), app.config_path)
                self._json(200, {"config": cfg.__dict__})
            elif path == "/api/report":
                week = body.get("week")
                week = int(week) if week not in (None, "", 0) else None
                job = app.start_report(week, bool(body.get("bundle_only")), bool(body.get("refresh_players")))
                self._json(202, job.to_dict())
            elif path == "/api/recommend":
                week = body.get("week")
                if not isinstance(week, int):
                    raise pipeline.PipelineError("week is required")
                self._json(202, app.start_recommend(week).to_dict())
            else:
                self._json(404, {"error": "not found"})

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(200 if urlparse(self.path).path in ("/", "/api/status") else 404)
            self.end_headers()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Allow", "GET, POST, HEAD, OPTIONS")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

    return Handler


def serve(*, host: str, port: int, config_path: Path, data_dir: Path, out_dir: Path, open_browser: bool = True) -> None:
    app = App(config_path=config_path, data_dir=data_dir, out_dir=out_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}/"
    print(f"sleeper-report UI: {url}  (config: {config_path}, reports: {out_dir})", file=sys.stderr)
    if host == "0.0.0.0":
        print("  bound to all interfaces; reachable from other devices on your network", file=sys.stderr)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  ANTHROPIC_API_KEY is not set: reports will be bundle-only until you restart with it", file=sys.stderr)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
    finally:
        httpd.server_close()
