"""
area.py — physical area model for the TXIP and RXIP IP blocks.

Computes silicon area and the physical bounding box (chiplet side only) from
bump/pad geometry and per-lane cell area. Results feed gen_lef.py and the run
summary.
"""

import math
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Density constants — base defaults then per-PDK overrides.
#
# The base dict (_AREA_DEFAULTS) is a generic 65nm-class planar bulk CMOS
# reference.  _AREA_DEFAULTS_BY_NODE maps cfg.process.node (lower-cased) to a
# *partial* dict of values that differ from the base; keys not listed inherit
# the base.  This public framework ships an entry for "freepdk45" only.
#
# Node matching: "freepdk45" → open 45nm bulk CMOS (documented from the kit)
# Unknown nodes fall back to the base 65nm-class numbers; supply real values
# for any other PDK via that config's "area_hidden" section.
#
# Citation notes — all values are calibrated to published data; PDK-specific
# values (DRC rules, exact sheet R) are NDA and must be overridden per-project
# via the area_hidden config section:
#
#   [CAP65]  S.-H. Chen et al., "Metal-layer capacitors in the 65nm CMOS
#            process and the application for low-leakage power-rail ESD clamp
#            circuit," Microelectronics Reliability, vol. 54, 2014.
#            https://www.sciencedirect.com/science/article/abs/pii/S0026271413003223
#            → 65nm double-MIM density ≈ 1–2 fF/um² (single-plate ~1, double ~2).
#
#   [CAP16]  C.-H. Jan et al., "A 16nm FinFET CMOS technology for mobile SoC
#            and computing applications," IEDM 2014, doi:10.1109/IEDM.2014.6724591.
#            https://ieeexplore.ieee.org/document/6724591
#            → 16FF+ offers a planar HK-MIM (>15 fF/um²) for decoupling only;
#            general signal routing uses MOM cap ≈ 1.0–1.4 fF/um² (community
#            estimate — no single canonical public datasheet).
#
#   [RES65]  Y.-C. Lu et al., "Mis-matching characteristics study of P+-poly-
#            silicon resistor in newly CMOS process technology," CICC 2007,
#            doi:10.1109/CICC.2007.4545864.
#            https://ieeexplore.ieee.org/document/4450328
#            → Unsilicided P+/N+ poly sheet R ≈ 200–400 Ω/□ at 65–90nm node.
#
#   [ESD_SI] B. Van Thourhout et al., "ESD protection design in active-lite
#            interposer for 2.5 and 3D systems-in-package," IEEE Trans. Device
#            Mater. Rel., vol. 16, no. 1, 2016.
#            https://www.researchgate.net/publication/283460154
#            → Silicon interposer ESD diode cap density used to calibrate
#            esd_diode_fF_per_um2 = 0.3 fF/um² (65nm planar bulk).
#
#   [ESD_16] S. Thijs et al., "Optimized Low Parasitic Capacitance ESD Clamps
#            for High-Bandwidth 2.5D/3D Chiplet Interfaces in Advanced FinFET
#            Technology," IEEE EOS/ESD Symposium, 2024.
#            https://monthly-pulse.com/2024/12/25/optimized-low-parasitic-capacitance-esd-clamps-for-high-bandwidth-2-5d-3d-chiplet-interfaces-in-advanced-finfet-technology/
#            → FinFET fin-based ESD diode cap density ≈ 0.5 fF/um² at 16nm.
#
#   [IMEC_HB] imec, "Wafer-to-wafer hybrid bonding pushing boundaries toward
#            400nm interconnect pitch," IEDM 2023 / imec press release.
#            https://www.imec-int.com/en/articles/wafer-wafer-hybrid-bonding-pushing-boundaries-400nm-interconnect-pitch
#            → Hybrid bonding clearance fractions used in _HYBRID_BONDING_RULES.
# ---------------------------------------------------------------------------

_AREA_DEFAULTS: Dict[str, float] = {
    # Passive component densities
    # cap_mim_fF_per_um2: 65nm double-MIM ≈ 2 fF/um²  [CAP65]
    "cap_mim_fF_per_um2":     2.0,
    # res_poly_ohm_per_sq: unsilicided poly sheet R ≈ 200–400 Ω/□ at 65nm  [RES65]
    # NOTE: exact value is PDK-specific (NDA); override via area_hidden if known.
    "res_poly_ohm_per_sq":    300.0,
    "res_min_width_um":       0.4,    # minimum R strip width (um); PDK DRC value
    "res_bias_ohm_per_sq":    1500.0, # high-R poly for Thevenin bias (MΩ-class); PDK-specific
    # esd_diode_fF_per_um2: planar bulk CMOS ESD diode ≈ 0.3 fF/um²  [ESD_SI]
    "esd_diode_fF_per_um2":   0.3,
    "pad_cap_fF_per_um2":     2.0,    # on-die landing pad stack cap density; matches MIM  [CAP65]

    # Layout / routing rules — consistent with published 65nm BEOL node descriptions.
    # Exact DRC values are PDK-specific (NDA); override via area_hidden.
    "metal_spacing_um":       0.20,   # M2+ min spacing at 65nm
    "metal_track_um":         0.20,   # M2+ min width at 65nm

    # nwell_pwell_gap_um: minimum n-well to p-well spacing; PDK DRC value (NDA).
    # 0.60 um is a representative value for 65nm bulk CMOS.
    "nwell_pwell_gap_um":     0.60,

    # Margins
    "active_margin_frac":     0.30,   # active_area *= (1 + frac); covers contacts, extensions, taps
    "pad_access_overhead_um": 2.0,    # extra height above landing pad for routing access
}

