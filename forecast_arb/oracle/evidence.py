"""
forecast_arb.oracle.evidence
============================
Evidence-class taxonomy for Kalshi-derived external probability signals.

Patch B: classification only.  Gating authority unchanged.
"""
from __future__ import annotations

from enum import Enum


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
