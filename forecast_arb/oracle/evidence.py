"""
forecast_arb.oracle.evidence
============================
Evidence-class taxonomy for Kalshi-derived external probability signals.

Patch B: classification only.  Gating authority unchanged.
Patch C: adds EVIDENCE_ROLE policy table and is_authoritative_capable() helper.
         The table encodes the *current* intended role of each class.
         It does NOT change gating behaviour — that is deferred to a later patch.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, Optional


class EvidenceClass(str, Enum):
    """
    Semantic strength of Kalshi-derived external evidence.

    Values are plain strings so instances serialise to JSON without extra
    conversion (``json.dumps(EvidenceClass.EXACT_TERMINAL)`` → ``"EXACT_TERMINAL"``).

    Authoritative capability
    ------------------------
    - EXACT_TERMINAL is *authoritative-capable* in a future patch; Patch B
      classifies it only — no gating change yet.
    - All other classes are non-authoritative in Patch B.
    """

    #: KXINX (daily) terminal market with mapping_error ≤ EXACT_MATCH_THRESHOLD_PCT.
    #: Semantics: the Kalshi contract settles on the same terminal value,
    #: same date, with essentially no strike distance.
    EXACT_TERMINAL = "EXACT_TERMINAL"

    #: Terminal market with non-trivial mapping_error, OR yearly-close (KXINXY)
    #: series.  Informative but not authoritative in Patch B.
    NEARBY_TERMINAL = "NEARBY_TERMINAL"

    #: Path-dependent proxy from KXINXMINY / KXINXMAXY (hazard-scaled).
    #: Settles on yearly-min/max, not the terminal value.  Never authoritative.
    PATHWISE_PROXY = "PATHWISE_PROXY"

    #: Annual / directional context only.  A market was found and parsed but is
    #: too coarse to use as a terminal probability (e.g. KXINXY yearly-close
    #: found but outside terminal tolerance).  Never authoritative.
    COARSE_REGIME = "COARSE_REGIME"

    #: No usable signal: parse failure, no markets returned, error too large,
    #: no pricing data, or no Kalshi market found at all.
    #: ``p_external_value`` must be None when evidence_class is UNUSABLE.
    UNUSABLE = "UNUSABLE"


# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------

#: Mapping error (as a **percentage**, e.g. ``0.1`` means 0.1%) at or below
#: which a KXINX match is classified EXACT_TERMINAL rather than NEARBY_TERMINAL.
EXACT_MATCH_THRESHOLD_PCT: float = 0.1

#: Series that can yield EXACT_TERMINAL classification.
#: Only KXINX (daily close) qualifies — it is a terminal bet on a specific date.
EXACT_TERMINAL_SERIES: frozenset = frozenset({"KXINX"})

#: Series that are at most NEARBY_TERMINAL regardless of mapping_error.
#: KXINXY is a yearly-close contract — semantically approximate even when
#: mapping_error is numerically small.
YEARLY_SERIES: frozenset = frozenset({"KXINXY"})

#: Upper bound on mapping_error_pct for COARSE_REGIME classification.
#: A KXINXY market beyond this threshold is too far to provide meaningful
#: directional context and should be classified UNUSABLE.
COARSE_REGIME_MAX_ERROR_PCT: float = 15.0


# ---------------------------------------------------------------------------
# Patch C: policy role table
# ---------------------------------------------------------------------------

#: Maps each EvidenceClass to its current operational role string.
#:
#: Role semantics:
#:   AUTHORITATIVE_CAPABLE — may be used as the authoritative p_external in a
#:     future gating patch.  Currently classified only; no gating change yet.
#:   INFORMATIVE_ONLY — present in diagnostics/summary; does not gate.
#:   CONTEXT_ONLY — coarse directional context; never used as a probability.
#:   DIAGNOSTIC_ONLY — no usable signal; contributes to absence-of-evidence logs.
#:
#: This table is the single source of truth for `is_authoritative_capable()`.
EVIDENCE_ROLE: Dict[str, str] = {
    EvidenceClass.EXACT_TERMINAL:  "AUTHORITATIVE_CAPABLE",
    EvidenceClass.NEARBY_TERMINAL: "INFORMATIVE_ONLY",
    EvidenceClass.PATHWISE_PROXY:  "INFORMATIVE_ONLY",
    EvidenceClass.COARSE_REGIME:   "CONTEXT_ONLY",
    EvidenceClass.UNUSABLE:        "DIAGNOSTIC_ONLY",
}


def is_authoritative_capable(ec: Optional[EvidenceClass]) -> bool:
    """Return True iff *ec* is AUTHORITATIVE_CAPABLE under the current policy.

    Only ``EXACT_TERMINAL`` qualifies today.  Returns ``False`` for ``None``
    (pre-Patch-B objects or unclassified results).

    This function does **not** change gating behaviour.  It is a classification
    helper used to populate ``authoritative_capable`` in artifacts so that
    downstream analytics and future gating patches can act on the information.

    Args:
        ec: An ``EvidenceClass`` member or ``None``.

    Returns:
        ``True`` only when ``EVIDENCE_ROLE[ec] == "AUTHORITATIVE_CAPABLE"``.
    """
    if ec is None:
        return False
    return EVIDENCE_ROLE.get(ec) == "AUTHORITATIVE_CAPABLE"
