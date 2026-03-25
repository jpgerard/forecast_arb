"""
ccc_status.py — Show CCC allocator state for operator inspection.

CCC v1.9 "Operator Hygiene"

Prints a concise snapshot of: actual inventory, pending intents (with age),
and stale pending warnings.  No system modifications — read-only.

Usage:
    python scripts/ccc_status.py
    python scripts/ccc_status.py --policy configs/allocator_ccc_v1.yaml
    python scripts/ccc_status.py --stale-days 3

Output:
    ══════════════════════════════════════════ CCC STATUS (v1.9) ════════
      ACTUAL INVENTORY  (from positions.json):
        crash  : 1
        selloff: 0

      PENDING  (committed-not-filled):
        crash  : 1
        selloff: 0

      PENDING INTENT DETAIL:
        intent_id                candidate_id          regime  age  intent_path
        abc1234...               cand_xyz...           crash    2d  intents/... [MISSING]

      ⚠  STALE PENDING WARNINGS:
        STALE_PENDING: intent_id=abc1234  age=2d  regime=crash
          → suggest: python scripts/ccc_cancel.py --intent-id abc1234 --reason "stale" --paper
    ═════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_DEFAULT_COMMIT_LEDGER = Path("runs/allocator/allocator_commit_ledger.jsonl")
_DEFAULT_FILLS_LEDGER = Path("runs/allocator/allocator_fills_ledger.jsonl")
_DEFAULT_POSITIONS = Path("runs/allocator/positions.json")

STALE_PENDING_DAYS_DEFAULT: int = 2  # config default (task spec)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_today() -> str:
    """Return today's date YYYY-MM-DD in America/New_York, fallback to system local."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return date_cls.today().isoformat()


