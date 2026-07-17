"""
termination.py — termination decision and energy model.

Decides whether RX termination is needed, selects a graduated termination
level, and computes the power/energy overhead.

Public API (called by main.py):
    get_termination(cfg, ch_result)
"""

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Default termination level table
# ---------------------------------------------------------------------------
# Each tuple: (max_reach_ratio, r_scale, c_ac_scale, label)
#   max_reach_ratio: if reach_mm / max_reach_unterm_mm <= this, select level
#   r_scale:         actual R_term = r_scale * r_rx_ohm
#   c_ac_scale:      actual C_ac   = c_ac_scale * base_c_ac_pF (30 pF)
#   A max_reach_ratio of 0 means infinity (catch-all).
DEFAULT_TERM_LEVELS = [
    (1.0,  0.0, 0.0, "none"),
    (1.25, 2.0, 0.5, "light"),
    (1.5,  1.0, 1.0, "standard"),
    (0,    0.5, 2.0, "strong"),
]

# Base AC coupling capacitor (pF)
_BASE_C_AC_PF = 30.0


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TerminationResult:
    # Decision
    use_termination:     bool
    max_reach_unterm_mm: float   # boundary at this (tx_swing, data_rate) without termination
    max_reach_term_mm:   float   # boundary with termination applied

    # Graduated level
    term_level:          int     # 0 = none, 1 = light, 2 = standard, 3 = strong
    term_level_label:    str     # human-readable level name
    reach_ratio:         float   # reach_mm / max_reach_unterm_mm

    # Actual termination parameters (vary by level)
    r_term_ohm:          float   # actual termination resistance used (RT in SPICE)
    c_ac_pF:             float   # actual AC coupling capacitance (CT in SPICE)

    # Energy / power (zero when termination not used)
    P_term_mW:           float   # DC termination power  [mW]
    E_term_pJ:           float   # energy per bit        [pJ/bit]

    # Parameters echoed for downstream consumers
    r_tx_ohm:            float
    r_rx_ohm:            float
    ac_coupled:          bool

    def report(self) -> str:
        if self.use_termination:
            decision = f"TERMINATED — Level {self.term_level} ({self.term_level_label})"
        else:
            decision = "UNTERMINATED — Level 0 (none)"
        lines = [
            "=== Termination ===",
            f"  Decision         : {decision}",
            f"  Max reach (unterm): {self.max_reach_unterm_mm:.1f} mm",
            f"  Max reach (term)  : {self.max_reach_term_mm:.1f} mm",
            f"  Reach ratio       : {self.reach_ratio:.2f}x",
        ]
        if self.use_termination:
            lines += [
                f"  R_TX (source)    : {self.r_tx_ohm:.1f} Ohm",
                f"  R_term (actual)  : {self.r_term_ohm:.1f} Ohm",
                f"  C_ac (actual)    : {self.c_ac_pF:.1f} pF",
                f"  AC-coupled       : {self.ac_coupled}",
                "  Energy           : captured by the RX Liberate/CharLib run (the RT +",
                "                     bias network lives in rxip.scs on the PAD).",
            ]
        else:
            lines.append("  No termination overhead.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Boundary table lookup  (internalized from SerialLinkDesigner)
# ---------------------------------------------------------------------------

def _parse_table(raw_table: Dict[str, List]) -> Dict[float, List[Tuple[float, float]]]:
    """
    Convert the JSON boundary table (string-keyed) to float-keyed dict of
    (rate, reach) tuples.

    raw_table : {"0.85": [[16.0, 0.0], [16.0, 5.0], ...], ...}
    """
    return {
        float(swing): [(float(pair[0]), float(pair[1])) for pair in pairs]
        for swing, pairs in raw_table.items()
    }


def _nearest_swing(table: Dict[float, List], tx_swing: float) -> float:
    """Return the closest available swing entry in the table."""
    return min(table.keys(), key=lambda s: abs(s - tx_swing))


def _lookup_max_reach(
    table: Dict[float, List[Tuple[float, float]]],
    tx_swing: float,
    data_rate_GTs: float,
) -> float:
    """
    Direct piecewise lookup for max unterminated reach (mm) given
    a data rate (GT/s).  Matches the boundary segments of the datasheet
    exactly — no smoothing or global interpolation.

    If data_rate_GTs is outside the table range it is clamped to the nearest
    table boundary so that the closest available restriction is used.

    Algorithm: walk the boundary sorted by rate descending, find the two
    adjacent points that bracket data_rate_GTs, then interpolate along
    that segment.
    """
    swing = _nearest_swing(table, tx_swing)
    limits = table[swing]

    # Sort boundary points by rate descending (high rate → short reach)
    sorted_pts = sorted(limits, key=lambda p: p[0], reverse=True)

    # Clamp to the table's rate range so out-of-range rates use the
    # restriction at the nearest defined boundary.
    max_rate = sorted_pts[0][0]
    min_rate = sorted_pts[-1][0]
    data_rate_GTs = max(min_rate, min(max_rate, data_rate_GTs))

    for i in range(len(sorted_pts) - 1):
        r_hi, reach_hi = sorted_pts[i]
        r_lo, reach_lo = sorted_pts[i + 1]

        if r_lo <= data_rate_GTs <= r_hi:
            if r_hi == r_lo:
                # Vertical segment (same rate, two reach values):
                # the boundary means the link can operate without termination
                # up to the *larger* reach at this rate.
                return max(reach_hi, reach_lo)
            # Linear interpolation along this segment
            t = (data_rate_GTs - r_lo) / (r_hi - r_lo)
            return reach_lo + t * (reach_hi - reach_lo)

    # Fallback — should not be reached after clamping
    return sorted_pts[-1][1]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_termination(cfg, ch_result) -> TerminationResult:
    """
    Decide whether to use termination, select graduated level, and compute
    its energy overhead.

    Parameters
    ----------
    cfg       : Config (from main.py)
    ch_result : ChannelResult (from channel.get_channel)

    Decision rule (hard boundary from datasheet):
        use_termination = (reach_mm > max_reach_unterm)

    Graduated level selection:
        reach_ratio = reach_mm / max_reach_unterm_mm
        Select level from term_levels table based on reach_ratio.

    NRZ encoding: data_rate_Gbps == symbol rate GT/s — no division by 2.
    """
    term     = cfg.termination
    hid      = cfg.termination_hidden
    lk       = cfg.link
    vdd      = cfg.process.vdd

    # Parse the boundary table from config
    table = _parse_table(hid.unterminated_limits)

    # Lookup boundary for this (swing, rate) point
    max_reach_unterm = _lookup_max_reach(table, vdd, lk.data_rate_Gbps)
    max_reach_term   = max_reach_unterm * hid.termination_improvement_factor

    # Compute reach ratio (guard against max_reach_unterm == 0)
    if max_reach_unterm > 0:
        reach_ratio = lk.reach_mm / max_reach_unterm
    else:
        reach_ratio = float('inf') if lk.reach_mm > 0 else 0.0

    # --- Graduated level selection ---
    term_levels = getattr(hid, 'term_levels', None)
    if term_levels is None:
        term_levels = DEFAULT_TERM_LEVELS

    term_level = 0
    term_label = "none"
    r_scale = 0.0
    c_scale = 0.0

    for idx, (max_ratio, rs, cs, label) in enumerate(term_levels):
        if max_ratio == 0:
            # Catch-all: highest level
            term_level = idx
            term_label = label
            r_scale = rs
            c_scale = cs
            break
        if reach_ratio <= max_ratio:
            term_level = idx
            term_label = label
            r_scale = rs
            c_scale = cs
            break
    else:
        term_level = len(term_levels) - 1
        term_label = term_levels[-1][3]
        r_scale = term_levels[-1][1]
        c_scale = term_levels[-1][2]

    # Apply user override: "auto" = reach-based decision, True = always, False = never
    _enabled = term.enabled
    if _enabled == "auto":
        use_term = r_scale > 0.0
    else:
        use_term = bool(_enabled)

    # Compute actual termination component values used by the netlist.
    # rx.py patches r_term_ohm (RT) + r_bias_*_ohm into the rx subckt in
    # rxip.scs, so the termination's loading/energy is captured by the RX
    # Liberate run.  The P_term_mW / E_term_pJ dataclass fields are kept for
    # backward compatibility but are not used analytically (get_metrics
    # attributes termination energy via the RX char results).
    if use_term:
        r_term_ohm = r_scale * hid.r_rx_ohm
        c_ac_pF = c_scale * _BASE_C_AC_PF if hid.ac_coupled else 0.0
    else:
        r_term_ohm = 0.0
        c_ac_pF = 0.0

    P_mW = 0.0
    E_pJ = 0.0

    return TerminationResult(
        use_termination      = use_term,
        max_reach_unterm_mm  = max_reach_unterm,
        max_reach_term_mm    = max_reach_term,
        term_level           = term_level,
        term_level_label     = term_label,
        reach_ratio          = reach_ratio,
        r_term_ohm           = r_term_ohm,
        c_ac_pF              = c_ac_pF,
        P_term_mW            = P_mW,
        E_term_pJ            = E_pJ,
        r_tx_ohm             = hid.r_tx_ohm,
        r_rx_ohm             = hid.r_rx_ohm,
        ac_coupled           = hid.ac_coupled,
    )