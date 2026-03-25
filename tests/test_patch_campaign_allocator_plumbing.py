"""
Tests for Campaign→Allocator Candidate Plumbing patch.

Verifies:
  1. _load_candidates_any_schema() – recommended.json schema {"selected": [...]}
  2. _load_candidates_any_schema() – flat candidates schema {"candidates": [...]}
  3. _load_candidates_any_schema() – raw list schema [{...}]
  4. run_allocator_plan() raises ValueError (fail loud) on empty candidates
  5. run_allocator_plan() emits [ALLOCATOR] Loaded N candidate(s) INFO log (optional)

Constraints
-----------
  - No network calls, no IBKR connections
  - Uses real configs/allocator_ccc_v1.yaml (committed to repo)
  - All file I/O via pytest tmp_path
  - dry_run=True so no filesystem side-effects from full runs
"""
from __future__ import annotations

import json
import logging
import pathlib

import pytest

from forecast_arb.allocator.plan import (
    _load_candidates_any_schema,
    run_allocator_plan,
)

# ---------------------------------------------------------------------------
# Minimal candidate dict that passes all downstream field accesses.
# Fields required by open_plan._evaluate_candidate:
#   representable, computed_premium_usd / debit_per_contract, ev_per_dollar,
#   max_gain_per_contract, regime, candidate_id
# ---------------------------------------------------------------------------
_MINIMAL_CANDIDATE: dict = {
    "candidate_id": "SPY_20260402_590_570_crash_test001",
    "regime": "crash",
    "underlier": "SPY",
    "expiry": "20260402",
    "long_strike": 590.0,
    "short_strike": 570.0,
    "computed_premium_usd": 5.00,     # debit in $/contract (same as debit_per_contract)
    "ev_per_dollar": 2.0,             # above crash threshold of 1.6
    "max_gain_per_contract": 2000.0,  # 2000/5 = 400x > 25x convexity threshold
    "representable": True,
    "p_used_src": "implied",
    "run_id": "test_run_001",
}

# Path to the real policy YAML committed in the repo
_POLICY_PATH = str(
    pathlib.Path(__file__).resolve().parent.parent / "configs" / "allocator_ccc_v1.yaml"
)


# ===========================================================================
# Task 1 — _load_candidates_any_schema unit tests
# ===========================================================================

class TestLoadCandidatesAnySchema:
    """Direct unit tests for the schema-aware loader helper."""

    def test_recommended_json_schema(self, tmp_path: pathlib.Path) -> None:
        """recommended.json schema: {"selected": [<candidate>]} → list of length 1."""
        f = tmp_path / "recommended.json"
        f.write_text(
            json.dumps({"selected": [_MINIMAL_CANDIDATE]}),
            encoding="utf-8",
        )

        result = _load_candidates_any_schema(str(f))

        assert isinstance(result, list), "Should return a list"
        assert len(result) == 1, "Should have exactly 1 candidate"
        assert result[0]["candidate_id"] == _MINIMAL_CANDIDATE["candidate_id"]

    def test_flat_candidates_schema(self, tmp_path: pathlib.Path) -> None:
        """candidates_flat.json schema: {"candidates": [<candidate>]} → list of length 1."""
        f = tmp_path / "candidates_flat.json"
        f.write_text(
            json.dumps({"candidates": [_MINIMAL_CANDIDATE]}),
            encoding="utf-8",
        )

        result = _load_candidates_any_schema(str(f))

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["candidate_id"] == _MINIMAL_CANDIDATE["candidate_id"]

    def test_raw_list_schema(self, tmp_path: pathlib.Path) -> None:
        """Raw list schema: [{...}] → list of length 1."""
        f = tmp_path / "candidates.json"
        f.write_text(
            json.dumps([_MINIMAL_CANDIDATE]),
            encoding="utf-8",
        )

        result = _load_candidates_any_schema(str(f))

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["candidate_id"] == _MINIMAL_CANDIDATE["candidate_id"]

    def test_missing_file_raises_file_not_found(self, tmp_path: pathlib.Path) -> None:
        """Non-existent file → FileNotFoundError with descriptive message."""
        missing = tmp_path / "does_not_exist.json"

        with pytest.raises(FileNotFoundError, match="Allocator candidates_path not found"):
            _load_candidates_any_schema(str(missing))

    def test_empty_selected_returns_empty_list(self, tmp_path: pathlib.Path) -> None:
        """{"selected": []} → empty list; caller is responsible for fail-loud check."""
        f = tmp_path / "recommended.json"
        f.write_text(json.dumps({"selected": []}), encoding="utf-8")

        result = _load_candidates_any_schema(str(f))

        assert result == []

    def test_empty_candidates_key_returns_empty_list(self, tmp_path: pathlib.Path) -> None:
        """{"candidates": []} → empty list."""
        f = tmp_path / "candidates_flat.json"
        f.write_text(json.dumps({"candidates": []}), encoding="utf-8")

        result = _load_candidates_any_schema(str(f))

        assert result == []

    def test_multiple_candidates_returned(self, tmp_path: pathlib.Path) -> None:
        """{"selected": [c1, c2]} → list of length 2."""
        c2 = dict(_MINIMAL_CANDIDATE)
        c2["candidate_id"] = "SPY_20260402_590_570_crash_test002"
        f = tmp_path / "recommended.json"
        f.write_text(
            json.dumps({"selected": [_MINIMAL_CANDIDATE, c2]}),
            encoding="utf-8",
        )

        result = _load_candidates_any_schema(str(f))

        assert len(result) == 2


