"""Shared HTTP layer: one httpx client, JSON helpers, retry with backoff."""

from __future__ import annotations

import random
import sys
import time
from typing import Any

import httpx

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 529}
_client: httpx.Client | None = None


class HTTPError(RuntimeError):
    """Raised when a request fails after retries or with a non-retryable status."""


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "sleeper-report/0.1"},
        )
    return _client


def request_json(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: Any = None,
    headers: dict | None = None,
    timeout: float | None = None,
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Any:
    """Perform a request and decode JSON, retrying transient failures with exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        retry_after: str | None = None
        try:
            resp = client().request(
                method, url, params=params, json=json, headers=headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TransportError as exc:
            last_error = exc
        else:
            if resp.status_code < 400:
                return resp.json()
            message = f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code not in RETRY_STATUSES:
                raise HTTPError(message)
            last_error = HTTPError(message)
            retry_after = resp.headers.get("retry-after")

        if attempt == max_retries:
            break
        delay = min(max_delay, base_delay * (2**attempt)) + random.uniform(0, 0.5)
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        print(f"  retry {attempt + 1}/{max_retries} in {delay:.1f}s ({last_error})", file=sys.stderr)
        time.sleep(delay)

    raise HTTPError(f"giving up on {method} {url}: {last_error}")


def get_json(url: str, **kwargs: Any) -> Any:
    return request_json("GET", url, **kwargs)


def post_json(url: str, **kwargs: Any) -> Any:
    return request_json("POST", url, **kwargs)