def _load_positions(positions_path: Path) -> Dict[str, int]:
    """
    Load positions.json → {regime: count}.

    Supports two schemas:
      - List of position dicts:  [{regime: "crash", ...}, ...]
      - Dict {regime: int}:      {"crash": 1, "selloff": 0}
      - Dict {regime: list}:     {"crash": [...], "selloff": [...]}

    Returns empty dict if file is missing or unreadable.
    """
    if not positions_path.exists():
        return {}
    try:
        with open(positions_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            counts: Dict[str, int] = {}
            for pos in data:
                regime = str(pos.get("regime", "unknown")).lower()
                counts[regime] = counts.get(regime, 0) + 1
            return counts
        if isinstance(data, dict):
            result: Dict[str, int] = {}
            for k, v in data.items():
                if isinstance(v, int):
                    result[k] = v
                elif isinstance(v, list):
                    result[k] = len(v)
                elif isinstance(v, dict):
                    # nested dict — count keys as proxy
                    result[k] = 1 if v else 0
            return result
    except Exception:
        pass
    return {}


def _age_days(date_str: str, today: str) -> int:
    """Compute non-negative days between date_str and today (YYYY-MM-DD strings)."""
    try:
        delta = date_cls.fromisoformat(today) - date_cls.fromisoformat(date_str)
        return max(0, delta.days)
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Core status logic (public API)
# ---------------------------------------------------------------------------


def run_status(
    commit_ledger_path: Path = _DEFAULT_COMMIT_LEDGER,
    fills_ledger_path: Path = _DEFAULT_FILLS_LEDGER,
    positions_path: Path = _DEFAULT_POSITIONS,
    stale_days: int = STALE_PENDING_DAYS_DEFAULT,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compute and print the full CCC status panel.

    Args:
        commit_ledger_path: Path to allocator_commit_ledger.jsonl
        fills_ledger_path:  Path to allocator_fills_ledger.jsonl
        positions_path:     Path to positions.json
        stale_days:         Intents older than this (days) are flagged as stale.
        today:              Override today date (YYYY-MM-DD).  Default: local date.

    Returns:
        Dict:
          {
            "actual":         {regime: int},
            "pending_counts": {"crash": int, "selloff": int},
            "pending_rows":   [{intent_id, candidate_id, regime, date, age_days, ...}],
            "stale_count":    int,
          }
    """
    from forecast_arb.allocator.pending import (
        compute_pending_intent_ids,
        load_commit_ledger_rows,
    )

    if today is None:
        today = _get_today()

    # ── ACTUAL inventory ────────────────────────────────────────────────────
    actual = _load_positions(positions_path)
    crash_actual = actual.get("crash", 0)
    selloff_actual = actual.get("selloff", 0)

    # ── PENDING (committed − filled) ────────────────────────────────────────
    pending_ids = compute_pending_intent_ids(commit_ledger_path, fills_ledger_path)

    # Enrich with age + intent_path existence (first OPEN row per intent_id)
    commit_rows = load_commit_ledger_rows(commit_ledger_path)
    _OPEN_ONLY = {"OPEN", "", None}
    seen_ids: set = set()
    pending_rows: List[Dict[str, Any]] = []

    for row in commit_rows:
        if row.get("action") not in _OPEN_ONLY:
            continue
        iid = str(row.get("intent_id", "")).strip()
        if not iid or iid not in pending_ids or iid in seen_ids:
            continue
        seen_ids.add(iid)

        age = _age_days(row.get("date", ""), today)
        intent_path_str = row.get("intent_path", "")
        intent_exists = bool(intent_path_str and Path(intent_path_str).exists())

        pending_rows.append(
            {
                "intent_id": iid,
                "candidate_id": row.get("candidate_id", ""),
                "regime": row.get("regime", "unknown"),
                "date": row.get("date", ""),
                "age_days": age,
                "intent_path": intent_path_str,
                "intent_exists": intent_exists,
            }
        )

    pending_counts = {
        "crash": sum(1 for r in pending_rows if r["regime"] == "crash"),
        "selloff": sum(1 for r in pending_rows if r["regime"] == "selloff"),
    }
    stale_rows = [r for r in pending_rows if r["age_days"] > stale_days]

    # ── Print panel ─────────────────────────────────────────────────────────
    W = 72
    print()
    print("═" * W)
    print("  CCC STATUS  (v1.9 Operator Hygiene)")
    print("═" * W)

    # Actual
    print()
    pos_note = f"(from {positions_path})"
    if not positions_path.exists():
        pos_note = f"⚠ positions.json NOT FOUND at {positions_path} — defaulting to 0"
    print(f"  ACTUAL INVENTORY  {pos_note}:")
    print(f"    crash  : {crash_actual}")
    print(f"    selloff: {selloff_actual}")

    # Pending counts
    print()
    print("  PENDING  (committed-not-filled, ledger-based):")
    print(f"    crash  : {pending_counts['crash']}")
    print(f"    selloff: {pending_counts['selloff']}")

    # Pending detail
    print()
    if pending_rows:
        print("  PENDING INTENT DETAIL:")
        hdr = (
            f"    {'intent_id':<26} {'candidate_id':<22} "
            f"{'regime':<8} {'age':>4}  intent_path"
        )
        print(hdr)
        print("    " + "─" * 88)
        for r in pending_rows:
            iid_s = r["intent_id"][:26]
            cid_s = (r["candidate_id"] or "—")[:22]
            age_s = f"{r['age_days']}d"
            exist_tag = "[EXISTS]" if r["intent_exists"] else "[MISSING]"
            ip = r["intent_path"] or "—"
            stale_tag = "  ⚠ STALE" if r["age_days"] > stale_days else ""
            print(
                f"    {iid_s:<26} {cid_s:<22} {r['regime']:<8} {age_s:>4}  "
                f"{ip}  {exist_tag}{stale_tag}"
            )
    else:
        print("  PENDING INTENT DETAIL: (none — ledgers are clean)")

    # Stale warnings
    print()
    if stale_rows:
        print(f"  ⚠  STALE PENDING WARNINGS  (>{stale_days} days old):")
        for r in stale_rows:
            print(
                f"    STALE_PENDING: intent_id={r['intent_id']}"
                f"  age={r['age_days']}d"
                f"  regime={r['regime']}"
            )
            print(
                f"      → suggest: python scripts/ccc_cancel.py "
                f"--intent-id {r['intent_id']} "
                f"--reason \"stale pending\" --paper"
            )
        print()
    else:
        print(f"  ✓  No stale pending intents (threshold={stale_days}d).")

    print()
    print("═" * W)
    print()

    return {
        "actual": actual,
        "pending_counts": pending_counts,
        "pending_rows": pending_rows,
        "stale_count": len(stale_rows),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show CCC allocator state (v1.9 Operator Hygiene). Read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick status with default paths
  python scripts/ccc_status.py

  # Use a specific policy file for ledger paths
  python scripts/ccc_status.py --policy configs/allocator_ccc_v1.yaml

  # Custom stale threshold (flag as stale after 3 days instead of 2)
  python scripts/ccc_status.py --stale-days 3
""",
    )
    parser.add_argument(
        "--policy",
        default=None,
        help="Path to allocator policy YAML (optional; derives ledger paths from policy).",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=STALE_PENDING_DAYS_DEFAULT,
        dest="stale_days",
        help=(
            f"Days after which a pending intent is flagged as stale "
            f"(default: {STALE_PENDING_DAYS_DEFAULT})."
        ),
    )

    args = parser.parse_args()

    commit_ledger = _DEFAULT_COMMIT_LEDGER
    fills_ledger = _DEFAULT_FILLS_LEDGER
    positions = _DEFAULT_POSITIONS

    if args.policy:
        try:
            from forecast_arb.allocator.policy import (
                get_commit_ledger_path,
                get_fills_ledger_path,
                get_positions_path,
                load_policy,
            )

            pol = load_policy(args.policy)
            commit_ledger = get_commit_ledger_path(pol)
            fills_ledger = get_fills_ledger_path(pol)
            positions = get_positions_path(pol)
        except Exception as exc:
            print(f"\n  ⚠️  Could not load policy {args.policy!r}: {exc}")
            print("  Using default paths.\n")

    run_status(
        commit_ledger_path=commit_ledger,
        fills_ledger_path=fills_ledger,
        positions_path=positions,
        stale_days=args.stale_days,
    )


if __name__ == "__main__":
    main()
