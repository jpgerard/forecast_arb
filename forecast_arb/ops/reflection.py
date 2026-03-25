"""
forecast_arb.ops.reflection
=============================
Weekly LLM reflection layer — OpenAI-backed structured analysis.

Advisory only. Never raises. Openai is imported lazily inside the function
body so that a missing package or unconfigured client returns a structured
error dict instead of breaking module import.

Public API
----------
    run_weekly_reflection(reflection_packet: dict) -> dict

Result schema
-------------
    {
        "status":         "ok" | "error",
        "period":         {"since": str, "until": str},
        "summary": {
            "headline":              str,
            "overall_assessment":    str,   # PERFORMING|UNDERPERFORMING|MIXED|INSUFFICIENT_DATA
            "evidence_strength":     str,   # STRONG|MODERATE|WEAK|INSUFFICIENT
            "n_runs_assessed":       int,
            "n_trades_assessed":     int,
        },
        "what_worked":     [{"observation", "evidence", "why", "confidence", "n_supporting"}],
        "what_failed":     [{"observation", "common_factors", "why_it_failed",
                             "confidence", "n_supporting"}],
        "calibration_assessment": {
            "overall":                    str,   # WELL_CALIBRATED|OVERFIT|UNDERFIT|UNCLEAR
            "edge_vs_outcome_narrative":  str,
            "rejection_pattern_narrative":str,
            "confidence":                 float,
            "caveats":                    list[str],
        },
        "market_regime_assessment": {
            "inferred_regime":       str,   # TRENDING_UP|TRENDING_DOWN|RANGING|VOLATILE|UNCLEAR
            "supporting_evidence":   str,
            "strategy_fit_narrative":str,
            "confidence":            float,
        },
        "parameter_suggestions": [{
            "parameter", "current_value", "suggested_value",
            "reasoning", "expected_effect",
            "overfit_risk",     # HIGH|MEDIUM|LOW
            "confidence",       # float 0.0-1.0
            "promotion_path",   # concrete verification step
        }],
        "open_questions":      list[str],
        "weak_evidence_flags": list[str],
        "raw_response":        str,
        "error":               str | null,
        "ts_utc":              str,
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

log = logging.getLogger(__name__)

REFLECTION_MODEL = "gpt-4o-mini"
SUPPORTED_SCHEMA_VERSION = "1.0"

_VALID_ASSESSMENTS = {"PERFORMING", "UNDERPERFORMING", "MIXED", "INSUFFICIENT_DATA"}
_VALID_EVIDENCE = {"STRONG", "MODERATE", "WEAK", "INSUFFICIENT"}
_VALID_CALIBRATION = {"WELL_CALIBRATED", "OVERFIT", "UNDERFIT", "UNCLEAR"}
_VALID_REGIMES = {"TRENDING_UP", "TRENDING_DOWN", "RANGING", "VOLATILE", "UNCLEAR"}
_VALID_OVERFIT_RISK = {"HIGH", "MEDIUM", "LOW"}

_SYSTEM_PROMPT = """\
You are a systematic quantitative trading analyst reviewing a weekly run-level summary \
for an options-arbitrage system.

Respond ONLY with valid JSON matching the exact schema below — no markdown fencing, no extra keys.

EVIDENCE-STRENGTH RULES (strictly enforced):
- summary.evidence_strength = "STRONG" only if n_runs_assessed >= 15
- summary.evidence_strength = "MODERATE" if n_runs_assessed 8-14
- summary.evidence_strength = "WEAK" if n_runs_assessed 3-7
- summary.evidence_strength = "INSUFFICIENT" if n_runs_assessed < 3
- Any conclusion drawn from fewer than 3 data points MUST appear in weak_evidence_flags.

PARAMETER SUGGESTIONS:
- Only reference parameter names that appear in active_parameters in the input.
- If active_parameters is empty, parameter_suggestions MUST be [].
- All suggestions are hypotheses only; promotion_path must describe a concrete \
verification step before any change is applied.
- Use low confidence values (< 0.4) when evidence is sparse.