# Per-PDK overrides (only keys that differ from the 65nm-class base above).
# The public framework ships defaults for FreePDK45 only; for any other PDK,
# supply the densities/geometry in that config's "area_hidden" section (the
# config value always wins over these built-in defaults).
_AREA_DEFAULTS_BY_NODE: Dict[str, Dict[str, float]] = {
    # ------------------------------------------------------------------ FreePDK45 (open 45nm bulk CMOS)
    # FreePDK45 is open-source, so the geometry values below are taken DIRECTLY
    # from the kit's published design rules and the OSU standard-cell LEF:
    #   - osu_soc/lib/files/gscl45nm.lef
    #   - ncsu_basekit/techfile/calibre/calibreDRC.rul
    # Values marked "(estimate)" are NOT in the kit: FreePDK45 is a digital-only
    # PDK with no MIM cap, precision/poly resistor, ESD diode, or RDL/pad layer,
    # so those remain 45nm-class engineering estimates (override via area_hidden).
    "freepdk45": {
        # ---- Documented from the open PDK ----
        # Signal routing on M2/M3: WIDTH 0.07, SPACING 0.07  [gscl45nm.lef metal2/3]
        # (M1-M3 routing PITCH is 0.19 incl. via landing; 0.07/0.07 is the DRC
        #  line+space minimum — a tight lower bound for the routing track pitch.)
        "metal_track_um":         0.07,
        "metal_spacing_um":       0.07,
        # n-well to p-well external spacing = 0.225 um
        #   [calibreDRC.rul: "EXTERNAL nwell pwell < 0.225"]
        "nwell_pwell_gap_um":     0.225,
        # Active/diffusion margin (contacts, poly endcaps, well taps) at 45nm bulk;
        # consistent with INVX1 = 0.57 x 2.47 um cell area  [gscl45nm.lef MACRO INVX1].
        "active_margin_frac":     0.25,
        "pad_access_overhead_um": 1.5,    # tighter BEOL stack than 65nm

        # ---- Estimates: layer not defined in FreePDK45 (see header note) ----
        # No MIM in FreePDK45 -> model on-die caps (EQ + pad) as MOM (metal-finger),
        # ~1.2 fF/um² at a 45nm BEOL.  (MOSCAP gate-ox alt. ~8.6 fF/um².)
        "cap_mim_fF_per_um2":     1.2,
        "pad_cap_fF_per_um2":     1.2,
        # No precision resistor layer; 45nm poly estimate.
        "res_poly_ohm_per_sq":    250.0,
        "res_min_width_um":       0.10,
        "res_bias_ohm_per_sq":    1200.0,
        # No ESD device; planar-bulk diode estimate.
        "esd_diode_fF_per_um2":   0.3,
    },
}

# ---------------------------------------------------------------------------
# Bump / pad sizing rules — physics-based, two assembly regimes.
# Mirrored in gen_lef.py (_BUMP_PAD_RULES); keep the two in sync.
#
# References:
#
#   [CUPILLAR] M. Dreiza et al., "Cu Pillar Bump Design Parameters for Flip
#              Chip Integration," IEEE ECTC 2021, doi:10.1109/ECTC32696.2021.9501659.
#              https://ieeexplore.ieee.org/document/9501659
#              → Cu pillar bump-to-bump clearance rules and UBM overhang (delta)
#              for silicon interposer and organic substrate regimes.
#
#   [FCPKG]    J. H. Lau et al., "Status and Outlooks of Flip Chip Technology,"
#              IPC APEX 2014.
#              https://www.circuitinsight.com/pdf/status_outlooks_flip_chip_technology_ipc.pdf
#              → Organic substrate clearance floors (g_bump_min, g_pad_min).
#
#   [HB_2UM]   imec, "imec demonstrates D2W hybrid bonding with 2-µm Cu
#              interconnect pad pitch," press release, 2021.
#              https://www.imec-int.com/en/press/imec-demonstrates-die-wafer-hybrid-bonding-cu-interconnect-pad-pitch-2mm
#
#   [HB_400NM] imec, "Wafer-to-wafer hybrid bonding pushing boundaries toward
#              400nm interconnect pitch," IEDM 2023 / imec article.
#              https://www.imec-int.com/en/articles/wafer-wafer-hybrid-bonding-pushing-boundaries-400nm-interconnect-pitch
#              → Hybrid bonding clearance fractions (g_bump_frac = 0.20×P,
#              g_pad_frac = 0.15×P) consistent with overlay < 150nm at 1 µm pitch.
# ---------------------------------------------------------------------------

# Pitch below which the hybrid-bonding (Cu–Cu direct bond) model is used.
# Above this threshold: Cu pillar / C4 with assembly-equipment-limited floors.
_HYBRID_BONDING_THRESHOLD_UM: float = 10.0

# Hybrid bonding (P < 10 µm): purely lithography-limited, no UBM overhang.
#   g_bump_frac : bump-to-bump alignment gap as fraction of pitch  [HB_400NM]
#   g_pad_frac  : pad-to-pad DRC spacing as fraction of pitch      [HB_400NM]
#   delta_um    : pillar-to-pad overhang = 0 (pad IS the Cu bond interface)
_HYBRID_BONDING_RULES = {
    "g_bump_frac": 0.20,
    "g_pad_frac":  0.15,
    "delta_um":    0.0,
}

# Cu pillar / C4 regime (P ≥ 10 µm): assembly-process-limited floors.
#   g_bump_frac / g_bump_min_um : bump-to-bump clearance (solder bridging)  [CUPILLAR]
#   g_pad_frac  / g_pad_min_um  : pad-to-pad DRC clearance (metal shorts)   [CUPILLAR]
#   delta_frac / delta_min_um / delta_max_um : Cu pillar overhang per side   [CUPILLAR]
_BUMP_PAD_RULES = {
    # 2.5D silicon interposer — fine-pitch Cu pillar, tight bonder tolerance  [CUPILLAR]
    "silicon": {
        "g_bump_frac": 0.10, "g_bump_min_um": 1.0,
        "g_pad_frac":  0.05, "g_pad_min_um":  0.5,
        "delta_frac":  0.03, "delta_min_um":  0.3, "delta_max_um": 5.0,
    },
    # Organic flip-chip / 2-D packaging — large C4, looser placement tolerance  [FCPKG]
    "organic": {
        "g_bump_frac": 0.25, "g_bump_min_um": 30.0,
        "g_pad_frac":  0.15, "g_pad_min_um":  15.0,
        "delta_frac":  0.10, "delta_min_um":   5.0, "delta_max_um": 25.0,
    },
    # Fallback for unrecognised pkg_type strings
    "default": {
        "g_bump_frac": 0.10, "g_bump_min_um": 1.0,
        "g_pad_frac":  0.05, "g_pad_min_um":  0.5,
        "delta_frac":  0.03, "delta_min_um":  0.3, "delta_max_um": 5.0,
    },
}

# Legal bump-map tokens
_VALID_TOKENS = {"tx", "rx", "vdd", "vss", "other", "-", "."}
_EMPTY_TOKENS = {"-", "."}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BumpMap:
    grid:        List[List[str]]   # grid[row][col]
    rows:        int
    cols:        int
    n_tx:        int
    n_rx:        int
    n_vdd:       int
    n_vss:       int
    n_other:     int
    n_empty:     int
    source_path: str
    split_col:   int = 0           # column boundary: cols 0..split_col-1 = TX half,
                                   #                  cols split_col..cols-1 = RX half