# ===========================================================================
# Task 2 — Fail loud (ValueError) when allocator receives zero candidates
# ===========================================================================

class TestFailLoudEmptyCandidates:
    """
    run_allocator_plan() must raise ValueError with the schema mismatch message
    whenever candidates_path resolves to an empty list.

    This ensures a pipeline bug (e.g. campaign failed to populate selected)
    is never silently swallowed as a HOLD NO_QUALIFYING_TRADES.
    """

    def test_raises_on_empty_selected(self, tmp_path: pathlib.Path) -> None:
        """{"selected": []} → ValueError with 'Allocator received zero candidates'."""
        candidates_file = tmp_path / "recommended.json"
        candidates_file.write_text(json.dumps({"selected": []}), encoding="utf-8")

        with pytest.raises(ValueError, match="Allocator received zero candidates"):
            run_allocator_plan(
                policy_path=_POLICY_PATH,
                candidates_path=str(candidates_file),
                dry_run=True,
            )

    def test_raises_on_empty_candidates_key(self, tmp_path: pathlib.Path) -> None:
        """{"candidates": []} → ValueError."""
        candidates_file = tmp_path / "candidates_flat.json"
        candidates_file.write_text(json.dumps({"candidates": []}), encoding="utf-8")

        with pytest.raises(ValueError, match="Allocator received zero candidates"):
            run_allocator_plan(
                policy_path=_POLICY_PATH,
                candidates_path=str(candidates_file),
                dry_run=True,
            )

    def test_error_message_mentions_schema_hint(self, tmp_path: pathlib.Path) -> None:
        """
        ValueError message must contain 'Schema mismatch' so operator knows
        the cause is not a trading decision but a plumbing failure.
        """
        candidates_file = tmp_path / "recommended.json"
        candidates_file.write_text(json.dumps({"selected": []}), encoding="utf-8")

        with pytest.raises(ValueError, match="Schema mismatch or upstream failure"):
            run_allocator_plan(
                policy_path=_POLICY_PATH,
                candidates_path=str(candidates_file),
                dry_run=True,
            )

    def test_no_error_when_candidates_present(self, tmp_path: pathlib.Path) -> None:
        """
        Smoke test: run_allocator_plan() completes (no ValueError) when
        candidates_path points to a non-empty recommended.json.

        The plan may still HOLD for other reasons (budget/EV/convexity gates)
        but must NOT raise ValueError.
        """
        candidates_file = tmp_path / "recommended.json"
        candidates_file.write_text(
            json.dumps({"selected": [_MINIMAL_CANDIDATE]}),
            encoding="utf-8",
        )

        # Should complete without raising; dry_run=True → no file writes
        plan = run_allocator_plan(
            policy_path=_POLICY_PATH,
            candidates_path=str(candidates_file),
            dry_run=True,
        )

        # Basic sanity: plan object returned
        assert plan is not None
        assert plan.actions  # at least one action (OPEN or HOLD)


# ===========================================================================
# Task 3 — INFO log "Loaded N candidate(s)" (operational sanity)
# ===========================================================================

class TestLoadedCandidateCountLog:
    """
    run_allocator_plan() must emit exactly one INFO-level log line containing
    '[ALLOCATOR] Loaded N candidate(s)' right after loading candidates.
    This is mandatory for operational traceability.
    """

    def test_info_log_emitted_with_count(
        self,
        tmp_path: pathlib.Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """[ALLOCATOR] Loaded 1 candidate(s) appears in allocator INFO log."""
        candidates_file = tmp_path / "recommended.json"
        candidates_file.write_text(
            json.dumps({"selected": [_MINIMAL_CANDIDATE]}),
            encoding="utf-8",
        )

        logger_name = "forecast_arb.allocator.plan"
        with caplog.at_level(logging.INFO, logger=logger_name):
            try:
                run_allocator_plan(
                    policy_path=_POLICY_PATH,
                    candidates_path=str(candidates_file),
                    dry_run=True,
                )
            except Exception:
                # We only care about the log line; ignore any downstream error
                pass

        matching = [
            r
            for r in caplog.records
            if "[ALLOCATOR] Loaded" in r.message and "candidate(s)" in r.message
        ]
        assert matching, (
            f"Expected '[ALLOCATOR] Loaded N candidate(s)' in logs.\n"
            f"Captured records: {[r.message for r in caplog.records]}"
        )
        # The count should match the number of candidates loaded
        assert "1 candidate(s)" in matching[0].message, (
            f"Expected count 1 in log message, got: {matching[0].message}"
        )

    def test_info_log_reflects_actual_count(
        self,
        tmp_path: pathlib.Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When 2 candidates are loaded, log says '2 candidate(s)'."""
        c2 = dict(_MINIMAL_CANDIDATE)
        c2["candidate_id"] = "SPY_20260402_590_570_crash_test002"
        candidates_file = tmp_path / "recommended.json"
        candidates_file.write_text(
            json.dumps({"selected": [_MINIMAL_CANDIDATE, c2]}),
            encoding="utf-8",
        )

        logger_name = "forecast_arb.allocator.plan"
        with caplog.at_level(logging.INFO, logger=logger_name):
            try:
                run_allocator_plan(
                    policy_path=_POLICY_PATH,
                    candidates_path=str(candidates_file),
                    dry_run=True,
                )
            except Exception:
                pass

        matching = [
            r
            for r in caplog.records
            if "[ALLOCATOR] Loaded" in r.message and "candidate(s)" in r.message
        ]
        assert matching, "Expected '[ALLOCATOR] Loaded N candidate(s)' in logs"
        assert "2 candidate(s)" in matching[0].message
