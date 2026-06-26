"""
gen_verilog.py — behavioral Verilog generator for the TX and RX cells.

Writes tx.v and rx.v that structurally mirror the SPICE netlists, with the lane
count from cfg.link.lane_count.

Usage (main.py Stage 7):
    gen_verilog.generate(cfg, metrics, tx_result, rx_result, run_dir)
"""

import os


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(cfg, metrics, tx_result, rx_result, run_dir: str) -> None:
    """
    Write tx.v and rx.v into run_dir.

    Parameters
    ----------
    cfg        : Config          (from main.py)
    metrics    : MetricsResult   (from get_metrics.py)
    tx_result  : TxResult        (from tx.py)
    rx_result  : RxResult        (from rx.py)
    run_dir    : str
    """
    lib_dir   = os.path.join(run_dir, "link_library")
    os.makedirs(lib_dir, exist_ok=True)

    tx_path   = os.path.join(lib_dir, "tx.v")
    rx_path   = os.path.join(lib_dir, "rx.v")
    num_lanes = cfg.link.lane_count

    _write_tx(cfg, metrics, tx_result, tx_path, num_lanes)
    _write_rx(cfg, metrics, rx_result, rx_path, num_lanes)

    print(f"  [gen_verilog] tx.v -> {tx_path}")
    print(f"  [gen_verilog] rx.v -> {rx_path}")


# ---------------------------------------------------------------------------
# TX
# ---------------------------------------------------------------------------