@dataclass
class LaneAreaBreakdown:
    # Raw component areas (um²) — before overhead stacking
    active_um2:          float
    eq_um2:              float   # TX only (0 for RX)
    term_um2:            float   # RX only (0 for TX)
    esd_um2:             float
    pad_cap_um2:         float
    routing_um2:         float
    isolation_um2:       float
    total_component_um2: float   # sum of above

    # Physical dimensions (um)
    bump_pitch_um:       float   # = cfg.link.bump_pitch_um (center-to-center, NOT cell width)
    lane_width_um:       float   # actual IO cell width (< bump_pitch_um)
    inter_cell_gap_um:   float   # bump_pitch - lane_width (routing keep-out between cells)
    active_stack_h_um:   float   # component stack height (silicon)
    active_overhead_um:  float   # pin band + power straps + gaps
    pad_band_h_um:       float   # landing pad + access margin
    lane_height_um:      float   # final lane height = max(active_total, pad_total)
    lane_area_um2:       float   # lane_width × lane_height

    # Bump / pad geometry — both circular (echoed from compute_bump_and_pad)
    bump_diam_um:        float   # physical Cu pillar / C4 bump diameter
    pad_diam_um:         float   # UBM / RDL landing pad diameter (< bump in Cu-pillar)

    # Height constraint
    constraint:          str     # "active-limited" | "pad-limited"
    active_total_h_um:   float   # active_stack_h + active_overhead
    pad_total_h_um:      float   # pad_band_h + active_overhead

    # 2-D density check (per-pitch-cell adequacy)
    active_sq_side_um:   float   # √(A_total) — equivalent single-layer square side
    active_sq_warn_um:   float   # (√2/2) × P ≈ 0.707P — advisory threshold
    active_sq_max_um:    float   # P − 1 µm — hard per-cell limit
    active_sq_util:      float   # active_sq_side / active_sq_max (0–1+)
    active_sq_status:    str     # "OK" | "DENSE" | "OVER BUDGET"


@dataclass
class IpBlockArea:
    name:             str                  # "txip" | "rxip"
    per_lane:         LaneAreaBreakdown
    lane_count:       int

    # Linear (no bump-map) layout: single row of lanes
    linear_width_um:  float                # lane_count · bump_pitch
    linear_height_um: float                # lane_height
    linear_area_um2:  float                # linear_width · linear_height

    # Bump-map derived block (0 when bump map disabled)
    block_width_um:   float
    block_height_um:  float
    block_area_um2:   float


