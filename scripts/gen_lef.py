"""
gen_lef.py — LEF macro generator for the txip and rxip IP blocks.

Emits link_ip.lef (LEF 5.6 text) with txip and rxip MACROs (pin geometry, pad
placement, bounding box); no OpenROAD/ODB required.
"""

import os
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Layer / unit defaults keyed by cfg.process.node
# ---------------------------------------------------------------------------
# (signal_layer, pad_layer, power_layer, obs_layers, lef_units)
# Generic M1..M9 stack; override per-PDK by adding an entry keyed by
# cfg.process.node (lower-cased).  Unknown nodes use "default".
_PROCESS_DEFAULTS = {
    "freepdk45": ("M1", "M9", "M8",
                  ["M1","M2","M3","M4","M5","M6","M7","M8","M9"], 2000),
    "default": ("M1", "M9", "M8",
                ["M1","M2","M3","M4","M5","M6","M7","M8","M9"], 2000),
}

# ---------------------------------------------------------------------------
# Bump / pad sizing rules — mirrored from area.py; keep the two in sync.
# ---------------------------------------------------------------------------

_HYBRID_BONDING_THRESHOLD_UM: float = 10.0   # pitch below which hybrid model applies

# Hybrid bonding (P < 10 µm): lithography-limited, δ = 0 (pad = bump)
_HYBRID_BONDING_RULES = {
    "g_bump_frac": 0.20,
    "g_pad_frac":  0.15,
    "delta_um":    0.0,
}

# Cu pillar / C4 regime (P ≥ 10 µm): assembly-process-limited floors
_BUMP_PAD_RULES = {
    "silicon": {
        "g_bump_frac": 0.10, "g_bump_min_um": 1.0,
        "g_pad_frac":  0.05, "g_pad_min_um":  0.5,
        "delta_frac":  0.03, "delta_min_um":  0.3, "delta_max_um": 5.0,
    },
    "organic": {
        "g_bump_frac": 0.25, "g_bump_min_um": 30.0,
        "g_pad_frac":  0.15, "g_pad_min_um":  15.0,
        "delta_frac":  0.10, "delta_min_um":   5.0, "delta_max_um": 25.0,
    },
    "default": {
        "g_bump_frac": 0.10, "g_bump_min_um": 1.0,
        "g_pad_frac":  0.05, "g_pad_min_um":  0.5,
        "delta_frac":  0.03, "delta_min_um":  0.3, "delta_max_um": 5.0,
    },
}

# Remaining physical layout constants
_METAL_SPACING_UM  = 0.20   # minimum metal spacing (65nm-class M1/M2 design rules)
_METAL_TRACK_UM    = 0.20   # minimum metal track width
_NWELL_PWELL_GAP   = 0.60   # nwell-to-pwell isolation band width (um)


# ---------------------------------------------------------------------------
# Physical dimension model
# ---------------------------------------------------------------------------

def _normalize_pkg_type(pkg_type: str) -> str:
    """Map cfg.link.pkg_type string to a _PAD_DEFAULTS key."""
    t = pkg_type.lower()
    if any(s in t for s in ("silicon", "interposer")):
        return "silicon"
    if any(s in t for s in ("organic", "pkg_organic")):
        return "organic"
    return "default"