def _write_tx(cfg, metrics, tx_result, path: str, num_lanes: int) -> None:
    # ---- pull sizing ----
    sizing     = getattr(tx_result, "sizing",       {})
    num_stages = sizing.get("num_stages",    2)
    inv_sizes  = sizing.get("inverter_sizes", [])

    r_pad_ohm  = getattr(tx_result, "r_pad_ohm",   cfg.termination_hidden.r_tx_ohm)
    c_load_fF  = getattr(tx_result, "c_load_fF",   0.0)
    eq_enabled = getattr(tx_result, "eq_enabled",  False)
    r_eq_ohm   = getattr(tx_result, "r_eq_ohm",    0.0)
    c_eq_fF    = getattr(tx_result, "c_eq_fF",     0.0)

    # ---- timing ----
    rise_ns = getattr(metrics, "tx_delay_rr_mid_ns", 0.0) or 0.0
    fall_ns = getattr(metrics, "tx_delay_ff_mid_ns", 0.0) or 0.0
    rise_ps = max(1, int(round(rise_ns * 1000)))
    fall_ps = max(1, int(round(fall_ns * 1000)))

    # ---- channel for comments ----
    R_ch  = getattr(metrics, "ch_r_total_ohm", 0.0) or 0.0
    C_ch  = getattr(metrics, "ch_c_total_fF",  0.0) or 0.0
    ch_ns = 0.69 * R_ch * C_ch * 1e-6

    # ---- power ----
    leak_nW = getattr(metrics, "tx_leak_avg_nW", 0.0) or 0.0
    sw_pJ   = getattr(metrics, "tx_sw_avg_pJ",   0.0) or 0.0
    epb_pJ  = getattr(metrics, "tx_epb_pJ",      0.0) or 0.0
    term_mW = getattr(metrics, "term_power_mW",  0.0) or 0.0

    link_tag   = _link_tag(cfg)
    r_pad_dly  = max(1, int(round(r_pad_ohm * c_load_fF * 1e-3)))
    eq_tau_ps  = max(1, int(round(r_eq_ohm * c_eq_fF * 1e-3))) if eq_enabled else 0

    L = []

    # ================================================================
    # tx_lane  —  single-lane sub-cell (mirrors SPICE subckt)
    # ================================================================
    L += [
        "// " + "=" * 62,
        f"// tx_lane -- single-lane TX cell",
        f"// Mirrors the .subckt tx (in VDD VSS PAD) SPICE cell",
        "// " + "=" * 62,
        "//",
        "// Topology:",
        f"//   in -> [inv chain x{num_stages}] -> pre_pad -> R_PAD({r_pad_ohm:.1f} ohm) -> PAD",
    ]
    if eq_enabled:
        L.append(f"//   Passive EQ: R_EQ={r_eq_ohm:.2f} ohm + C_EQ={c_eq_fF:.1f} fF at input")
    L.append("//")
    L.append("// Inverter chain:")
    for i, sz in enumerate(inv_sizes):
        if isinstance(sz, (list, tuple)) and len(sz) == 2:
            L.append(f"//   Stage {i+1}: NMOS w={sz[0]} um  PMOS w={sz[1]} um")
        else:
            L.append(f"//   Stage {i+1}: w={sz} um")
    L += [
        "//",
        "// Timing (Liberate, mid slew/load):",
        f"//   in->PAD rise={rise_ns:.4f} ns  fall={fall_ns:.4f} ns",
        f"//   Channel Elmore={ch_ns:.4f} ns  (R={R_ch:.2f} ohm, C={C_ch:.1f} fF)",
        "//",
        "// Power (alpha=0.5):",
        f"//   Leakage={leak_nW:.3f} nW  Switching={sw_pJ:.4f} pJ/trans",
        f"//   Energy/bit={epb_pJ:.4f} pJ/bit",
        f"//   Termination={term_mW:.4f} mW  "
        + ("(AC-coupled)" if cfg.termination_hidden.ac_coupled else "(DC)"),
        "// " + "=" * 62,
        "",
        "`timescale 1ps/1ps",
        "",
        "module tx_lane (",
        "    input  wire in,",
        "    inout  wire PAD,",
        "    input  wire VDD,",
        "    input  wire VSS",
        ");",
        "",
        "    // Internal nodes",
    ]
    for i in range(1, num_stages):
        L.append(f"    wire out{i};")
    L.append("    wire pre_pad;")
    if eq_enabled:
        L.append("    wire eq_n;")
    L.append("")

    # passive EQ
    if eq_enabled:
        L += [
            f"    // R_EQ={r_eq_ohm:.3f} ohm + C_EQ={c_eq_fF:.1f} fF  tau={eq_tau_ps} ps",
            f"    assign #({eq_tau_ps}) eq_n = in;",
            "",
        ]
        first_in = "eq_n"
    else:
        first_in = "in"

    # inverter chain
    L.append("    // Inverter chain")
    for i in range(num_stages):
        in_node  = first_in       if i == 0             else f"out{i}"
        out_node = f"out{i+1}"   if i < num_stages - 1 else "pre_pad"
        sz = inv_sizes[i] if i < len(inv_sizes) else None
        if sz is not None and isinstance(sz, (list, tuple)) and len(sz) == 2:
            sz_str = f"NMOS w={sz[0]} um  PMOS w={sz[1]} um"
        elif sz is not None:
            sz_str = f"w={sz} um"
        else:
            sz_str = "size unknown"
        L.append(f"    not u_inv{i+1} ({out_node}, {in_node});  // {sz_str}")
    L.append("")

    # R_PAD
    L += [
        f"    // R_PAD={r_pad_ohm:.1f} ohm  C_load={c_load_fF:.1f} fF  tau={r_pad_dly} ps",
        f"    assign #({r_pad_dly}) PAD = pre_pad;",
        "",
    ]

    # specify
    L += [
        "    // STA arc (Liberate, cell only)",
        "    specify",
        f"        ( posedge in => (PAD +: in) ) = ({rise_ps}, {fall_ps});",
        f"        ( negedge in => (PAD -: in) ) = ({fall_ps}, {rise_ps});",
        "    endspecify",
        "",
        "endmodule // tx_lane",
        "",
        "",
    ]

    # ================================================================
    # tx  —  N-lane top (flat instantiation, mirrors N-lane SPICE)
    # ================================================================
    L += [
        "// " + "=" * 62,
        f"// tx -- {num_lanes}-lane TX top  ({link_tag})",
        f"// Flat instantiation of {num_lanes} tx_lane cells.",
        f"// Port names match SPICE: in0..in{num_lanes-1}, PAD0..PAD{num_lanes-1}",
        "// " + "=" * 62,
        "",
        "module tx (",
    ]
    for i in range(num_lanes):
        L.append(f"    input  wire in{i},")
    for i in range(num_lanes):
        comma = "," if i < num_lanes - 1 else ""
        L.append(f"    inout  wire PAD{i}{comma}")
    L += [
        "    // VDD / VSS supplied as implicit globals in SPICE;",
        "    // kept here for LVS consistency",
        "    // input wire VDD,",
        "    // input wire VSS",
        ");",
        "",
        "    // supply rails",
        "    supply1 VDD;",
        "    supply0 VSS;",
        "",
        f"    // {num_lanes} lane instantiations",
    ]
    for i in range(num_lanes):
        L += [
            f"    tx_lane u_lane{i} (",
            f"        .in  (in{i}),",
            f"        .PAD (PAD{i}),",
            f"        .VDD (VDD),",
            f"        .VSS (VSS)",
            f"    );",
            "",
        ]
    L += [
        "endmodule // tx",
        "",
    ]

    _write_lines(path, L)