REQUIRED JSON SCHEMA:
{
  "summary": {
    "headline": <str>,
    "overall_assessment": <"PERFORMING"|"UNDERPERFORMING"|"MIXED"|"INSUFFICIENT_DATA">,
    "evidence_strength": <"STRONG"|"MODERATE"|"WEAK"|"INSUFFICIENT">,
    "n_runs_assessed": <int>,
    "n_trades_assessed": <int>
  },
  "what_worked": [{"observation":<str>,"evidence":<str>,"why":<str>,"confidence":<float>,"n_supporting":<int>}],
  "what_failed": [{"observation":<str>,"common_factors":[<str>],"why_it_failed":<str>,"confidence":<float>,"n_supporting":<int>}],
  "calibration_assessment": {
    "overall": <"WELL_CALIBRATED"|"OVERFIT"|"UNDERFIT"|"UNCLEAR">,
    "edge_vs_outcome_narrative": <str>,
    "rejection_pattern_narrative": <str>,
    "confidence": <float>,
    "caveats": [<str>]
  },
  "market_regime_assessment": {
    "inferred_regime": <"TRENDING_UP"|"TRENDING_DOWN"|"RANGING"|"VOLATILE"|"UNCLEAR">,
    "supporting_evidence": <str>,
    "strategy_fit_narrative": <str>,
    "confidence": <float>
  },
  "parameter_suggestions": [{
    "parameter":<str>,"current_value":<any>,"suggested_value":<any>,
    "reasoning":<str>,"expected_effect":<str>,
    "overfit_risk":<"HIGH"|"MEDIUM"|"LOW">,"confidence":<float>,"promotion_path":<str>
  }],
  "open_questions": [<str>],
  "weak_evidence_flags": [<str>]
}
"""


def _empty_result(ts_utc: str, period: dict) -> Dict[str, Any]:
    return {
        "status": "error",
        "period": period,
        "summary": {
            "headline": "",
            "overall_assessment": "INSUFFICIENT_DATA",
            "evidence_strength": "INSUFFICIENT",
            "n_runs_assessed": 0,
            "n_trades_assessed": 0,
        },
        "what_worked": [],
        "what_failed": [],
        "calibration_assessment": {
            "overall": "UNCLEAR",
            "edge_vs_outcome_narrative": "",
            "rejection_pattern_narrative": "",
            "confidence": 0.0,
            "caveats": [],
        },
        "market_regime_assessment": {
            "inferred_regime": "UNCLEAR",
            "supporting_evidence": "",
            "strategy_fit_narrative": "",
            "confidence": 0.0,
        },
        "parameter_suggestions": [],
        "open_questions": [],
        "weak_evidence_flags": [],
        "raw_response": "",
        "error": None,
        "ts_utc": ts_utc,
    }


def _coerce_str(val: Any, valid: set, fallback: str) -> str:
    s = str(val) if val is not None else ""
    return s if s in valid else fallback


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_summary(raw: dict) -> dict:
    s = raw.get("summary") or {}
    return {
        "headline": str(s.get("headline") or ""),
        "overall_assessment": _coerce_str(s.get("overall_assessment"), _VALID_ASSESSMENTS, "INSUFFICIENT_DATA"),
        "evidence_strength": _coerce_str(s.get("evidence_strength"), _VALID_EVIDENCE, "INSUFFICIENT"),
        "n_runs_assessed": int(s.get("n_runs_assessed") or 0),
        "n_trades_assessed": int(s.get("n_trades_assessed") or 0),
    }


def _extract_what_worked(raw: dict) -> List[dict]:
    items = raw.get("what_worked") or []
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append({
            "observation": str(item.get("observation") or ""),
            "evidence": str(item.get("evidence") or ""),
            "why": str(item.get("why") or ""),
            "confidence": _safe_float(item.get("confidence")),
            "n_supporting": int(item.get("n_supporting") or 0),
        })
    return result


def _extract_what_failed(raw: dict) -> List[dict]:
    items = raw.get("what_failed") or []
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cf = item.get("common_factors") or []
        result.append({
            "observation": str(item.get("observation") or ""),
            "common_factors": [str(f) for f in cf] if isinstance(cf, list) else [],
            "why_it_failed": str(item.get("why_it_failed") or ""),
            "confidence": _safe_float(item.get("confidence")),
            "n_supporting": int(item.get("n_supporting") or 0),
        })
    return result


def _extract_calibration(raw: dict) -> dict:
    c = raw.get("calibration_assessment") or {}
    caveats = c.get("caveats") or []
    return {
        "overall": _coerce_str(c.get("overall"), _VALID_CALIBRATION, "UNCLEAR"),
        "edge_vs_outcome_narrative": str(c.get("edge_vs_outcome_narrative") or ""),
        "rejection_pattern_narrative": str(c.get("rejection_pattern_narrative") or ""),
        "confidence": _safe_float(c.get("confidence")) or 0.0,
        "caveats": [str(x) for x in caveats] if isinstance(caveats, list) else [],
    }


def _extract_regime(raw: dict) -> dict:
    r = raw.get("market_regime_assessment") or {}
    return {
        "inferred_regime": _coerce_str(r.get("inferred_regime"), _VALID_REGIMES, "UNCLEAR"),
        "supporting_evidence": str(r.get("supporting_evidence") or ""),
        "strategy_fit_narrative": str(r.get("strategy_fit_narrative") or ""),
        "confidence": _safe_float(r.get("confidence")) or 0.0,
    }


def _extract_suggestions(raw: dict) -> List[dict]:
    items = raw.get("parameter_suggestions") or []
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        param = item.get("parameter")
        if not param:
            continue  # drop suggestions without parameter name
        result.append({
            "parameter": str(param),
            "current_value": item.get("current_value"),
            "suggested_value": item.get("suggested_value"),
            "reasoning": str(item.get("reasoning") or ""),
            "expected_effect": str(item.get("expected_effect") or ""),
            "overfit_risk": _coerce_str(item.get("overfit_risk"), _VALID_OVERFIT_RISK, "HIGH"),
            "confidence": _safe_float(item.get("confidence")),
            "promotion_path": str(item.get("promotion_path") or ""),
        })
    return result


def run_weekly_reflection(reflection_packet: dict) -> dict:
    """
    Call OpenAI to generate a structured weekly performance reflection.

    Advisory only — never mutates any state. Never raises.

    Args:
        reflection_packet: Output of build_reflection_packet().
                           Must have schema_version "1.0".

    Returns:
        Reflection result dict (see module docstring for full schema).
    """
    ts_utc = datetime.now(timezone.utc).isoformat()
    period = reflection_packet.get("period", {})
    result = _empty_result(ts_utc, period)

    # ------------------------------------------------------------------
    # Guard: schema version
    # ------------------------------------------------------------------
    schema = reflection_packet.get("schema_version", "MISSING")
    if schema != SUPPORTED_SCHEMA_VERSION:
        result["error"] = (
            f"run_weekly_reflection: unsupported schema_version={schema!r}. "
            f"Expected {SUPPORTED_SCHEMA_VERSION!r}."
        )
        log.warning(result["error"])
        return result

    # ------------------------------------------------------------------
    # Guard: openai package (lazy import)
    # ------------------------------------------------------------------
    try:
        import openai as _openai_mod  # noqa: PLC0415
    except ImportError:
        result["error"] = "openai package not installed; pip install openai"
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
    # Build user message — compact JSON summary
    # ------------------------------------------------------------------
    try:
        user_content = json.dumps(reflection_packet, default=str, indent=2)
    except Exception as exc:
        result["error"] = f"Failed to serialize reflection_packet: {exc}"
        return result

    # ------------------------------------------------------------------
    # API call
    # ------------------------------------------------------------------
    raw_response = ""
    try:
        client = _openai_mod.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=REFLECTION_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
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

    # ------------------------------------------------------------------
    # Post-parse evidence-strength enforcement
    # ------------------------------------------------------------------
    summary = _extract_summary(parsed)
    n_runs = summary["n_runs_assessed"]
    if n_runs < 3 and summary["evidence_strength"] not in ("INSUFFICIENT", "WEAK"):
        summary["evidence_strength"] = "INSUFFICIENT"

    # Extract all fields
    what_worked = _extract_what_worked(parsed)
    what_failed = _extract_what_failed(parsed)
    calibration = _extract_calibration(parsed)
    regime = _extract_regime(parsed)
    suggestions = _extract_suggestions(parsed)

    # Enforce: if active_parameters is empty → no suggestions
    if not reflection_packet.get("active_parameters"):
        suggestions = []

    open_questions_raw = parsed.get("open_questions") or []
    open_questions = [str(q) for q in open_questions_raw] if isinstance(open_questions_raw, list) else []

    weak_flags_raw = parsed.get("weak_evidence_flags") or []
    weak_flags = [str(f) for f in weak_flags_raw] if isinstance(weak_flags_raw, list) else []

    # Append auto-generated weak flag if n_runs very low
    if n_runs < 3 and "Fewer than 3 runs in period — conclusions are speculative" not in weak_flags:
        weak_flags.append("Fewer than 3 runs in period — conclusions are speculative")

    result.update({
        "status": "ok",
        "period": period,
        "summary": summary,
        "what_worked": what_worked,
        "what_failed": what_failed,
        "calibration_assessment": calibration,
        "market_regime_assessment": regime,
        "parameter_suggestions": suggestions,
        "open_questions": open_questions,
        "weak_evidence_flags": weak_flags,
        "raw_response": raw_response,
        "error": None,
        "ts_utc": ts_utc,
    })
    return result