def _compute_bump_and_pad(
    bump_pitch_um:             float,
    bump_diam_scale:           float,
    pkg_type_key:              str,
    bump_diameter_um_override: Optional[float] = None,
) -> Tuple[float, float]:
    """
    Return (bump_diam_um, pad_diam_um) — both circular diameters.

    Mirrors area.compute_bump_and_pad(); see area.py Section 1 for full derivation.
    LEF PIN rects use the diameter as side length (bounding square of the circle).

    Two regimes:
      P < 10 µm : hybrid bonding — pad_diam = bump_diam (δ = 0)
      P ≥ 10 µm : Cu pillar / C4 — pad_diam = bump_diam − 2δ
    """
    P = float(bump_pitch_um)
    d_bump = (
        float(bump_diameter_um_override)
        if bump_diameter_um_override is not None
        else bump_diam_scale * P
    )

    if P < _HYBRID_BONDING_THRESHOLD_UM:
        g_bump = _HYBRID_BONDING_RULES["g_bump_frac"] * P
        g_pad  = _HYBRID_BONDING_RULES["g_pad_frac"]  * P
        d_bump = min(d_bump, P - g_bump)
        d_bump = max(d_bump, 0.1)
        d_pad  = min(d_bump, P - g_pad)
    else:
        rules  = _BUMP_PAD_RULES.get(pkg_type_key, _BUMP_PAD_RULES["default"])
        g_bump = max(rules["g_bump_min_um"], rules["g_bump_frac"] * P)
        g_pad  = max(rules["g_pad_min_um"],  rules["g_pad_frac"]  * P)
        delta  = min(rules["delta_max_um"],
                     max(rules["delta_min_um"], rules["delta_frac"] * P))
        d_bump = min(d_bump, P - g_bump)
        d_bump = max(d_bump, 1.0)
        d_pad  = d_bump - 2.0 * delta
        d_pad  = min(d_pad, P - g_pad)
        d_pad  = max(d_pad, d_bump * 0.5)

    return round(d_bump, 3), round(d_pad, 3)


def _compute_strap_h(bump_pitch_um: float) -> float:
    """VDD / VSS strap height, scaled to bump pitch."""
    return round(min(1.50, max(0.10, bump_pitch_um * 0.15)), 3)


def _compute_pin_dims(bump_pitch_um: float) -> Tuple[float, float]:
    """Core-side signal pin (width_um, height_um), scaled to bump pitch."""
    dim = round(min(0.50, max(0.18, bump_pitch_um * 0.05)), 3)
    return dim, dim


def _estimate_dimensions(
    lane_count:      int,
    bump_pitch_um:   float,
    bump_diam:       float,
    pad_diam:        float,
    stage_sizes:     List[Tuple[float, float]],   # [(w_n, w_p), ...] for all stages, 1 lane
    l_um:            float,
    n_signal_tracks: int,
) -> Tuple[float, float]:
    """
    Estimate physical bounding box (width_um, height_um) for one txip or rxip
    block, using the same area components as area.py.

    Width  = lane_count × bump_pitch_um
    Height = (active + ESD + routing + isolation + pin_overheads) / lane_width

    bump_diam : physical bump/pillar diameter — used for the ESD footprint area
    pad_diam  : metal landing-pad opening — defines the top-band reserved height
    """
    lane_width = bump_pitch_um
    strap_h    = _compute_strap_h(bump_pitch_um)
    pin_w, pin_h = _compute_pin_dims(bump_pitch_um)

    # Active transistor area per lane (all stages)
    active_area = sum((wn + wp) * l_um for wn, wp in stage_sizes)

    # ESD / bump pad area per lane — bump body footprint (matches area.py model)
    esd_area = bump_diam * max(bump_diam, 5.0)

    # Routing overhead: signal tracks + 2 power tracks
    n_tracks     = n_signal_tracks + 2
    routing_area = n_tracks * (_METAL_SPACING_UM + _METAL_TRACK_UM) * lane_width

    # nwell / pwell isolation bands
    isolation_area = 2.0 * _NWELL_PWELL_GAP * lane_width

    # Active height
    active_h = (active_area + esd_area + routing_area + isolation_area) / lane_width

    # Overhead for pin access bands
    #   bottom: signal pin height + 0.2 gap
    #   above : VSS strap + 0.5 gap + VDD strap + 0.5 gap
    #   top   : pad_diam band (metal landing pad — wider than bump body when pad_diam > bump_diam)
    pin_overhead = (pin_h + 0.20
                    + strap_h + 0.50
                    + strap_h + 0.50
                    + pad_diam)

    height = round(max(active_h + pin_overhead, pin_overhead * 2.0), 3)
    width  = round(lane_count * bump_pitch_um, 3)
    return width, height