# ---------------------------------------------------------------------------
# RX
# ---------------------------------------------------------------------------

def _write_rx(cfg, metrics, rx_result, path: str, num_lanes: int) -> None:
    # ---- pull sizing ----
    sizing     = getattr(rx_result, "sizing",      {})
    num_stages = sizing.get("num_stages",    2)
    inv_sizes  = sizing.get("inverter_sizes", [])

    r_rx_ohm   = getattr(rx_result, "r_rx_ohm",   cfg.termination_hidden.r_rx_ohm)
    ac_coupled = getattr(rx_result, "ac_coupled",  cfg.termination_hidden.ac_coupled)
    c_ac_fF    = getattr(rx_result, "c_ac_fF",     0.0)

    # ---- timing ----
    rx_rise_ns = getattr(metrics, "rx_delay_rr_mid_ns", None)
    rx_fall_ns = getattr(metrics, "rx_delay_ff_mid_ns", None)
    tx_rise_ns = getattr(metrics, "tx_delay_rr_mid_ns", 0.0) or 0.0
    tx_fall_ns = getattr(metrics, "tx_delay_ff_mid_ns", 0.0) or 0.0
    sym_note   = ""
    if rx_rise_ns is None:
        rx_rise_ns = tx_rise_ns
        sym_note   = "  // mirrored from TX"
    if rx_fall_ns is None:
        rx_fall_ns = tx_fall_ns

    R_ch  = getattr(metrics, "ch_r_total_ohm", 0.0) or 0.0
    C_ch  = getattr(metrics, "ch_c_total_fF",  0.0) or 0.0
    ch_ns = 0.69 * R_ch * C_ch * 1e-6

    total_rise_ns = tx_rise_ns + ch_ns + rx_rise_ns
    total_fall_ns = tx_fall_ns + ch_ns + rx_fall_ns
    total_rise_ps = max(1, int(round(total_rise_ns * 1000)))
    total_fall_ps = max(1, int(round(total_fall_ns * 1000)))

    # ---- power ----
    leak_nW = getattr(metrics, "rx_leak_avg_nW", 0.0) or 0.0
    sw_pJ   = getattr(metrics, "rx_sw_avg_pJ",   0.0) or 0.0
    epb_pJ  = getattr(metrics, "rx_epb_pJ",      0.0) or 0.0
    term_mW = getattr(metrics, "term_power_mW",  0.0) or 0.0

    link_tag  = _link_tag(cfg)
    ac_tau_ps = max(1, int(round(r_rx_ohm * c_ac_fF * 1e-3))) if (ac_coupled and c_ac_fF > 0) else 0

    L = []

    # ================================================================
    # rx_lane  —  single-lane sub-cell
    # ================================================================
    L += [
        "// " + "=" * 62,
        f"// rx_lane -- single-lane RX cell",
        f"// Mirrors .subckt rx PAD VDD VSS DOUT from rx.py",
        "// " + "=" * 62,
        "//",
        "// Topology:",
    ]
    if ac_coupled:
        L += [
            f"//   PAD -> C_AC({c_ac_fF:.1f} fF) -> ac_n -> R_TERM({r_rx_ohm:.1f} ohm) -> VSS",
            f"//   ac_n -> [inv chain x{num_stages}] -> DOUT",
        ]
    else:
        L.append(f"//   PAD -> R_TERM({r_rx_ohm:.1f} ohm) -> [inv chain x{num_stages}] -> DOUT")
    L.append("//")
    L.append("// Inverter chain:")
    for i, sz in enumerate(inv_sizes):
        if isinstance(sz, (list, tuple)) and len(sz) == 2:
            L.append(f"//   Stage {i+1}: NMOS w={sz[0]} um  PMOS w={sz[1]} um")
        else:
            L.append(f"//   Stage {i+1}: w={sz} um")
    L += [
        "//",
        "// Timing (full link: TX + channel Elmore + RX cell):",
        f"//   TX   rise={tx_rise_ns:.4f} ns  fall={tx_fall_ns:.4f} ns",
        f"//   CH   Elmore={ch_ns:.4f} ns  (R={R_ch:.2f} ohm, C={C_ch:.1f} fF)",
        f"//   RX   rise={rx_rise_ns:.4f} ns  fall={rx_fall_ns:.4f} ns{sym_note}",
        f"//   TOTAL rise={total_rise_ns:.4f} ns  fall={total_fall_ns:.4f} ns",
        "// NOTE: specify on PAD->DOUT encodes full link delay.",
        "//",
        "// Power (alpha=0.5):",
        f"//   Leakage={leak_nW:.3f} nW  Switching={sw_pJ:.4f} pJ/trans",
        f"//   Energy/bit={epb_pJ:.4f} pJ/bit",
        f"//   Termination={term_mW:.4f} mW  "
        + ("(AC-coupled)" if ac_coupled else "(DC)"),
        "// " + "=" * 62,
        "",
        "`timescale 1ps/1ps",
        "",
        "module rx_lane (",
        "    input  wire PAD,",
        "    output wire DOUT,",
        "    input  wire VDD,",
        "    input  wire VSS",
        ");",
        "",
        "    // Internal nodes",
    ]
    if ac_coupled:
        L.append("    wire ac_n;")
        chain_input = "ac_n"
    else:
        chain_input = "PAD"
    for i in range(1, num_stages):
        L.append(f"    wire rx_out{i};")
    L.append("")

    # AC coupling / termination
    if ac_coupled:
        if c_ac_fF > 0.0:
            L += [
                f"    // C_AC={c_ac_fF:.1f} fF  R_TERM={r_rx_ohm:.1f} ohm  tau={ac_tau_ps} ps",
                f"    assign #({ac_tau_ps}) ac_n = PAD;",
                "",
            ]
        else:
            L += [
                "    // AC-coupled (C_AC not yet resolved)",
                "    assign ac_n = PAD;",
                "",
            ]
    else:
        L += [
            f"    // DC termination: R_TERM={r_rx_ohm:.1f} ohm to VSS",
            "",
        ]

    # inverter chain
    L.append("    // Input buffer / slicer chain")
    for i in range(num_stages):
        in_node  = chain_input     if i == 0             else f"rx_out{i}"
        out_node = f"rx_out{i+1}" if i < num_stages - 1 else "DOUT"
        sz = inv_sizes[i] if i < len(inv_sizes) else None
        if sz is not None and isinstance(sz, (list, tuple)) and len(sz) == 2:
            sz_str = f"NMOS w={sz[0]} um  PMOS w={sz[1]} um"
        elif sz is not None:
            sz_str = f"w={sz} um"
        else:
            sz_str = "size unknown"
        L.append(f"    not u_inv{i+1} ({out_node}, {in_node});  // {sz_str}")
    L.append("")

    # specify — full link delay
    L += [
        "    // STA arc: full link delay (TX + channel + RX)",
        "    specify",
        f"        ( posedge PAD => (DOUT +: PAD) ) = ({total_rise_ps}, {total_fall_ps});",
        f"        ( negedge PAD => (DOUT -: PAD) ) = ({total_fall_ps}, {total_rise_ps});",
        "    endspecify",
        "",
        "endmodule // rx_lane",
        "",
        "",
    ]

    # ================================================================
    # rx  —  N-lane top
    # ================================================================
    L += [
        "// " + "=" * 62,
        f"// rx -- {num_lanes}-lane RX top  ({link_tag})",
        f"// Flat instantiation of {num_lanes} rx_lane cells.",
        f"// Port names match SPICE: PAD0..PAD{num_lanes-1}, DOUT0..DOUT{num_lanes-1}",
        "// " + "=" * 62,
        "",
        "module rx (",
    ]
    for i in range(num_lanes):
        L.append(f"    input  wire PAD{i},")
    for i in range(num_lanes):
        comma = "," if i < num_lanes - 1 else ""
        L.append(f"    output wire DOUT{i}{comma}")
    L += [
        ");",
        "",
        "    supply1 VDD;",
        "    supply0 VSS;",
        "",
        f"    // {num_lanes} lane instantiations",
    ]
    for i in range(num_lanes):
        L += [
            f"    rx_lane u_lane{i} (",
            f"        .PAD  (PAD{i}),",
            f"        .DOUT (DOUT{i}),",
            f"        .VDD  (VDD),",
            f"        .VSS  (VSS)",
            f"    );",
            "",
        ]
    L += [
        "endmodule // rx",
        "",
    ]

    _write_lines(path, L)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _link_tag(cfg) -> str:
    return (f"{cfg.link.pkg_type}  {cfg.link.reach_mm} mm  "
            f"{cfg.link.bump_pitch_um} um pitch  "
            f"{cfg.link.data_rate_Gbps} Gb/s  "
            f"{cfg.process.node.upper()}")


def _write_lines(path: str, lines: list) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines))