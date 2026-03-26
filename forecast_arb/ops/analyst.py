"""
forecast_arb.ops.analyst
========================
Optional LLM analyst layer — OpenAI-backed structured recommendation.

Receives the rendered operator summary markdown and the decision packet,
calls OpenAI to produce a structured advisory recommendation, and returns
a typed result dict. Never raises; all errors are surfaced in the result.

Public API
----------
    run_analyst(packet, summary_md) -> dict

Result schema
-------------
    {
        "status":         "ok" | "error",
        "recommendation": "EXECUTE" | "SKIP" | "REVIEW" | null,
        "confidence":     float | null,       # 0.0–1.0
        "rationale":      str,
        "flags":          list[str],
        "raw_response":   str,
        "error":          str | null,
        "ts_utc":         str,
    }

Configuration
-------------
    OPENAI_API_KEY  environment variable (required at call time)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import openai

log = logging.getLogger(__name__)

ANALYST_MODEL = "gpt-4o-mini"
SUPPORTED_SCHEMA_VERSION = "2.0"

_VALID_RECOMMENDATIONS = {"EXECUTE", "SKIP", "REVIEW"}

_SYSTEM_PROMPT = """\
You are a quantitative trading analyst reviewing a daily options-arbitrage run summary.
Respond in JSON only, with no markdown fencing. Your JSON must have exactly these keys:
  "recommendation": one of "EXECUTE", "SKIP", or "REVIEW"
  "confidence": a float between 0.0 and 1.0
  "rationale": a concise string (1-3 sentences) explaining your recommendation
  "flags": a list of short strings naming any specific concerns or highlights

Use "EXECUTE" if the run looks clean and the edge is well-supported.
Use "SKIP" if the edge is absent, preflight is blocked, or key signals are missing.
Use "REVIEW" if the situation is ambiguous and warrants human attention before acting.
"""


def _empty_result(ts_utc: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "recommendation": None,
        "confidence": None,
        "rationale": "",
        "flags": [],
        "raw_response": "",
        "error": None,
        "ts_utc": ts_utc,
    }


def run_analyst(
    packet: Dict[str, Any],
    summary_md: str,
) -> Dict[str, Any]:
    """
    Call OpenAI to analyze the decision packet and return a structured
    advisory recommendation. Never raises.

    Args:
        packet:     Decision packet dict (schema_version "2.0").
        summary_md: Rendered operator summary markdown (from render_operator_summary).

    Returns:
        Analyst result dict (see module docstring for full schema).
    """
    ts_utc = datetime.now(timezone.utc).isoformat()
    result = _empty_result(ts_utc)

    # ------------------------------------------------------------------
    # Guard: schema version
    # ------------------------------------------------------------------
    schema = packet.get("schema_version", "MISSING")
    if schema != SUPPORTED_SCHEMA_VERSION:
        result["error"] = (
            f"run_analyst: unsupported schema_version={schema!r}. "
            f"Expected {SUPPORTED_SCHEMA_VERSION!r}."
        )
        log.warning(result["error"])
        return result

    # ------------------------------------------------------------------
    # Guard: API key
    # ------------------------------------------------------------------
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        result["error"] = "OPENAI_API_KEY environment variable not set"
        log.warning(result["error"])
        return result

    # ------------------------------------------------------------------
    # API call
    # ------------------------------------------------------------------
    raw_response = ""
    try:
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=ANALYST_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": summary_md},
            ],
        )
        raw_response = response.choices[0].message.content or ""
    except Exception as exc:
        result["error"] = f"OpenAI API error: {exc}"
        result["raw_response"] = raw_response
        log.warning(result["error"])
        return result

    # ------------------------------------------------------------------
    # Parse structured output
    # ------------------------------------------------------------------
    try:
        parsed = json.loads(raw_response)
    except Exception as exc:
        result["error"] = f"JSON parse error: {exc}"
        result["raw_response"] = raw_response
        log.warning(result["error"])
        return result

    # Extract and normalise fields with safe defaults
    recommendation = parsed.get("recommendation")
    if recommendation not in _VALID_RECOMMENDATIONS:
        recommendation = None

    raw_confidence = parsed.get("confidence")
    try:
        confidence: Optional[float] = float(raw_confidence) if raw_confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    rationale = str(parsed.get("rationale") or "")
    flags_raw = parsed.get("flags", [])
    flags: List[str] = [str(f) for f in flags_raw] if isinstance(flags_raw, list) else []

    result.update({
        "status": "ok",
        "recommendation": recommendation,
        "confidence": confidence,
        "rationale": rationale,
        "flags": flags,
        "raw_response": raw_response,
        "error": None,
        "ts_utc": ts_utc,
    })
    return result