# ---------------------------------------------------------------------------
# LEF text helpers
# ---------------------------------------------------------------------------

def _f(v: float) -> str:
    """Format a coordinate in microns to 3 decimal places."""
    return f"{v:.3f}"


def _pin_lef(name: str, direction: str, use: str,
             layer: str, x1: float, y1: float, x2: float, y2: float) -> List[str]:
    return [
        f"  PIN {name}",
        f"    DIRECTION {direction} ;",
        f"    USE {use} ;",
        f"    PORT",
        f"      LAYER {layer} ;",
        f"        RECT {_f(x1)} {_f(y1)} {_f(x2)} {_f(y2)} ;",
        f"    END",
        f"  END {name}",
        "",
    ]


# ---------------------------------------------------------------------------
# Per-macro MACRO block builder
# ---------------------------------------------------------------------------

def _build_macro_lef(
    macro_name:    str,
    lane_count:    int,
    width_um:      float,
    height_um:     float,
    bump_pitch_um: float,
    bump_diam:     float,
    pad_diam:      float,
    signal_layer:  str,
    pad_layer:     str,
    power_layer:   str,
    obs_layers:    List[str],
    sig_pins:      List[Tuple[str, str]],   # [(name, "INPUT"|"OUTPUT"), ...]
    pad_positions: Optional[List[Tuple[float, float]]] = None,   # [(cx, cy), ...] from bump map; None → linear top-edge
    vdd_positions: Optional[List[Tuple[float, float]]] = None,   # [(cx, cy), ...] per-bump cells; None → horizontal strap
    vss_positions: Optional[List[Tuple[float, float]]] = None,
) -> str:
    """
    Build one LEF MACRO block.

    sig_pins contains the ordered (pin_name, DIRECTION) tuples for the
    non-PAD signal pins (IN_i for txip, OUT_i for rxip).  They are placed
    on signal_layer at the bottom edge (y=0), centred horizontally within
    each bump lane.

    PAD_i pins are generated from lane_count and placed on pad_layer at
    the top edge (y=height_um).  The PIN rect is sized by pad_diam (the
    metal landing-pad opening) and centred on each bump lane.

    VDD and VSS power straps span the full width on power_layer, just
    above the signal pin band.

    bump_diam : physical bump body diameter (used for comment annotation)
    pad_diam  : metal landing-pad opening — sets PAD pin rect width and
                the height of the top band reserved for pads
    """
    assert len(sig_pins) == lane_count, (
        f"[gen_lef] {macro_name}: expected {lane_count} signal pins, "
        f"got {len(sig_pins)}"
    )

    pad_half   = pad_diam / 2.0
    strap_h    = _compute_strap_h(bump_pitch_um)
    pin_w, pin_h = _compute_pin_dims(bump_pitch_um)

    # Y positions (bottom edge = core side = y=0)
    sig_y1   = 0.0
    sig_y2   = pin_h

    vss_y1   = sig_y2 + 0.20   # small gap above signal pins
    vss_y2   = vss_y1 + strap_h

    vdd_y1   = vss_y2 + 0.50   # gap between VSS and VDD
    vdd_y2   = vdd_y1 + strap_h

    # Linear PAD band position (used when no bump-map pad_positions supplied).
    # Top band sized by landing-pad, not bump body.
    pad_y1   = height_um - pad_diam
    pad_y2   = height_um

    # Clamp signal pin width so it stays inside the lane
    eff_pin_w = min(pin_w, bump_pitch_um * 0.40)

    L: List[str] = []

    L.append(f"MACRO {macro_name}")
    L.append(f"  CLASS PAD ;")
    L.append(f"  ORIGIN 0.000 0.000 ;")
    L.append(f"  SIZE {_f(width_um)} BY {_f(height_um)} ;")
    L.append(f"  SYMMETRY X Y ;")
    L.append("")

    # ---- Signal pins (IN_i or OUT_i) at bottom edge ----
    # Anchor each signal pin horizontally under its corresponding PAD (so lane
    # routing stays local).  When a bump map provides PAD positions we use
    # those column centres; otherwise fall back to the linear (i+0.5)*pitch.
    for i, (pname, direction) in enumerate(sig_pins):
        if pad_positions is not None:
            cx = pad_positions[i][0]
        else:
            cx = (i + 0.5) * bump_pitch_um
        x1 = cx - eff_pin_w / 2.0
        x2 = cx + eff_pin_w / 2.0
        L.extend(_pin_lef(pname, direction, "SIGNAL",
                          signal_layer, x1, sig_y1, x2, sig_y2))

    # ---- PAD pins ----
    if pad_positions is not None:
        # Bump map: place each PAD at its grid (cx, cy)
        for i, (cx, cy) in enumerate(pad_positions):
            x1 = cx - pad_half
            x2 = cx + pad_half
            y1 = cy - pad_half
            y2 = cy + pad_half
            L.extend(_pin_lef(f"PAD_{i}", "INOUT", "SIGNAL",
                              pad_layer, x1, y1, x2, y2))
    else:
        # Linear: top edge, centred on each bump lane
        for i in range(lane_count):
            cx = (i + 0.5) * bump_pitch_um
            x1 = cx - pad_half
            x2 = cx + pad_half
            L.extend(_pin_lef(f"PAD_{i}", "INOUT", "SIGNAL",
                              pad_layer, x1, pad_y1, x2, pad_y2))

    # ---- VDD / VSS ----
    if vdd_positions is not None or vss_positions is not None:
        # Bump map: per-bump VDD/VSS pads on the pad_layer
        for i, (cx, cy) in enumerate(vdd_positions or []):
            x1 = cx - pad_half
            x2 = cx + pad_half
            y1 = cy - pad_half
            y2 = cy + pad_half
            L.extend(_pin_lef(f"VDD{i}" if i > 0 else "VDD", "INOUT", "POWER",
                              pad_layer, x1, y1, x2, y2))
        for i, (cx, cy) in enumerate(vss_positions or []):
            x1 = cx - pad_half
            x2 = cx + pad_half
            y1 = cy - pad_half
            y2 = cy + pad_half
            L.extend(_pin_lef(f"VSS{i}" if i > 0 else "VSS", "INOUT", "GROUND",
                              pad_layer, x1, y1, x2, y2))
    else:
        # Linear: horizontal power straps on the power_layer
        L.extend(_pin_lef("VDD", "INOUT", "POWER",
                          power_layer, 0.0, vdd_y1, width_um, vdd_y2))
        L.extend(_pin_lef("VSS", "INOUT", "GROUND",
                          power_layer, 0.0, vss_y1, width_um, vss_y2))

    # ---- Obstructions ----
    # Every routing layer is blocked except for the explicit pin access rects.
    # EDA tools treat declared PIN rects as accessible even when OBS covers the
    # same area, so this is the correct approach for hard/custom IP macros.
    L.append(f"  OBS")
    gridded = pad_positions is not None
    for layer in obs_layers:
        if gridded:
            # Gridded bump-map layout: PAD/VDD/VSS pins are scattered across
            # the block. Emit a single full-area OBS on every routing layer;
            # the explicit PIN rects still cut pin windows through it.
            L.append(f"    LAYER {layer} ;")
            L.append(f"      RECT 0.000 0.000 {_f(width_um)} {_f(height_um)} ;")
        elif layer == pad_layer:
            # Linear: only block below the landing-pad band
            if pad_y1 > 0.001:
                L.append(f"    LAYER {layer} ;")
                L.append(f"      RECT 0.000 0.000 {_f(width_um)} {_f(pad_y1)} ;")
        elif layer == power_layer:
            # Linear: block below VSS, between VSS and VDD, and above VDD up to pads
            L.append(f"    LAYER {layer} ;")
            if vss_y1 > 0.001:
                L.append(f"      RECT 0.000 0.000 {_f(width_um)} {_f(vss_y1)} ;")
            L.append(f"      RECT 0.000 {_f(vss_y2)} {_f(width_um)} {_f(vdd_y1)} ;")
            if vdd_y2 < pad_y1 - 0.001:
                L.append(f"      RECT 0.000 {_f(vdd_y2)} {_f(width_um)} {_f(pad_y1)} ;")
        else:
            # Full-area OBS on all other layers
            L.append(f"    LAYER {layer} ;")
            L.append(f"      RECT 0.000 0.000 {_f(width_um)} {_f(height_um)} ;")
    L.append(f"  END")
    L.append("")

    L.append(f"END {macro_name}")
    L.append("")

    return "\n".join(L)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(cfg, metrics, tx_result, rx_result, run_dir: str,
             area_result=None) -> None:
    """
    Write link_ip.lef to *run_dir*.

    The file contains two MACRO definitions:
        txip — N-lane TX with IN_i, PAD_i, VDD, VSS
        rxip — N-lane RX with PAD_i, OUT_i, VDD, VSS

    Parameters
    ----------
    cfg         : Config (from main.py) — uses process, link, channel, transistor sections
    metrics     : LinkMetrics (from get_metrics.py) — used only for header comments
    tx_result   : TxNetlistResult (from tx.py) — provides inverter_sizes, num_stages
    rx_result   : RxNetlistResult (from rx.py) — provides w_preamp_*, w_buf_* sizes
    run_dir     : top-level combo output directory; link_ip.lef is written here
    area_result : optional AreaResult from area.compute(). When supplied:
                    - per-lane / block dimensions come from the area model
                    - if area_result.bump_map_enabled, PAD / VDD / VSS pins are
                      placed at the exact grid positions from the bump map,
                      and both MACROs share the full IP bounding box.
    """
    lk  = cfg.link
    tx  = cfg.transistor
    ch  = cfg.channel

    # ---- Resolve layer names and units from process node ----
    node_key = cfg.process.node.lower()
    signal_layer, pad_layer, power_layer, obs_layers, lef_units = (
        _PROCESS_DEFAULTS.get(node_key, _PROCESS_DEFAULTS["default"])
    )

    # ---- Resolve bump diameter and metal landing-pad size ----
    pkg_key = _normalize_pkg_type(lk.pkg_type)
    bump_diam, pad_diam = _compute_bump_and_pad(
        bump_pitch_um             = lk.bump_pitch_um,
        bump_diam_scale           = ch.bump_diameter_scale,
        pkg_type_key              = pkg_key,
        bump_diameter_um_override = ch.bump_diameter_um,
    )

    # ---- TX inverter stage sizes (one lane) ----
    inv_sizes: List[Tuple[float, float]] = list(tx_result.inverter_sizes)
    if not inv_sizes:
        # Fallback: use the configured minimum transistor sizes as a 2-stage chain
        inv_sizes = [(tx.w_n_um, tx.w_p_um), (tx.w_n_um, tx.w_p_um)]

    # ---- RX fixed stage sizes: pre-amplifier + output buffer ----
    rx_sizes: List[Tuple[float, float]] = [
        (rx_result.w_preamp_n_um, rx_result.w_preamp_p_um),
        (rx_result.w_buf_n_um,    rx_result.w_buf_p_um),
    ]

    # Signal routing tracks per lane:
    #   TX: one track per inverter stage + IN net + equalization node (if any) + 1 margin
    #   RX: one track per amp stage + PAD net + out net + 1 margin
    n_sig_tx = len(inv_sizes) + 2 + (1 if tx_result.use_equalization else 0)
    n_sig_rx = len(rx_sizes)  + 2 + (1 if rx_result.use_termination  else 0)

    # ---- Determine bounding box dimensions + pad positions ----
    #
    # Preference order:
    #   1. area_result with bump map enabled → share block bbox, pad positions
    #      come from the grid.
    #   2. area_result without bump map     → linear layout, width/height from
    #      area_result.per_lane (lane_count × pitch, lane_height).
    #   3. no area_result                    → _estimate_dimensions fallback.
    tx_pad_positions = None
    rx_pad_positions = None
    vdd_positions    = None
    vss_positions    = None

    if area_result is not None and area_result.bump_map_enabled:
        bm = area_result.bump_map
        bump_pitch = float(lk.bump_pitch_um)
        tx_w = area_result.tx.block_width_um
        tx_h = area_result.tx.block_height_um
        rx_w = area_result.rx.block_width_um
        rx_h = area_result.rx.block_height_um
        row_h = tx_h / bm.rows  # uniform row height (may exceed bump_pitch)

        sc = bm.split_col          # TX half: cols 0..sc-1; RX half: cols sc..cols-1
        rx_x_offset = sc * bump_pitch  # subtracted from RX coords → rxip local origin

        tx_pad_positions = []
        rx_pad_positions = []
        tx_vdd_positions = []
        tx_vss_positions = []
        rx_vdd_positions = []
        rx_vss_positions = []
        # Row 0 is the top row in the text file; translate to LEF y where
        # y=0 is the bottom edge and y=height_um is the top edge.
        for r, row in enumerate(bm.grid):
            cy = tx_h - (r + 0.5) * row_h
            for c, tok in enumerate(row):
                cx = (c + 0.5) * bump_pitch
                is_tx_half = c < sc
                if tok == "tx":
                    tx_pad_positions.append((cx, cy))
                elif tok == "rx":
                    rx_pad_positions.append((cx - rx_x_offset, cy))
                elif tok == "vdd":
                    if is_tx_half:
                        tx_vdd_positions.append((cx, cy))
                    else:
                        rx_vdd_positions.append((cx - rx_x_offset, cy))
                elif tok == "vss":
                    if is_tx_half:
                        tx_vss_positions.append((cx, cy))
                    else:
                        rx_vss_positions.append((cx - rx_x_offset, cy))

        vdd_positions = tx_vdd_positions  # used by txip
        vss_positions = tx_vss_positions
    elif area_result is not None:
        tx_w = area_result.tx.linear_width_um
        tx_h = area_result.tx.linear_height_um
        rx_w = area_result.rx.linear_width_um
        rx_h = area_result.rx.linear_height_um
    else:
        tx_w, tx_h = _estimate_dimensions(
            lane_count      = lk.lane_count,
            bump_pitch_um   = lk.bump_pitch_um,
            bump_diam       = bump_diam,
            pad_diam        = pad_diam,
            stage_sizes     = inv_sizes,
            l_um            = tx.l_um,
            n_signal_tracks = n_sig_tx,
        )
        rx_w, rx_h = _estimate_dimensions(
            lane_count      = lk.lane_count,
            bump_pitch_um   = lk.bump_pitch_um,
            bump_diam       = bump_diam,
            pad_diam        = pad_diam,
            stage_sizes     = rx_sizes,
            l_um            = tx.l_um,
            n_signal_tracks = n_sig_rx,
        )

    # ---- Build signal pin lists ----
    tx_sig_pins = [(f"IN_{i}",  "INPUT")  for i in range(lk.lane_count)]
    rx_sig_pins = [(f"OUT_{i}", "OUTPUT") for i in range(lk.lane_count)]

    # ---- Generate MACRO text ----
    txip_lef = _build_macro_lef(
        macro_name    = "txip",
        lane_count    = lk.lane_count,
        width_um      = tx_w,
        height_um     = tx_h,
        bump_pitch_um = lk.bump_pitch_um,
        bump_diam     = bump_diam,
        pad_diam      = pad_diam,
        signal_layer  = signal_layer,
        pad_layer     = pad_layer,
        power_layer   = power_layer,
        obs_layers    = obs_layers,
        sig_pins      = tx_sig_pins,
        pad_positions = tx_pad_positions,
        vdd_positions = vdd_positions,
        vss_positions = vss_positions,
    )

    rxip_lef = _build_macro_lef(
        macro_name    = "rxip",
        lane_count    = lk.lane_count,
        width_um      = rx_w,
        height_um     = rx_h,
        bump_pitch_um = lk.bump_pitch_um,
        bump_diam     = bump_diam,
        pad_diam      = pad_diam,
        signal_layer  = signal_layer,
        pad_layer     = pad_layer,
        power_layer   = power_layer,
        obs_layers    = obs_layers,
        sig_pins      = rx_sig_pins,
        pad_positions = rx_pad_positions,
        vdd_positions = rx_vdd_positions if area_result is not None and area_result.bump_map_enabled else vdd_positions,
        vss_positions = rx_vss_positions if area_result is not None and area_result.bump_map_enabled else vss_positions,
    )

    # ---- Assemble LEF file ----
    total_delay_ns = (getattr(metrics, "tx_delay_rr_avg_ns", 0.0) or 0.0) + \
                     (getattr(metrics, "rx_delay_rr_avg_ns", 0.0) or 0.0)
    total_epb      = getattr(metrics, "E_total_pJ_per_bit", 0.0) or 0.0

    header = (
        "# LEF abstract for chiplet link IP  —  txip + rxip\n"
        f"# Process    : {cfg.process.node}  VDD={cfg.process.vdd}V\n"
        f"# Link       : {lk.pkg_type}  {lk.reach_mm} mm  "
                        f"{lk.bump_pitch_um} um pitch  "
                        f"{lk.data_rate_Gbps} Gb/s  "
                        f"{lk.lane_count} lanes\n"
        f"# Pad model  : bump_diam={bump_diam:.3f} um  pad_diam={pad_diam:.3f} um"
                        f"  ({pkg_key} pkg)\n"
        f"# Metrics    : total delay={total_delay_ns*1e3:.2f} ps  "
                        f"E/bit={total_epb:.4f} pJ\n"
        "# Generator  : chiplet_link_gen / gen_lef.py  (LEF 5.6)\n"
        "#\n"
        "\n"
        "VERSION 5.6 ;\n"
        "BUSBITCHARS \"[]\" ;\n"
        "DIVIDERCHAR \"/\" ;\n"
        "\n"
        "UNITS\n"
        f"  DATABASE MICRONS {lef_units} ;\n"
        "END UNITS\n"
        "\n"
    )

    lef_text = header + txip_lef + rxip_lef + "END LIBRARY\n"

    lib_dir  = os.path.join(run_dir, "link_library")
    os.makedirs(lib_dir, exist_ok=True)
    lef_path = os.path.join(lib_dir, "link_ip.lef")
    with open(lef_path, "w") as fh:
        fh.write(lef_text)

    print(f"  [gen_lef] pad model : bump_diam={bump_diam:.3f} um  "
          f"pad_diam={pad_diam:.3f} um  ({lk.bump_pitch_um} um pitch, {pkg_key})")
    if area_result is not None and area_result.bump_map_enabled:
        bm = area_result.bump_map
        print(f"  [gen_lef] bump map : {bm.rows}x{bm.cols} grid  "
              f"(split_col={bm.split_col}: TX cols 0..{bm.split_col-1}, "
              f"RX cols {bm.split_col}..{bm.cols-1})")
        print(f"  [gen_lef] bumps   : {bm.n_tx} tx, {bm.n_rx} rx, "
              f"{bm.n_vdd} vdd, {bm.n_vss} vss")
    print(f"  [gen_lef] txip : {tx_w:.3f} um wide x {tx_h:.3f} um tall  "
          f"({lk.lane_count} lanes @ {lk.bump_pitch_um} um pitch)")
    print(f"  [gen_lef] rxip : {rx_w:.3f} um wide x {rx_h:.3f} um tall  "
          f"({lk.lane_count} lanes @ {lk.bump_pitch_um} um pitch)")
    print(f"  [gen_lef] link_ip.lef -> {lef_path}")