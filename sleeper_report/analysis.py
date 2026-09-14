"""Send the bundle to the Anthropic Messages API and return the recommendation text."""

from __future__ import annotations

import os

from .http import post_json

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4000

SYSTEM_PROMPT = """You are advising on a 12-team full-PPR Sleeper league. The manager's
stated preference is consistent weekly floor over boom-bust ceiling —
weight that heavily and say so explicitly when it changes a call.
Do not recommend a player you cannot name a concrete reason for.

Produce exactly three sections:

START/SIT — the optimal lineup for the given roster slots, then a short
list of the genuinely close calls with the reasoning for each. Do not
re-justify obvious starts.

WAIVERS — ranked pickups with a FAAB bid as a percentage of the
manager's REMAINING budget, and the specific drop candidate for each.
If nothing is worth a bid this week, say that plainly instead of
manufacturing a recommendation.

WATCH — players to monitor but not claim yet, with the trigger that
would change that (snap share, a starter's injury designation, a bye
coming up).

You do not have projections. Reason from role, snap share, target
volume, matchup, and injury designations. Never invent a stat line or
a projected point total. If you're uncertain, say so."""


class AnalysisError(RuntimeError):
    pass


def recommend(bundle_markdown: str, *, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AnalysisError("ANTHROPIC_API_KEY is not set (use --bundle-only to skip the analysis step).")

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": bundle_markdown}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    resp = post_json(ANTHROPIC_URL, json=body, headers=headers, timeout=300.0, max_retries=3)

    text = "\n".join(block.get("text", "") for block in resp.get("content", []) if block.get("type") == "text").strip()
    stop = resp.get("stop_reason")
    if stop == "refusal":
        raise AnalysisError(f"model refused the request: {resp.get('stop_details')}")
    if not text:
        raise AnalysisError(f"empty response from model (stop_reason={stop})")
    if stop == "max_tokens":
        text += f"\n\n_(Response was cut off at max_tokens={max_tokens}.)_"
    return text