@dataclass
class AreaResult:
    tx:                IpBlockArea
    rx:                IpBlockArea
    bump_map_enabled:  bool
    bump_map:          Optional[BumpMap]
    warnings:          List[str] = field(default_factory=list)

    def report(self) -> str:
        """
        Three-level area report:
          Level 1 — bump geometry (shared, printed once)
          Level 2 — single lane (TX and RX independently)
          Level 3 — full IP block: linear footprint + bump-map block (if enabled)
        """
        SEP = "  " + "-" * 58

        tp = self.tx.per_lane
        P  = tp.bump_pitch_um   # same pitch for TX and RX

        lines: List[str] = []
        lines.append("=== Area Model ===")
        lines.append("")

        # ── Level 1 : bump / pad geometry ────────────────────────────────────
        lines.append("  [Bump geometry]")
        lines.append(f"    pitch      : {P:.2f} um  (center-to-center)")
        lines.append(f"    bump diam  : {tp.bump_diam_um:.2f} um  (circle, Cu pillar / C4)")
        lines.append(f"    pad diam   : {tp.pad_diam_um:.2f} um  (UBM / RDL circle on top metal)")
        lines.append(f"    lane width : {tp.lane_width_um:.2f} um  (IO cell width < pitch)")
        lines.append(f"    cell gap   : {tp.inter_cell_gap_um:.2f} um  (pitch - lane_width, routing keep-out)")
        lines.append("")

        # ── Level 2 : single-lane breakdown (TX then RX) ─────────────────────
        def _lane_section(tag: str, blk: IpBlockArea) -> List[str]:
            p  = blk.per_lane
            L  = []

            L.append(f"  [Single lane — {tag}  ({blk.lane_count} lanes total)]")
            L.append("")

            # --- pad footprint (determines minimum height) ---
            access = p.pad_band_h_um - p.pad_diam_um
            L.append(f"    Pad footprint:")
            L.append(f"      pad band h  : {p.pad_band_h_um:.2f} um"
                     f"  (pad {p.pad_diam_um:.2f} + {access:.2f} um routing access)")
            L.append("")

            # --- active components (stacked under / beside the pad) ---
            L.append(f"    Active components:")
            L.append(f"      active        : {p.active_um2:8.2f} um^2")
            if p.eq_um2 > 0:
                L.append(f"      equalization  : {p.eq_um2:8.2f} um^2")
            if p.term_um2 > 0:
                L.append(f"      termination   : {p.term_um2:8.2f} um^2")
            L.append(f"      ESD           : {p.esd_um2:8.2f} um^2")
            L.append(f"      pad cap       : {p.pad_cap_um2:8.2f} um^2")
            L.append(f"      routing tracks: {p.routing_um2:8.2f} um^2")
            L.append(f"      nwell/pwell   : {p.isolation_um2:8.2f} um^2")
            L.append(f"      ─────────────────────────────")
            L.append(f"      total         : {p.total_component_um2:8.2f} um^2"
                     f"  ->  active stack h = {p.active_stack_h_um:.2f} um"
                     f"  (total / lane_width {p.lane_width_um:.2f} um)")
            L.append(f"      overhead      : {p.active_overhead_um:.2f} um"
                     f"  (IO pins + VDD/VSS power straps)")
            L.append("")

            # --- final lane size ---
            constraint_tag = "PAD-LIMITED" if p.constraint == "pad-limited" else "ACTIVE-LIMITED"
            L.append(f"    Lane size : {p.lane_width_um:.2f} um (W)  x  {p.lane_height_um:.2f} um (H)"
                     f"  [{constraint_tag}]")
            L.append(f"    Lane area : {p.lane_area_um2:.2f} um^2")

            # --- density / utilisation (always shown) ---
            status_tag = p.active_sq_status  # "OK" | "DENSE" | "OVER BUDGET"
            L.append(f"    Area util : s_fit = {p.active_sq_side_um:.2f} um"
                     f"  s_warn = {p.active_sq_warn_um:.2f} um"
                     f"  s_max = {p.active_sq_max_um:.2f} um"
                     f"  util = {p.active_sq_util:.1%}  [{status_tag}]")
            if status_tag == "DENSE":
                L.append(f"    NOTE: active area is dense (>50% of pitch cell)."
                         f" Verify layout DRC margins.")
            elif status_tag == "OVER BUDGET":
                L.append(f"    WARNING: active area exceeds lane width budget."
                         f" Increase bump pitch or split into multiple rows.")

            # --- height vs pitch warning ---
            if p.lane_height_um > P + 1e-6:
                L.append(f"    WARNING: lane height ({p.lane_height_um:.2f} um) exceeds bump pitch"
                         f" ({P:.2f} um). A multi-row bump map is required.")

            L.append("")
            return L

        lines += _lane_section("TX (txip)", self.tx)
        lines += _lane_section("RX (rxip)", self.rx)

        # ── Level 3 : full IP block footprint ────────────────────────────────
        lines.append(SEP)
        lines.append("")
        if self.bump_map_enabled and self.bump_map is not None:
            bm = self.bump_map
            lines.append("  [IP block — bump map]")
            lines.append(f"    source    : {bm.source_path}")
            lines.append(f"    grid      : {bm.rows} rows x {bm.cols} cols")
            lines.append(f"    split col : {bm.split_col}"
                         f"  (TX cols 0..{bm.split_col - 1}"
                         f" | RX cols {bm.split_col}..{bm.cols - 1})")
            lines.append(f"    bumps     : {bm.n_tx} tx  {bm.n_rx} rx"
                         f"  |  {bm.n_vdd} vdd  {bm.n_vss} vss  {bm.n_other} other"
                         f"  ({bm.n_empty} empty)")
            lines.append(f"    txip size : {self.tx.block_width_um:.2f} um (W)"
                         f"  x  {self.tx.block_height_um:.2f} um (H)"
                         f"  =  {self.tx.block_area_um2:.2f} um^2")
            lines.append(f"    rxip size : {self.rx.block_width_um:.2f} um (W)"
                         f"  x  {self.rx.block_height_um:.2f} um (H)"
                         f"  =  {self.rx.block_area_um2:.2f} um^2")
            total_w = self.tx.block_width_um + self.rx.block_width_um
            total_a = self.tx.block_area_um2 + self.rx.block_area_um2
            lines.append(f"    total IP  : {total_w:.2f} um (W)"
                         f"  x  {self.tx.block_height_um:.2f} um (H)"
                         f"  =  {total_a:.2f} um^2")
        else:
            lines.append("  [IP block — linear layout (no bump map, single row per IP)]")
            lines.append(f"    TX : {self.tx.lane_count} lanes"
                         f"  ->  {self.tx.linear_width_um:.2f} um (W)"
                         f"  x  {self.tx.linear_height_um:.2f} um (H)"
                         f"  =  {self.tx.linear_area_um2:.2f} um^2")
            lines.append(f"    RX : {self.rx.lane_count} lanes"
                         f"  ->  {self.rx.linear_width_um:.2f} um (W)"
                         f"  x  {self.rx.linear_height_um:.2f} um (H)"
                         f"  =  {self.rx.linear_area_um2:.2f} um^2")

        if self.warnings:
            lines.append("")
            for w in self.warnings:
                lines.append(f"  WARNING: {w}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# cfg helpers
# ---------------------------------------------------------------------------

def _get_area_hidden(cfg) -> Dict[str, float]:
    """Return the area_hidden density dict, starting from PDK-aware defaults.

    Resolution order (later wins):
      1. _AREA_DEFAULTS            — 65nm-class reference values
      2. _AREA_DEFAULTS_BY_NODE    — node-specific overrides (keyed by cfg.process.node)
      3. cfg.area_hidden           — per-config explicit overrides (user-tunable)
    """
    out = dict(_AREA_DEFAULTS)

    # Apply PDK-specific defaults based on process node
    node = ""
    proc = getattr(cfg, "process", None)
    if proc is not None:
        node = str(getattr(proc, "node", "") or "").lower().strip()
    node_overrides = _AREA_DEFAULTS_BY_NODE.get(node, {})
    out.update(node_overrides)

    # Apply explicit cfg.area_hidden overrides
    ah = getattr(cfg, "area_hidden", None)
    if ah is not None:
        for k in list(_AREA_DEFAULTS.keys()):
            v = getattr(ah, k, None)
            if v is not None:
                out[k] = float(v)
    return out


def _get_layout(cfg) -> Tuple[bool, Optional[str]]:
    """Return (bump_map_enabled, bump_map_file_path_or_None)."""
    lay = getattr(cfg, "layout", None)
    if lay is None:
        return (False, None)
    enabled = bool(getattr(lay, "bump_map_enabled", False))
    path    = getattr(lay, "bump_map_file", None)
    return (enabled, path)


def _normalize_pkg_type(pkg_type: str) -> str:
    t = pkg_type.lower()
    if any(s in t for s in ("silicon", "interposer")):
        return "silicon"
    if "organic" in t:
        return "organic"
    return "default"


# ---------------------------------------------------------------------------
# Bump / pad sizing (public so gen_lef.py can share the same logic)
# ---------------------------------------------------------------------------

def compute_bump_and_pad(
    bump_pitch_um:             float,
    bump_diam_scale:           float,
    pkg_type_key:              str,
    bump_diameter_um_override: Optional[float] = None,
) -> Tuple[float, float]:
    """
    Return (bump_diam_um, pad_diam_um) — both circular diameters.

    Two assembly regimes selected by bump_pitch_um:

    P < _HYBRID_BONDING_THRESHOLD_UM (10 µm) — hybrid bonding (Cu–Cu direct):
        Clearances scale purely as fractions of pitch (lithography-limited).
        No UBM overhang: pad_diam = bump_diam.

    P >= 10 µm — Cu pillar / C4:
        Clearances = max(process floor, frac × P).
        pad_diam = bump_diam − 2 × delta   (Cu pillar overhangs pad by delta/side).

    See module docstring Section 1 for full derivation and example values.
    """
    P = float(bump_pitch_um)

    # ---- bump diameter (same formula for both regimes) ----
    d_bump = (
        float(bump_diameter_um_override)
        if bump_diameter_um_override is not None
        else bump_diam_scale * P
    )

    if P < _HYBRID_BONDING_THRESHOLD_UM:
        # --- Hybrid bonding regime ---
        g_bump = _HYBRID_BONDING_RULES["g_bump_frac"] * P
        g_pad  = _HYBRID_BONDING_RULES["g_pad_frac"]  * P
        d_bump = min(d_bump, P - g_bump)
        d_bump = max(d_bump, 0.1)            # absolute floor (sub-µm possible)
        d_pad  = min(d_bump, P - g_pad)      # pad = bump (no overhang)
    else:
        # --- Cu pillar / C4 regime ---
        rules  = _BUMP_PAD_RULES.get(pkg_type_key, _BUMP_PAD_RULES["default"])
        g_bump = max(rules["g_bump_min_um"], rules["g_bump_frac"] * P)
        g_pad  = max(rules["g_pad_min_um"],  rules["g_pad_frac"]  * P)
        delta  = min(rules["delta_max_um"],
                     max(rules["delta_min_um"], rules["delta_frac"] * P))

        d_bump = min(d_bump, P - g_bump)
        d_bump = max(d_bump, 1.0)

        d_pad  = d_bump - 2.0 * delta        # pillar overhangs pad by delta/side
        d_pad  = min(d_pad, P - g_pad)       # pad clearance cap
        d_pad  = max(d_pad, d_bump * 0.5)    # sanity floor: pad ≥ 50 % of bump

    return round(d_bump, 3), round(d_pad, 3)


# ---------------------------------------------------------------------------
# Access-overhead model (shared with gen_lef.py — per-lane vertical overhead)
# ---------------------------------------------------------------------------

def _strap_h_um(bump_pitch_um: float) -> float:
    return min(1.50, max(0.10, bump_pitch_um * 0.15))


def _pin_dim_um(bump_pitch_um: float) -> float:
    return min(0.50, max(0.18, bump_pitch_um * 0.05))


def _active_overhead_um(bump_pitch_um: float) -> float:
    """Vertical overhead above the active stack for core-side pins + VDD/VSS straps."""
    strap_h = _strap_h_um(bump_pitch_um)
    pin_h   = _pin_dim_um(bump_pitch_um)
    return pin_h + 0.20 + strap_h + 0.50 + strap_h + 0.50


# ---------------------------------------------------------------------------
# Passive area helpers
# ---------------------------------------------------------------------------

def _cap_area_um2(cap_fF: float, density_fF_per_um2: float) -> float:
    if cap_fF <= 0 or density_fF_per_um2 <= 0:
        return 0.0
    return cap_fF / density_fF_per_um2


def _res_area_um2(r_ohm: float, sheet_ohm_per_sq: float, width_um: float) -> float:
    """
    Area of a rectangular poly-R strip realising r_ohm.
    n_squares = r_ohm / sheet_R; length = n_squares · width; area = length · width.
    """
    if r_ohm <= 0 or sheet_ohm_per_sq <= 0 or width_um <= 0:
        return 0.0
    n_squares = r_ohm / sheet_ohm_per_sq
    length    = n_squares * width_um
    return length * width_um


# ---------------------------------------------------------------------------
# Bump map loader
# ---------------------------------------------------------------------------

def load_bump_map(path: str, expected_lane_count: int) -> BumpMap:
    """
    Parse a plain-text bump map file.

    Tokens (case-insensitive): tx, rx, vdd, vss, other, - (empty), . (empty).
    Separator: whitespace and/or commas.  '#' starts a line comment.
    Blank lines are ignored.  Non-empty rows may have different lengths;
    shorter rows are right-padded with '-' so the grid is rectangular.

    Validates:
      - file exists and non-empty
      - all tokens are legal
      - count('tx') == expected_lane_count
      - count('rx') == expected_lane_count
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"[area] bump map file not found: {path}\n"
            f"       Disable cfg.layout.bump_map_enabled or provide the file."
        )

    grid: List[List[str]] = []
    with open(path, "r") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            # Normalise separators: treat commas as whitespace
            line = line.replace(",", " ")
            toks = [t.strip().lower() for t in line.split() if t.strip()]
            for t in toks:
                if t not in _VALID_TOKENS:
                    raise ValueError(
                        f"[area] bump map {path}:{line_no}: invalid token '{t}'. "
                        f"Allowed: {sorted(_VALID_TOKENS)}"
                    )
            grid.append(toks)

    if not grid:
        raise ValueError(f"[area] bump map {path} contains no data rows.")

    # Right-pad to a rectangular grid
    cols = max(len(r) for r in grid)
    for r in grid:
        while len(r) < cols:
            r.append("-")
    rows = len(grid)

    n_tx = sum(r.count("tx")    for r in grid)
    n_rx = sum(r.count("rx")    for r in grid)
    n_vdd = sum(r.count("vdd")  for r in grid)
    n_vss = sum(r.count("vss")  for r in grid)
    n_other = sum(r.count("other") for r in grid)
    n_empty = sum(sum(1 for t in r if t in _EMPTY_TOKENS) for r in grid)

    if n_tx != expected_lane_count:
        raise ValueError(
            f"[area] bump map {path}: found {n_tx} 'tx' bumps, "
            f"expected {expected_lane_count} (cfg.link.lane_count)."
        )
    if n_rx != expected_lane_count:
        raise ValueError(
            f"[area] bump map {path}: found {n_rx} 'rx' bumps, "
            f"expected {expected_lane_count} (cfg.link.lane_count)."
        )

    # Compute TX/RX split column using centroid of TX vs RX bump columns.
    tx_col_sum = sum(c for r in grid for c, t in enumerate(r) if t == "tx")
    rx_col_sum = sum(c for r in grid for c, t in enumerate(r) if t == "rx")
    tx_centroid = tx_col_sum / max(n_tx, 1)
    rx_centroid = rx_col_sum / max(n_rx, 1)
    split_col = round((tx_centroid + rx_centroid) / 2.0)
    # Clamp to [1, cols-1] so neither half is empty
    split_col = max(1, min(split_col, cols - 1))

    return BumpMap(
        grid=grid, rows=rows, cols=cols,
        n_tx=n_tx, n_rx=n_rx, n_vdd=n_vdd, n_vss=n_vss,
        n_other=n_other, n_empty=n_empty, source_path=path,
        split_col=split_col,
    )


# ---------------------------------------------------------------------------
# Bump-map signal adjacency — coupling-cap topology from physical layout
# ---------------------------------------------------------------------------

@dataclass
class SignalPair:
    """One physically adjacent signal-lane pair for coupling-cap generation."""
    lane_i:         int     # lower lane index (reading order through the grid)
    lane_j:         int     # higher lane index
    dist_pitches:   float   # Euclidean distance in bump-pitch units
    shielded:       bool    # True if a VDD/VSS bump sits between the pair


def get_signal_pairs(
    bm: BumpMap,
    kind: str,
    max_dist_pitches: float = 2.5,
) -> List[SignalPair]:
    """
    Compute physically adjacent signal-lane pairs from a bump map.

    Parameters
    ----------
    bm : BumpMap
        Parsed bump map grid.
    kind : "tx" or "rx"
        Which signal type to analyse.
    max_dist_pitches : float
        Maximum Euclidean distance (in bump pitches) to consider a pair
        coupled.  Default 2.5 covers nearest and second-nearest neighbours
        in typical checkerboard layouts.

    Returns
    -------
    List[SignalPair]
        Sorted by (dist_pitches, lane_i, lane_j).  Each unordered pair
        appears exactly once (lane_i < lane_j).

    Lane indices are assigned in reading order: row 0 left-to-right first,
    then row 1, etc. — the same order ``load_bump_map`` counts them.
    """
    # 1. Collect (row, col) for each lane in reading order
    positions: List[Tuple[int, int]] = []
    for r, row in enumerate(bm.grid):
        for c, tok in enumerate(row):
            if tok == kind:
                positions.append((r, c))

    n = len(positions)
    pairs: List[SignalPair] = []

    for i in range(n):
        r_i, c_i = positions[i]
        for j in range(i + 1, n):
            r_j, c_j = positions[j]
            dr = abs(r_i - r_j)
            dc = abs(c_i - c_j)
            d = math.sqrt(dr * dr + dc * dc)
            if d > max_dist_pitches:
                continue

            # Shielding check: walk the cells on the straight line between
            # the two bumps and look for VDD/VSS.  For axis-aligned pairs
            # (same row or same column) check every cell between them.  For
            # diagonal pairs check the midpoint cell(s).
            shielded = False
            if dr == 0:
                # same row — check every column between c_i and c_j
                lo, hi = min(c_i, c_j), max(c_i, c_j)
                for cc in range(lo + 1, hi):
                    if bm.grid[r_i][cc] in ("vdd", "vss"):
                        shielded = True
                        break
            elif dc == 0:
                # same column — check every row between r_i and r_j
                lo, hi = min(r_i, r_j), max(r_i, r_j)
                for rr in range(lo + 1, hi):
                    if bm.grid[rr][c_i] in ("vdd", "vss"):
                        shielded = True
                        break
            else:
                # diagonal — check midpoint cell(s)
                mid_r = (r_i + r_j) / 2.0
                mid_c = (c_i + c_j) / 2.0
                # check the 1 or 2 integer cells closest to the midpoint
                for rr in {int(math.floor(mid_r)), int(math.ceil(mid_r))}:
                    for cc in {int(math.floor(mid_c)), int(math.ceil(mid_c))}:
                        if 0 <= rr < bm.rows and 0 <= cc < bm.cols:
                            if bm.grid[rr][cc] in ("vdd", "vss"):
                                shielded = True

            pairs.append(SignalPair(
                lane_i=i, lane_j=j,
                dist_pitches=round(d, 4),
                shielded=shielded,
            ))

    pairs.sort(key=lambda p: (p.dist_pitches, p.lane_i, p.lane_j))
    return pairs


# ---------------------------------------------------------------------------
# Per-lane area computation
# ---------------------------------------------------------------------------

def _lane_breakdown(
    kind:            str,    # "tx" or "rx"
    cfg,
    ch_result,
    stage_sizes:     List[Tuple[float, float]],
    l_um:            float,
    n_signal_tracks: int,
    passives_um2:    float,   # EQ (TX) or termination (RX) silicon area
    eq_um2:          float,
    term_um2:        float,
    density:         Dict[str, float],
    bump_diam:       float,
    pad_diam:        float,   # circular pad diameter (from compute_bump_and_pad)
    lane_width_frac: Optional[float] = None,  # None = auto (pad-driven)
) -> LaneAreaBreakdown:
    import math
    bump_pitch = float(cfg.link.bump_pitch_um)

    # ---- IO cell width (lane_width < bump_pitch) — Section 3 ----
    if lane_width_frac is not None:
        # Manual mode: explicit fraction of pitch
        lane_width = float(lane_width_frac) * bump_pitch
    else:
        # Auto mode: pad-driven — one metal-spacing margin per side
        lane_width = pad_diam + 2.0 * density["metal_spacing_um"]

    if lane_width > bump_pitch:
        raise ValueError(
            f"[area] lane_width ({lane_width:.3f} µm) > bump_pitch ({bump_pitch:.3f} µm). "
            f"Set lane_width_frac ≤ 1.0 or leave it null for auto (pad-driven)."
        )
    inter_cell_gap = bump_pitch - lane_width

    # Active transistor silicon
    active_raw = sum((wn + wp) * l_um for wn, wp in stage_sizes)
    active_um2 = active_raw * (1.0 + density["active_margin_frac"])

    # ESD diodes — sized by capacitance density (see Section 2d)
    esd_um2 = _cap_area_um2(
        float(getattr(ch_result, "esd_C_fF", 0.0)),
        density["esd_diode_fF_per_um2"],
    )

    # On-die landing-pad capacitor area (see Section 2e)
    pad_cap_um2 = _cap_area_um2(
        float(getattr(ch_result, "pad_chiplet_C_fF", 0.0)),
        density["pad_cap_fF_per_um2"],
    )

    # Routing: signal tracks + 2 power tracks (see Section 2f)
    n_tracks    = n_signal_tracks + 2
    routing_um2 = n_tracks * (density["metal_spacing_um"] + density["metal_track_um"]) * lane_width

    # Nwell/pwell isolation bands (see Section 2g)
    isolation_um2 = 2.0 * density["nwell_pwell_gap_um"] * lane_width

    total_component = (
        active_um2 + passives_um2 + esd_um2 + pad_cap_um2 + routing_um2 + isolation_um2
    )

    # ---- 1-D lane height (strip model, Section 3) ----
    active_stack_h = total_component / lane_width
    overhead_h     = _active_overhead_um(bump_pitch)
    pad_band_h     = pad_diam + float(density["pad_access_overhead_um"])

    active_total_h = active_stack_h + overhead_h
    pad_total_h    = pad_band_h     + overhead_h  # overhead band exists on both sides

    lane_height = max(active_total_h, pad_total_h)
    lane_area   = lane_width * lane_height
    constraint  = "active-limited" if active_total_h >= pad_total_h else "pad-limited"

    # ---- 2-D density check (Section 4) ----
    _edge_margin  = 0.5                              # DRC margin per side (µm)
    s_fit         = math.sqrt(total_component)
    s_warn        = (math.sqrt(2.0) / 2.0) * bump_pitch   # ≈ 0.707 × P
    s_max         = max(lane_width - 2.0 * _edge_margin, 0.01)   # IO cell, not pitch
    active_sq_util = s_fit / s_max
    if s_fit <= s_warn:
        sq_status = "OK"
    elif s_fit <= s_max:
        sq_status = "DENSE"
    else:
        sq_status = "OVER BUDGET"

    return LaneAreaBreakdown(
        active_um2          = round(active_um2, 3),
        eq_um2              = round(eq_um2, 3),
        term_um2            = round(term_um2, 3),
        esd_um2             = round(esd_um2, 3),
        pad_cap_um2         = round(pad_cap_um2, 3),
        routing_um2         = round(routing_um2, 3),
        isolation_um2       = round(isolation_um2, 3),
        total_component_um2 = round(total_component, 3),
        bump_pitch_um       = round(bump_pitch, 3),
        lane_width_um       = round(lane_width, 3),
        inter_cell_gap_um   = round(inter_cell_gap, 3),
        active_stack_h_um   = round(active_stack_h, 3),
        active_overhead_um  = round(overhead_h, 3),
        pad_band_h_um       = round(pad_band_h, 3),
        lane_height_um      = round(lane_height, 3),
        lane_area_um2       = round(lane_area, 3),
        bump_diam_um        = round(bump_diam, 3),
        pad_diam_um         = round(pad_diam, 3),
        constraint          = constraint,
        active_total_h_um   = round(active_total_h, 3),
        pad_total_h_um      = round(pad_total_h, 3),
        active_sq_side_um   = round(s_fit, 3),
        active_sq_warn_um   = round(s_warn, 3),
        active_sq_max_um    = round(s_max, 3),
        active_sq_util      = round(active_sq_util, 4),
        active_sq_status    = sq_status,
    )


def _tx_passive_area(tx_result, density: Dict[str, float]) -> Tuple[float, float]:
    """Return (eq_um2, eq_um2) — TX equalization R+C area."""
    if not bool(getattr(tx_result, "use_equalization", False)):
        return (0.0, 0.0)
    r_eq = float(getattr(tx_result, "R_eq_ohm", 0.0))
    c_eq = float(getattr(tx_result, "C_eq_fF", 0.0))
    r_area = _res_area_um2(r_eq, density["res_poly_ohm_per_sq"], density["res_min_width_um"])
    c_area = _cap_area_um2(c_eq, density["cap_mim_fF_per_um2"])
    eq_um2 = r_area + c_area
    return (eq_um2, eq_um2)


def _rx_passive_area(rx_result, density: Dict[str, float]) -> Tuple[float, float]:
    """Return (term_um2, term_um2) — RX termination R + bias R + C_ac area."""
    if not bool(getattr(rx_result, "use_termination", False)):
        return (0.0, 0.0)
    r_term = float(getattr(rx_result, "r_rx_ohm", 0.0))
    c_ac_pF = float(getattr(rx_result, "c_ac_pF", 0.0))
    r_bias_hi = float(getattr(rx_result, "r_bias_hi_ohm", 0.0))
    r_bias_lo = float(getattr(rx_result, "r_bias_lo_ohm", 0.0))

    r_term_area = _res_area_um2(r_term, density["res_poly_ohm_per_sq"], density["res_min_width_um"])
    c_ac_area   = _cap_area_um2(c_ac_pF * 1000.0, density["cap_mim_fF_per_um2"])  # pF → fF
    r_bias_area = (
        _res_area_um2(r_bias_hi, density["res_bias_ohm_per_sq"], density["res_min_width_um"])
      + _res_area_um2(r_bias_lo, density["res_bias_ohm_per_sq"], density["res_min_width_um"])
    )
    term_um2 = r_term_area + c_ac_area + r_bias_area
    return (term_um2, term_um2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute(cfg,
            tx_result,
            rx_result,
            ch_result,
            eq_result=None,
            term_result=None,
            run_dir: Optional[str] = None) -> AreaResult:
    """
    Compute TX/RX IP area and dimensions.

    Reads per-lane stage sizes from tx_result / rx_result, passive values from
    tx_result / rx_result (EQ / termination), and parasitic caps (ESD, pad)
    from ch_result.  Does NOT include channel / interposer / bump components.

    Writes area_report.txt and area.csv to run_dir when provided.
    """
    lk  = cfg.link
    tx  = cfg.transistor
    ch  = cfg.channel

    density = _get_area_hidden(cfg)
    bump_map_enabled, bump_map_path = _get_layout(cfg)

    pkg_key = _normalize_pkg_type(lk.pkg_type)
    bump_diam, pad_diam = compute_bump_and_pad(
        bump_pitch_um             = float(lk.bump_pitch_um),
        bump_diam_scale           = float(getattr(ch, "bump_diameter_scale", 0.64)),
        pkg_type_key              = pkg_key,
        bump_diameter_um_override = getattr(ch, "bump_diameter_um", None),
    )

    # ----- TX stage sizes -----
    tx_inv = list(getattr(tx_result, "inverter_sizes", []) or [])
    if not tx_inv:
        # Fallback: minimum-sized 2-stage chain from config
        tx_inv = [(tx.w_n_um, tx.w_p_um), (tx.w_n_um, tx.w_p_um)]
    tx_n_sig = len(tx_inv) + 2 + (1 if getattr(tx_result, "use_equalization", False) else 0)

    # ----- RX stage sizes -----
    rx_stages = [
        (float(getattr(rx_result, "w_preamp_n_um", tx.w_n_um)),
         float(getattr(rx_result, "w_preamp_p_um", tx.w_p_um))),
        (float(getattr(rx_result, "w_buf_n_um",    tx.w_n_um * 3)),
         float(getattr(rx_result, "w_buf_p_um",    tx.w_p_um * 3))),
    ]
    rx_n_sig = len(rx_stages) + 2 + (1 if getattr(rx_result, "use_termination", False) else 0)

    # lane_width_frac — None means auto (pad-driven), float means manual fraction of P
    lane_width_frac: Optional[float] = None
    ah = getattr(cfg, "area_hidden", None)
    if ah is not None:
        v = getattr(ah, "lane_width_frac", None)
        if v is not None:
            lane_width_frac = float(v)

    # Passive silicon areas
    eq_um2, _ = _tx_passive_area(tx_result, density)
    term_um2, _ = _rx_passive_area(rx_result, density)

    tx_lane = _lane_breakdown(
        kind="tx", cfg=cfg, ch_result=ch_result,
        stage_sizes=tx_inv, l_um=float(tx.l_um),
        n_signal_tracks=tx_n_sig,
        passives_um2=eq_um2, eq_um2=eq_um2, term_um2=0.0,
        density=density, bump_diam=bump_diam, pad_diam=pad_diam,
        lane_width_frac=lane_width_frac,
    )
    rx_lane = _lane_breakdown(
        kind="rx", cfg=cfg, ch_result=ch_result,
        stage_sizes=rx_stages, l_um=float(tx.l_um),
        n_signal_tracks=rx_n_sig,
        passives_um2=term_um2, eq_um2=0.0, term_um2=term_um2,
        density=density, bump_diam=bump_diam, pad_diam=pad_diam,
        lane_width_frac=lane_width_frac,
    )

    # Linear (no bump map) layout: single row of lanes
    tx_linear_w = tx_lane.lane_width_um * lk.lane_count
    tx_linear_h = tx_lane.lane_height_um
    rx_linear_w = rx_lane.lane_width_um * lk.lane_count
    rx_linear_h = rx_lane.lane_height_um

    # ----- Bump map load + block dimensions -----
    warnings: List[str] = []
    bump_map: Optional[BumpMap] = None
    tx_block_w = tx_block_h = tx_block_a = 0.0
    rx_block_w = rx_block_h = rx_block_a = 0.0

    if bump_map_enabled:
        if not bump_map_path:
            raise ValueError(
                "[area] cfg.layout.bump_map_enabled is true but "
                "cfg.layout.bump_map_file is null. Provide a bump map .txt file."
            )
        bump_map = load_bump_map(bump_map_path, int(lk.lane_count))

        bump_pitch = float(lk.bump_pitch_um)
        lane_h_required = max(tx_lane.lane_height_um, rx_lane.lane_height_um)
        row_h = max(bump_pitch, lane_h_required)
        if lane_h_required > bump_pitch + 1e-6:
            warnings.append(
                f"lane height {lane_h_required:.2f} um exceeds bump pitch "
                f"{bump_pitch:.2f} um — rows were stretched vertically in the "
                f"block dimensions (row height = {row_h:.2f} um). "
                f"Consider a finer bump map (more rows of power/empty pads) "
                f"or a larger bump pitch."
            )

        block_h = bump_map.rows * row_h

        # Split TX and RX into separate halves using split_col.
        # TX half: cols 0 .. split_col-1
        # RX half: cols split_col .. cols-1
        sc = bump_map.split_col
        tx_block_w = sc * bump_pitch
        rx_block_w = (bump_map.cols - sc) * bump_pitch
        tx_block_h = rx_block_h = block_h
        tx_block_a = tx_block_w * tx_block_h
        rx_block_a = rx_block_w * rx_block_h

    tx_ip = IpBlockArea(
        name             = "txip",
        per_lane         = tx_lane,
        lane_count       = int(lk.lane_count),
        linear_width_um  = round(tx_linear_w, 3),
        linear_height_um = round(tx_linear_h, 3),
        linear_area_um2  = round(tx_linear_w * tx_linear_h, 3),
        block_width_um   = round(tx_block_w, 3),
        block_height_um  = round(tx_block_h, 3),
        block_area_um2   = round(tx_block_a, 3),
    )
    rx_ip = IpBlockArea(
        name             = "rxip",
        per_lane         = rx_lane,
        lane_count       = int(lk.lane_count),
        linear_width_um  = round(rx_linear_w, 3),
        linear_height_um = round(rx_linear_h, 3),
        linear_area_um2  = round(rx_linear_w * rx_linear_h, 3),
        block_width_um   = round(rx_block_w, 3),
        block_height_um  = round(rx_block_h, 3),
        block_area_um2   = round(rx_block_a, 3),
    )

    result = AreaResult(
        tx=tx_ip, rx=rx_ip,
        bump_map_enabled=bump_map_enabled,
        bump_map=bump_map,
        warnings=warnings,
    )

    if run_dir is not None:
        _write_reports(result, run_dir)

    return result


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def _write_reports(result: AreaResult, run_dir: str) -> None:
    os.makedirs(run_dir, exist_ok=True)

    # area_report.txt
    txt_path = os.path.join(run_dir, "area_report.txt")
    with open(txt_path, "w") as fh:
        fh.write(result.report())
        fh.write("\n")

    # area.csv — flat per-lane breakdown for both blocks
    csv_path = os.path.join(run_dir, "area.csv")
    header_fields = [
        "block",
        "lane_count",
        "active_um2", "eq_um2", "term_um2", "esd_um2",
        "pad_cap_um2", "routing_um2", "isolation_um2",
        "total_component_um2",
        "bump_pitch_um", "lane_width_um", "inter_cell_gap_um", "lane_height_um", "lane_area_um2",
        "active_stack_h_um", "active_overhead_um",
        "pad_band_h_um", "bump_diam_um", "pad_diam_um",
        "constraint",
        "linear_width_um", "linear_height_um", "linear_area_um2",
        "block_width_um",  "block_height_um",  "block_area_um2",
    ]
    def _row(blk: IpBlockArea) -> List[str]:
        p = blk.per_lane
        return [
            blk.name, str(blk.lane_count),
            f"{p.active_um2:.3f}", f"{p.eq_um2:.3f}", f"{p.term_um2:.3f}",
            f"{p.esd_um2:.3f}",    f"{p.pad_cap_um2:.3f}",
            f"{p.routing_um2:.3f}", f"{p.isolation_um2:.3f}",
            f"{p.total_component_um2:.3f}",
            f"{p.bump_pitch_um:.3f}", f"{p.lane_width_um:.3f}", f"{p.inter_cell_gap_um:.3f}",
            f"{p.lane_height_um:.3f}", f"{p.lane_area_um2:.3f}",
            f"{p.active_stack_h_um:.3f}", f"{p.active_overhead_um:.3f}",
            f"{p.pad_band_h_um:.3f}", f"{p.bump_diam_um:.3f}", f"{p.pad_diam_um:.3f}",
            p.constraint,
            f"{blk.linear_width_um:.3f}", f"{blk.linear_height_um:.3f}", f"{blk.linear_area_um2:.3f}",
            f"{blk.block_width_um:.3f}",  f"{blk.block_height_um:.3f}",  f"{blk.block_area_um2:.3f}",
        ]

    with open(csv_path, "w") as fh:
        fh.write(",".join(header_fields) + "\n")
        fh.write(",".join(_row(result.tx)) + "\n")
        fh.write(",".join(_row(result.rx)) + "\n")

    print(f"  [area] report  -> {txt_path}")
    print(f"  [area] csv     -> {csv_path}")
