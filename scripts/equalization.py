"""
equalization.py — graduated passive equalization.

Selects an equalization level from the channel loss at Nyquist and computes the
equalizer component values (R_eq, C_eq).

Public API (called by main.py):
    get_equalization(cfg, ch_result)
"""

import math
from dataclasses import dataclass, field
from typing import List, Tuple


# ---------------------------------------------------------------------------
# Default equalization level table
# ---------------------------------------------------------------------------
# Each tuple: (max_loss_dB_multiplier, eq_cap_fraction, label)
#   max_loss_dB_multiplier is a multiplier of loss_threshold_dB.
#   The first level whose (multiplier * threshold) >= loss_dB is selected.
#   A multiplier of 0 means "infinity" (catch-all).
#
# Example with threshold = 3 dB:
#   Level 0  "none"       : loss < 3 dB   → eq_cap_fraction = 0
#   Level 1  "light"      : loss < 6 dB   → eq_cap_fraction = 0.05
#   Level 2  "moderate"   : loss < 9 dB   → eq_cap_fraction = 0.10
#   Level 3  "strong"     : loss < 15 dB  → eq_cap_fraction = 0.15
#   Level 4  "aggressive" : loss >= 15 dB → eq_cap_fraction = 0.20
DEFAULT_EQ_LEVELS = [
    (1.0,  0.0,  "none"),
    (2.0,  0.05, "light"),
    (3.0,  0.10, "moderate"),
    (5.0,  0.15, "strong"),
    (0,    0.20, "aggressive"),
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class EqualizationResult:
    # Decision
    use_equalization:    bool
    loss_dB_nyquist:     float   # channel RC loss at Nyquist [dB]
    loss_threshold_dB:   float   # configured threshold

    # Graduated level
    eq_level:            int     # 0 = none, 1 = light, ..., 4 = aggressive
    eq_level_label:      str     # human-readable level name
    eq_cap_fraction:     float   # actual eq_cap_fraction used for this level

    # Channel frequency parameters
    f_3dB_GHz:           float   # channel RC corner frequency
    nyquist_GHz:         float   # = data_rate / 2

    # Equalizer components (zero when equalization not used)
    R_eq_ohm:            float
    C_eq_fF:             float
    f_zero_GHz:          float   # equalizer zero frequency (should ≈ f_3dB)

    def report(self) -> str:
        if self.use_equalization:
            decision = f"ENABLED — Level {self.eq_level} ({self.eq_level_label})"
        else:
            decision = "NOT NEEDED — Level 0 (none)"
        lines = [
            "=== Equalization ===",
            f"  Decision         : {decision}",
            f"  Channel f_3dB    : {self.f_3dB_GHz:.3f} GHz",
            f"  Nyquist freq     : {self.nyquist_GHz:.1f} GHz",
            f"  Loss at Nyquist  : {self.loss_dB_nyquist:.2f} dB  (threshold: {self.loss_threshold_dB:.1f} dB)",
            f"  EQ cap fraction  : {self.eq_cap_fraction:.3f}",
        ]
        if self.use_equalization:
            lines += [
                f"  R_eq             : {self.R_eq_ohm:.2f} Ohm",
                f"  C_eq             : {self.C_eq_fF:.2f} fF",
                f"  Equalizer zero   : {self.f_zero_GHz:.3f} GHz",
            ]
        else:
            lines.append("  No equalization required.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_equalization(cfg, ch_result, term_result=None) -> EqualizationResult:
    """
    Decide whether passive equalization is needed and compute R_eq, C_eq.

    Graduated equalization: the equalizer strength (eq_cap_fraction) is
    selected from a tiered table based on the channel loss at Nyquist.
    Higher loss → larger fraction → stronger pre-emphasis.

    Level table (from config or DEFAULT_EQ_LEVELS):
        Level 0 "none"       : loss <  1× threshold  → fraction = 0
        Level 1 "light"      : loss <  2× threshold  → fraction = 0.05
        Level 2 "moderate"   : loss <  3× threshold  → fraction = 0.10
        Level 3 "strong"     : loss <  5× threshold  → fraction = 0.15
        Level 4 "aggressive" : loss >= 5× threshold  → fraction = 0.20

    Equalizer sizing (unchanged from original):
        Place the zero at the channel's -3dB corner so the combined
        channel + equalizer response is flat up to Nyquist.

        C_eq = eq_cap_fraction * C_channel
        R_eq = (R_channel * C_channel) / C_eq
             = R_channel / eq_cap_fraction
    """
    eq_cfg = cfg.equalization
    hid    = cfg.equalization_hidden
    lk     = cfg.link

    R_ch = ch_result.total_series_R_ohm   # Ohm
    C_ch = ch_result.total_shunt_C_fF     # fF
    f_ny = ch_result.nyquist_freq_GHz     # GHz

    # Channel -3dB corner: f_3dB = 1 / (2*pi*R*C)
    RC_s  = R_ch * C_ch * 1e-15           # seconds
    f_3dB = 1.0 / (2.0 * math.pi * RC_s) # Hz
    f_3dB_GHz = f_3dB * 1e-9

    # Channel loss at Nyquist (first-order RC low-pass)
    loss_dB = 10.0 * math.log10(1.0 + (f_ny / f_3dB_GHz) ** 2)

    # --- Graduated level selection ---
    eq_levels = getattr(hid, 'eq_levels', None)
    if eq_levels is None:
        eq_levels = DEFAULT_EQ_LEVELS

    threshold = eq_cfg.loss_threshold_dB
    eq_level = 0
    eq_label = "none"
    eq_frac = 0.0

    if eq_cfg.enabled == "auto" or eq_cfg.enabled is True:
        for idx, (mult, frac, label) in enumerate(eq_levels):
            if mult == 0:
                # Catch-all: highest level
                eq_level = idx
                eq_label = label
                eq_frac = frac
                break
            if loss_dB < mult * threshold:
                eq_level = idx
                eq_label = label
                eq_frac = frac
                break
        else:
            # Should not reach here if levels are well-defined
            eq_level = len(eq_levels) - 1
            eq_label = eq_levels[-1][2]
            eq_frac = eq_levels[-1][1]

    use_eq = eq_frac > 0.0

    if use_eq:
        C_eq_fF  = eq_frac * C_ch
        R_eq_ohm = (R_ch * C_ch) / C_eq_fF   # = R_ch / eq_frac

        # -------------------------------------------------------------
        # Cap R_eq so the EQ series resistance does not dominate the
        # time constant of the TX → channel → RX path.
        #
        # Without this cap, R_eq × C_downstream can be >> UI for long-
        # reach channels (e.g. 20 mm at 16 Gbps gives R_eq=139 Ω,
        # C_downstream ≈ 42 pF, τ ≈ 5.8 ns = 93 UI).  That makes the
        # pre-emphasis ineffective and slows the signal far beyond what
        # the channel RC alone would cause.
        #
        # C_downstream = C_channel + C_ac_coupling (when available).
        # The AC coupling cap is often the dominant far-end load.
        #
        # Constraint: R_eq × C_downstream ≤ max_eq_rc_ui × UI
        # When violated, increase eq_frac (and C_eq) so R_eq drops
        # while keeping the EQ zero at f_3dB.
        # -------------------------------------------------------------
        data_rate_Hz = lk.data_rate_Gbps * 1e9
        ui_s         = 1.0 / data_rate_Hz
        max_eq_rc_ui = getattr(hid, 'max_eq_rc_ui', 5.0)

        # Estimate total downstream capacitance (channel + AC coupling)
        c_downstream_fF = C_ch
        if term_result is not None and hasattr(term_result, 'c_ac_pF'):
            c_downstream_fF += term_result.c_ac_pF * 1000.0  # pF → fF
        R_eq_max = max_eq_rc_ui * ui_s / (c_downstream_fF * 1e-15)

        if R_eq_ohm > R_eq_max and R_eq_max > 0:
            R_eq_ohm = R_eq_max
            eq_frac  = R_ch / R_eq_ohm          # recalculate fraction
            C_eq_fF  = eq_frac * C_ch            # recalculate Ceq
            print(f"  [EQ] R_eq capped at {R_eq_ohm:.2f} Ohm  "
                  f"(max_eq_rc_ui={max_eq_rc_ui}, "
                  f"eq_frac raised to {eq_frac:.4f})")

        f_zero_GHz = 1.0 / (2.0 * math.pi * R_eq_ohm * C_eq_fF * 1e-15) * 1e-9
    else:
        R_eq_ohm   = 0.0
        C_eq_fF    = 0.0
        f_zero_GHz = 0.0

    return EqualizationResult(
        use_equalization  = use_eq,
        loss_dB_nyquist   = loss_dB,
        loss_threshold_dB = eq_cfg.loss_threshold_dB,
        eq_level          = eq_level,
        eq_level_label    = eq_label,
        eq_cap_fraction   = eq_frac,
        f_3dB_GHz         = f_3dB_GHz,
        nyquist_GHz       = f_ny,
        R_eq_ohm          = R_eq_ohm,
        C_eq_fF           = C_eq_fF,
        f_zero_GHz        = f_zero_GHz,
    )