"""
channel.py — physical RC model for a 2.5D chiplet die-to-die channel.

Computes per-component capacitance and resistance (chiplet pad, interposer pad,
microbump, trace, ESD).

Public API (called by main.py):
    get_channel(cfg)
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Universal physical constants — not process/package dependent, never in config
# ---------------------------------------------------------------------------
_EPS0 = 8.854e-3          # fF/um, vacuum permittivity
_MU0  = 4.0e-13 * math.pi # H/um, vacuum permeability


# ---------------------------------------------------------------------------
# Component models
# ---------------------------------------------------------------------------

def _cap_res_pad(
    wp_um: float,
    tox_um: float,
    eps_ox: float,
    r_ref_ohm: float,
    r_ref_width_um: float,
) -> Tuple[float, float]:
    """Parallel-plate pad capacitance; resistance scaled from reference width."""
    C_fF  = _EPS0 * eps_ox * (wp_um ** 2) / tox_um
    R_ohm = r_ref_ohm * (r_ref_width_um / wp_um)
    return C_fF, R_ohm


def _cap_res_bump(
    pitch_um: float,
    diameter_um: float,
    height_um: float,
    f_GHz: float,
    eps_underfill: float,
    rho_ohm_um: float,
    mu_r: float,
) -> Tuple[float, float]:
    """Cylindrical two-wire bump capacitance; DC + skin-effect resistance."""
    r   = diameter_um / 2.0
    s   = pitch_um - diameter_um
    arg = (2 * r + s) / (2 * r)
    C_fF = math.pi * _EPS0 * eps_underfill * height_um / math.log(arg + math.sqrt(arg**2 - 1))

    sigma    = 1.0 / rho_ohm_um
    mu       = _MU0 * mu_r
    delta_um = 1.0 / math.sqrt(math.pi * f_GHz * 1e9 * mu * sigma)

    A_dc = math.pi * r**2
    R_dc = rho_ohm_um * height_um / A_dc

    A_ac = 2.0 * math.pi * r * delta_um - math.pi * delta_um**2
    if A_ac <= 0.0:
        A_ac = A_dc
    R_ac = rho_ohm_um * height_um / A_ac

    return C_fF, math.sqrt(R_dc**2 + R_ac**2)


def _cap_res_trace(
    width_um: float,
    reach_mm: float,
    eps_r: float,
    c_per_mm_base: float,
    r_per_mm_base: float,
    w0_um: float,
    eps_r0: float,
) -> Tuple[float, float]:
    """Trace C and R scaled from tabulated base values by width and dielectric."""
    w_scale = width_um / w0_um
    C_fF  = c_per_mm_base * w_scale * (eps_r / eps_r0) * reach_mm
    R_ohm = r_per_mm_base * (1.0 / w_scale) * reach_mm
    return C_fF, R_ohm


def _cap_esd(
    esd_type: str,
    bump_pitch_um: float,
    cap_fine_fF: float,
    threshold_um: float,
    cap_interposer_fF: float,
    cap_organic_fF: float,
) -> float:
    """ESD protection capacitance — no resistance component."""
    if esd_type == "silicon":
        return cap_fine_fF if bump_pitch_um <= threshold_um else cap_interposer_fF
    return cap_organic_fF


def _lookup_ucie_pad_cap(
    data_rate_Gbps: float,
    table: list,
) -> float:
    """
    Return the UCIe-spec pad capacitance (fF) for a given data rate.

    table is a list of [max_data_rate_GTs, cap_fF] pairs, sorted by
    max_data_rate_GTs ascending.  The last entry's max_data_rate_GTs
    should be 0 (catch-all, i.e. infinity) — same convention as eq/term
    graduated level tables.

    If data_rate_Gbps exceeds all finite entries, the smallest cap
    (tightest entry, last row) is returned.
    """
    for max_rate, cap_fF in table:
        if max_rate == 0 or data_rate_Gbps <= max_rate:
            return float(cap_fF)
    # Fallback: last entry's cap (most stringent)
    return float(table[-1][1])


def _lookup_esd_multiplier(
    reach_mm: float,
    esd_mode: str,
    levels_table: list,
) -> float:
    """
    Return ESD capacitance multiplier based on reach_mm (auto mode) or explicit level.

    levels_table is a list of [max_reach_mm, multiplier, label] tuples, sorted by
    max_reach_mm ascending. The last entry's max_reach_mm should be 0 (catch-all).

    esd_mode can be:
      - "auto": select level based on reach_mm using the table
      - "minimal", "moderate", "standard": explicit level names
    """
    if not levels_table:
        return 1.0  # Fallback if no levels defined

    if esd_mode == "auto":
        for max_reach, mult, _ in levels_table:
            if max_reach == 0 or reach_mm <= max_reach:
                return float(mult)
        return float(levels_table[-1][1])
    else:
        for max_reach, mult, label in levels_table:
            if label == esd_mode:
                return float(mult)
        # Fallback: if mode not found, use standard
        return 1.0


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ChannelResult:
    # Pad cap mode used
    pad_cap_mode:         str    # 'physical' or 'ucie'

    # Per-component RC
    pad_chiplet_C_fF:     float
    pad_chiplet_R_ohm:    float
    pad_interposer_C_fF:  float
    pad_interposer_R_ohm: float
    bump_C_fF:            float
    bump_R_ohm:           float
    trace_C_fF:           float
    trace_R_ohm:          float
    esd_C_fF:             float

    # Resolved geometry (after defaults applied)
    pad_wp_um:            float
    bump_diameter_um:     float
    bump_height_um:       float
    trace_width_um:       float
    trace_type:           str
    esd_type:             str

    # RC summary
    total_series_R_ohm:   float
    total_shunt_C_fF:     float

    # Frequency reference
    nyquist_freq_GHz:     float   # = data_rate / 2  (NRZ)

    # Delay
    elmore_delay_ps:      float   # total_R * total_C [Ohm*fF*1e-3 = ps]
    elmore_delay_UI:      float   # elmore_delay normalized to one unit interval

    # Energy per bit (channel only, alpha=0.5 fixed for NRZ)
    # C_eff counts topology: TX pad + bump + interposer pad charge fully each bit;
    # trace and ESD are single-ended. See calculate_channel_energy() for reference.
    C_eff_fF:             float   # effective switching capacitance
    E_channel_pJ:         float   # 0.5 * C_eff * VDD^2  [pJ/bit]

    def report(self) -> str:
        lines = [
            "=== Channel RC ===",
            f"  Pad cap mode     : {self.pad_cap_mode}",
            f"  Pad (chiplet)    : C = {self.pad_chiplet_C_fF:.3f} fF   R = {self.pad_chiplet_R_ohm:.4f} Ohm   (Wp = {self.pad_wp_um:.2f} um)",
            f"  Pad (interposer) : C = {self.pad_interposer_C_fF:.3f} fF   R = {self.pad_interposer_R_ohm:.4f} Ohm",
            f"  Bump             : C = {self.bump_C_fF:.3f} fF   R = {self.bump_R_ohm:.4f} Ohm   (D = {self.bump_diameter_um:.2f} um, H = {self.bump_height_um:.2f} um)",
            f"  Trace ({self.trace_type:8s})  : C = {self.trace_C_fF:.3f} fF   R = {self.trace_R_ohm:.4f} Ohm   (w = {self.trace_width_um:.1f} um)",
            f"  ESD  ({self.esd_type:9s}) : C = {self.esd_C_fF:.3f} fF",
            f"  ---",
            f"  Total series R   : {self.total_series_R_ohm:.4f} Ohm",
            f"  Total shunt C    : {self.total_shunt_C_fF:.3f} fF",
            f"  Nyquist freq     : {self.nyquist_freq_GHz:.1f} GHz",
            f"  --- Delay ---",
            f"  Elmore delay     : {self.elmore_delay_ps:.2f} ps  ({self.elmore_delay_UI:.3f} UI)",
            f"  --- Energy per bit (channel only, alpha=0.5 NRZ) ---",
            f"  C_eff            : {self.C_eff_fF:.3f} fF",
            f"  E_channel        : {self.E_channel_pJ:.4f} pJ/bit",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_channel(cfg) -> ChannelResult:
    """
    Compute channel RC for the link in cfg.

    cfg.link           — pkg_type, reach_mm, bump_pitch_um, data_rate_Gbps
    cfg.channel        — user geometry choices (widths, heights, AC coupling)
    cfg.channel_hidden — physical constants from literature
    """
    lk  = cfg.link
    ch  = cfg.channel
    hid = cfg.channel_hidden
    pkg = lk.pkg_type.lower()

    # --- Package-dependent defaults ---
    if pkg in ("silicon", "silicon_interposer", "interposer"):
        trace_type        = "silicon"
        tox_interposer    = hid.tox_silicon_interposer_um
        eps_ox_interposer = hid.eps_sio2
        eps_underfill     = hid.eps_sio2
        esd_type_default  = "silicon"
        trace_hid         = hid.trace_silicon
    else:
        trace_type        = "organic"
        tox_interposer    = hid.tox_organic_substrate_um
        eps_ox_interposer = hid.eps_fr4
        eps_underfill     = hid.eps_polyimide
        esd_type_default  = "organic"
        trace_hid         = hid.trace_organic

    # --- Resolve geometry (channel choices > computed defaults) ---
    wp = 0.8 * lk.bump_pitch_um

    if ch.bump_diameter_um is not None:
        bump_d = ch.bump_diameter_um
    else:
        bump_d = ch.bump_diameter_scale * lk.bump_pitch_um

    if ch.bump_height_um is not None:
        bump_h = ch.bump_height_um
    else:
        bump_h = 35.0 if bump_d >= 20.0 else bump_d

    trace_w   = ch.trace_width_um if ch.trace_width_um is not None else trace_hid.default_width_um
    esd_type  = ch.esd_type       if ch.esd_type       is not None else esd_type_default

    nyquist_GHz = lk.data_rate_Gbps / 2.0

    # --- Pad capacitance mode ---
    pad_cap_mode = getattr(ch, 'pad_cap_mode', None) or 'physical'

    # --- Components ---
    _, R_pad_chip = _cap_res_pad(wp, hid.tox_chiplet_um,  hid.eps_sio2,         hid.r_pad_ref_ohm, hid.r_pad_ref_width_um)
    _, R_pad_ipos = _cap_res_pad(wp, tox_interposer,      eps_ox_interposer,    hid.r_pad_ref_ohm, hid.r_pad_ref_width_um)

    if pad_cap_mode == 'ucie':
        ucie_table = hid.ucie_pad_cap_table
        C_pad_chip = _lookup_ucie_pad_cap(lk.data_rate_Gbps, ucie_table)
        C_pad_ipos = _lookup_ucie_pad_cap(lk.data_rate_Gbps, ucie_table)
    else:
        C_pad_chip, _ = _cap_res_pad(wp, hid.tox_chiplet_um, hid.eps_sio2,      hid.r_pad_ref_ohm, hid.r_pad_ref_width_um)
        C_pad_ipos, _ = _cap_res_pad(wp, tox_interposer,     eps_ox_interposer, hid.r_pad_ref_ohm, hid.r_pad_ref_width_um)

    C_bump, R_bump = _cap_res_bump(
        pitch_um    = lk.bump_pitch_um,
        diameter_um = bump_d,
        height_um   = bump_h,
        f_GHz       = nyquist_GHz,
        eps_underfill = eps_underfill,
        rho_ohm_um  = hid.bump_resistivity_ohm_um,
        mu_r        = hid.bump_relative_permeability,
    )

    C_trace, R_trace = _cap_res_trace(
        width_um      = trace_w,
        reach_mm      = lk.reach_mm,
        eps_r         = trace_hid.default_eps_r,
        c_per_mm_base = trace_hid.c_per_mm_fF,
        r_per_mm_base = trace_hid.r_per_mm_ohm,
        w0_um         = trace_hid.default_width_um,
        eps_r0        = trace_hid.default_eps_r,
    )

    if pad_cap_mode == 'ucie':
        # UCIe pad cap already includes ESD; don't double-count it
        C_esd = 0.0
    else:
        C_esd_base = _cap_esd(
            esd_type         = esd_type,
            bump_pitch_um    = lk.bump_pitch_um,
            cap_fine_fF      = hid.esd_silicon_fine_pitch_fF,
            threshold_um     = hid.esd_fine_pitch_threshold_um,
            cap_interposer_fF= hid.esd_silicon_interposer_fF,
            cap_organic_fF   = hid.esd_organic_fF,
        )
        esd_mode = getattr(ch, 'esd_mode', 'auto') or 'auto'
        esd_mult = _lookup_esd_multiplier(lk.reach_mm, esd_mode, hid.esd_levels)
        C_esd = C_esd_base * esd_mult

    total_R   = R_pad_chip + R_bump + 2*R_pad_ipos + R_trace
    total_C   = C_pad_chip + C_esd + C_bump + 2*C_pad_ipos + C_trace
    elmore_ps = total_R * total_C * 1e-3  # Ohm * fF -> ps

    # Delay in unit intervals: 1 UI = 1 / data_rate [ns] = 1000 / data_rate [ps]
    UI_ps        = 1000.0 / lk.data_rate_Gbps
    elmore_UI    = elmore_ps / UI_ps

    # Channel energy per bit (NRZ, alpha=0.5 fixed)
    # Topology: both pads and bump are charged/discharged each bit (×2 in analysis
    # script), trace and ESD are single-ended (×1). VDD from process config.
    vdd    = cfg.process.vdd
    C_eff  = (2 * C_pad_chip + 2 * C_bump + 2 * C_pad_ipos + C_trace + C_esd)
    E_ch   = 0.5 * (C_eff * 1e-15) * (vdd ** 2) * 1e12   # fF -> F -> pJ

    return ChannelResult(
        pad_cap_mode         = pad_cap_mode,
        pad_chiplet_C_fF     = C_pad_chip,
        pad_chiplet_R_ohm    = R_pad_chip,
        pad_interposer_C_fF  = C_pad_ipos,
        pad_interposer_R_ohm = R_pad_ipos,
        bump_C_fF            = C_bump,
        bump_R_ohm           = R_bump,
        trace_C_fF           = C_trace,
        trace_R_ohm          = R_trace,
        esd_C_fF             = C_esd,
        pad_wp_um            = wp,
        bump_diameter_um     = bump_d,
        bump_height_um       = bump_h,
        trace_width_um       = trace_w,
        trace_type           = trace_type,
        esd_type             = esd_type,
        total_series_R_ohm   = total_R,
        total_shunt_C_fF     = total_C,
        nyquist_freq_GHz     = nyquist_GHz,
        elmore_delay_ps      = elmore_ps,
        elmore_delay_UI      = elmore_UI,
        C_eff_fF             = C_eff,
        E_channel_pJ         = E_ch,
    )