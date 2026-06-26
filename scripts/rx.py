"""
rx.py — RX netlist generation and Liberate characterization.

Builds the rxip SPICE netlist and Liberate scripts for one link configuration
and runs the characterization.

Public API (called by main.py):
    gen_netlist(cfg, ch_result, term_result, run_dir) -> RxNetlistResult
"""

import math
import os
import re
import subprocess
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import device
from device import DeviceSpec


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class RxNetlistResult:
    rx_dir:           str
    use_termination:  bool
    r_rx_ohm:         float
    c_ac_pF:          float        # AC coupling cap value used (pF) — 0 for Thevenin
    r_bias_hi_ohm:    float = 1e6  # Thevenin bias resistor to VDD (Ohm)
    r_bias_lo_ohm:    float = 1e6  # Thevenin bias resistor to VSS (Ohm)
    w_preamp_n_um:    float = 0.0
    w_preamp_p_um:    float = 0.0
    w_buf_n_um:       float = 0.0
    w_buf_p_um:       float = 0.0
    cap_in_pF:        float = 0.0  # Measured pre-amp input cap (pF)
    cap_in_source:    str   = ""   # "spice" or "analytical_fallback"
    input_slews_ns_used: Optional[List[float]] = None  # Actual slew values used

    def report(self) -> str:
        lines = [
            "=== RX Netlist ===",
            f"  Output dir      : {self.rx_dir}",
            f"  Pre-amp         : NMOS={self.w_preamp_n_um}u  PMOS={self.w_preamp_p_um}u",
            f"  Output buffer   : NMOS={self.w_buf_n_um}u  PMOS={self.w_buf_p_um}u",
            f"  Input cap       : {self.cap_in_pF:.5f} pF  [{self.cap_in_source}]",
        ]
        if self.input_slews_ns_used:
            slew_str = ", ".join(f"{s:.4f}" for s in self.input_slews_ns_used)
            lines.append(f"  Input slews (ns): [{slew_str}]")
        if self.use_termination:
            lines += [
                f"  Termination     : ENABLED (Thevenin split)",
                f"    R_term        : {self.r_rx_ohm:.1f} Ohm",
                f"    R_bias_hi     : {self.r_bias_hi_ohm:.0f} Ohm",
                f"    R_bias_lo     : {self.r_bias_lo_ohm:.0f} Ohm",
            ]
        else:
            lines.append("  Termination     : DISABLED")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coupling-cap config helper
# ---------------------------------------------------------------------------

def _coupling_cap_from_cfg(cfg) -> Tuple[bool, float]:
    """Return (enabled, cc_rx_pad_fF) from cfg.channel.coupling_cap, with defaults."""
    cc = getattr(getattr(cfg, "channel", None), "coupling_cap", None)
    if cc is None:
        return (False, 1.0)
    return (
        bool(getattr(cc, "enabled", True)),
        float(getattr(cc, "cc_rx_pad_fF", 1.0)),
    )


def _rx_signal_pairs_from_cfg(cfg) -> Optional[list]:
    """Return rx_signal_pairs from cfg.layout, or None if unavailable."""
    lay = getattr(cfg, "layout", None)
    if lay is None:
        return None
    return getattr(lay, "rx_signal_pairs", None)


# ---------------------------------------------------------------------------
# Template helpers: extract unit subckt, generate N-lane wrapper
# ---------------------------------------------------------------------------

def _find_subckt_start(content: str, unit_name: str) -> int:
    """Return the index of the line starting with '.subckt <unit_name>'.

    Line-anchored: ignores `.subckt` substrings inside SPICE comments.
    """
    pat = re.compile(rf'(^|\n)\.subckt\s+{re.escape(unit_name)}(\s|$)')
    m = pat.search(content)
    if m is None:
        return -1
    return m.start() + (1 if m.group(1) == '\n' else 0)


def _find_ends(content: str, unit_name: str, from_idx: int = 0) -> int:
    """Return the index of a line starting with '.ends <unit_name>'."""
    pat = re.compile(rf'(^|\n)\.ends\s+{re.escape(unit_name)}\b')
    m = pat.search(content, from_idx)
    if m is None:
        return -1
    return m.start() + (1 if m.group(1) == '\n' else 0)


def _extract_scs_header(content: str, first_subckt_name: str) -> str:
    """Return everything before '.subckt <first_subckt_name>' (line-anchored)."""
    idx = _find_subckt_start(content, first_subckt_name)
    return content[:idx] if idx != -1 else ""


def _extract_unit_subckt(content: str, unit_name: str) -> str:
    """Return '.subckt <unit_name> ... .ends <unit_name>' (line-anchored)."""
    start = _find_subckt_start(content, unit_name)
    if start == -1:
        raise ValueError(f".subckt {unit_name} not found in template")
    end = _find_ends(content, unit_name, from_idx=start)
    if end == -1:
        raise ValueError(f".ends {unit_name} not found in template")
    end_line_end = content.find('\n', end)
    if end_line_end == -1:
        end_line_end = len(content)
    return content[start:end_line_end]


def _gen_rxip_wrapper_scs(lane_count: int, use_coupling_cap: bool = False,
                          signal_pairs: Optional[list] = None) -> str:
    """
    Generate '.subckt rxip ... .ends rxip' for lane_count RX lanes.

    Port order: PAD_0..N-1 (4 per row)  VDD VSS  OUT_0..N-1 (4 per row)
    Instances : xrx{i}  PAD_{i}  VDD VSS  OUT_{i}  rx

    When ``use_coupling_cap`` is True, inter-lane on-die PAD-to-PAD fringe
    coupling caps are added between lane pairs.

    Pair topology:
      - If signal_pairs is provided (from bump map), pairs come from physical
        adjacency with coupling scaled by 1/distance and shielded pairs
        suppressed.
      - Otherwise, sequential pairs (0-1, 1-2, ..., N-2 to N-1) at unit
        distance.
    """
    _SHIELD_SUPPRESSION = 0.1

    lines = [".subckt rxip \\"]  # opening line with continuation

    # PAD pins — 4 per row, always continued with backslash
    for start in range(0, lane_count, 4):
        end  = min(start + 4, lane_count)
        pins = "  ".join(f"PAD_{i}" for i in range(start, end))
        lines.append(f"    {pins} \\")

    lines.append("    VDD VSS \\")

    # OUT pins — 4 per row; last row has no trailing backslash
    out_starts = list(range(0, lane_count, 4))
    for k, start in enumerate(out_starts):
        end    = min(start + 4, lane_count)
        pins   = "  ".join(f"OUT_{i}" for i in range(start, end))
        suffix = "" if k == len(out_starts) - 1 else " \\"
        lines.append(f"    {pins}{suffix}")

    lines.append("")

    for i in range(lane_count):
        lines.append(f"xrx{i:<2} PAD_{i:<2} VDD VSS OUT_{i:<2} rx")

    if use_coupling_cap and lane_count >= 2:
        # Build coupling pair list
        cc_pairs = []
        if signal_pairs is not None:
            for sp in signal_pairs:
                scale = (1.0 / sp.dist_pitches) if sp.dist_pitches > 0 else 1.0
                if sp.shielded:
                    scale *= _SHIELD_SUPPRESSION
                cc_pairs.append((sp.lane_i, sp.lane_j, scale))
        else:
            for i in range(lane_count - 1):
                cc_pairs.append((i, i + 1, 1.0))

        lines.append("")
        lines.append("* --------------------------------------------------------------------------")
        if signal_pairs is not None:
            lines.append(
                f"* Inter-lane on-die PAD-to-PAD coupling (bump-map topology, "
                f"{len(cc_pairs)} pairs)"
            )
            lines.append(
                f"*   Scaled by 1/distance; shielded pairs suppressed to "
                f"{_SHIELD_SUPPRESSION}x."
            )
        else:
            lines.append(
                f"* Inter-lane on-die PAD-to-PAD coupling (sequential, "
                f"{len(cc_pairs)} pairs)"
            )
        lines.append("* --------------------------------------------------------------------------")
        for li, lj, scale in cc_pairs:
            lines.append(
                f"Cc_pad_{li}_{lj} PAD_{li} PAD_{lj} "
                f"'{scale:.4f}*Cc_rx_pad_fF*1e-15'"
            )

    lines.append("")
    lines.append(".ends rxip")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SPICE netlist generator (reads template, patches term flag, builds N-lane wrapper)
# ---------------------------------------------------------------------------

def _gen_rx_mos_lines(
    w_preamp_n: float, w_preamp_p: float,
    w_buf_n:    float, w_buf_p:    float,
    nmos_name:  str,   pmos_name:  str,
    l_um:       float, nf:         int,
    spec:       DeviceSpec,
) -> List[str]:
    """Generate RX transistor instance lines for the pre-amp and output buffer."""
    lines = []

    if spec.is_finfet:
        l_nm = spec.l_nm()
        max_nfin = spec.nfin_max

        def _finfet_insts(prefix, out, inp, supply, bulk, model, nfin):
            """Return instance line(s), splitting if nfin > max_nfin."""
            result = []
            if nfin <= max_nfin:
                result.append(f"{prefix} {out} {inp} {supply} {bulk} {model} L={l_nm}n nfin={nfin}")
            else:
                remaining, part = nfin, 0
                while remaining > 0:
                    n = min(remaining, max_nfin)
                    result.append(f"{prefix}_{part} {out} {inp} {supply} {bulk} {model} L={l_nm}n nfin={n}")
                    remaining -= n
                    part += 1
            return result

        nfin_preamp_n = spec.w_to_nfin(w_preamp_n)
        nfin_preamp_p = spec.w_to_nfin(w_preamp_p)
        nfin_buf_n    = spec.w_to_nfin(w_buf_n)
        nfin_buf_p    = spec.w_to_nfin(w_buf_p)

        lines.append(f"// Pre-amplifier (nfin_n={nfin_preamp_n}, nfin_p={nfin_preamp_p})")
        lines.extend(_finfet_insts("xnm1", "out1", "PAD", "VSS", "VSS", nmos_name, nfin_preamp_n))
        lines.extend(_finfet_insts("xpm1", "out1", "PAD", "VDD", "VDD", pmos_name, nfin_preamp_p))
        lines.append(f"// Output buffer (nfin_n={nfin_buf_n}, nfin_p={nfin_buf_p})")
        lines.extend(_finfet_insts("xnm2", "out", "out1", "VSS", "VSS", nmos_name, nfin_buf_n))
        lines.extend(_finfet_insts("xpm2", "out", "out1", "VDD", "VDD", pmos_name, nfin_buf_p))

    else:
        # subckt_wl: x-prefix subckt sized by w/l; emit nf= only when include_nf.
        nft = f" nf={nf}" if spec.include_nf else ""
        lines.append(f"// Pre-amplifier (NMOS w={w_preamp_n}u, PMOS w={w_preamp_p}u)")
        lines.append(f"xnm1 out1 PAD VSS VSS {nmos_name} w={w_preamp_n}u l={l_um}u{nft}")
        lines.append(f"xpm1 out1 PAD VDD VDD {pmos_name} w={w_preamp_p}u l={l_um}u{nft}")
        lines.append(f"// Output buffer (NMOS w={w_buf_n}u, PMOS w={w_buf_p}u)")
        lines.append(f"xnm2 out  out1  VSS VSS {nmos_name} w={w_buf_n}u l={l_um}u{nft}")
        lines.append(f"xpm2 out  out1  VDD VDD {pmos_name} w={w_buf_p}u l={l_um}u{nft}")

    return lines


def _rebuild_rx_subckt(
    subckt:      str,
    w_preamp_n:  float, w_preamp_p: float,
    w_buf_n:     float, w_buf_p:    float,
    nmos_name:   str,   pmos_name:  str,
    l_um:        float, nf:         int,
    spec:        DeviceSpec,
) -> str:
    """Replace transistor instances in the rx subcircuit with new sizes."""
    lines = subckt.split('\n')

    # Find the last line of the static analog network (before MOS instances).
    # Priority: C_rx_esd line (new pad/ESD topology) > RT line (Thevenin only).
    boundary_idx = None
    ends_idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith('C_rx_esd') or s.lower().startswith('c_rx_esd'):
            boundary_idx = i
        elif boundary_idx is None and s.startswith('RT'):
            boundary_idx = i
        if s.startswith('.ends'):
            ends_idx = i
            break

    # Fallback: if neither found, insert before first transistor instance
    if boundary_idx is None:
        for i, line in enumerate(lines):
            s = line.strip().lower()
            if s.startswith('xnm') or s.startswith('xpm'):
                boundary_idx = i - 1
                break
        if boundary_idx is None:
            boundary_idx = 0

    prefix_lines = lines[:boundary_idx + 1]
    suffix_lines = [lines[ends_idx]] if ends_idx is not None else [".ends rx"]

    new_mos = _gen_rx_mos_lines(
        w_preamp_n, w_preamp_p, w_buf_n, w_buf_p,
        nmos_name, pmos_name, l_um, nf, spec,
    )

    return '\n'.join(prefix_lines + [''] + new_mos + [''] + suffix_lines)


def _patch_transistor_l(subckt: str, l_um: float, spec: DeviceSpec) -> str:
    """Replace hardcoded gate length in transistor instance lines with l_um."""
    if not l_um:
        return subckt
    if spec.is_finfet:
        l_nm = int(round(l_um * 1000))
        return re.sub(r'\bL=\d+n\b', f'L={l_nm}n', subckt)
    return re.sub(r'\bl=[\d.]+u\b', f'l={l_um}u', subckt)


def _gen_rxip_scs(
    template_scs_path: str,
    lane_count:        int,
    use_termination:   bool,
    r_rx_ohm:          float,
    c_ac_pF:           float,
    w_preamp_n:        float,
    w_preamp_p:        float,
    w_buf_n:           float,
    w_buf_p:           float,
    nmos_name:         str,
    pmos_name:         str,
    l_um:              float = 0.0,
    nf:                int   = 1,
    spec:              Optional[DeviceSpec] = None,
    r_bias_hi_ohm:     float = 1e6,
    r_bias_lo_ohm:     float = 1e6,
    use_coupling_cap:  bool  = False,
    cc_rx_pad_fF:      float = 1.0,
    signal_pairs:      Optional[list] = None,
) -> str:
    """
    Return the full rxip.scs SPICE text.

    Reads the unit 'rx' subcircuit from *template_scs_path*, replaces the
    transistor instance lines with freshly generated ones using the config
    RX sizes, patches the .param terminated flag, and the Thevenin element
    values (when termination enabled).  The far-end Thevenin termination
    (RT + bias resistors) lives inside the rx subckt in rxip.scs, so its
    energy/loading is captured by the RX Liberate run.
    """
    if spec is None:
        spec = DeviceSpec()
    with open(template_scs_path) as fh:
        template = fh.read()

    header      = _extract_scs_header(template, "rx")
    unit_subckt = _extract_unit_subckt(template, "rx")

    # --- Replace transistor instance lines with config sizes ---
    unit_subckt = _rebuild_rx_subckt(
        unit_subckt,
        w_preamp_n, w_preamp_p, w_buf_n, w_buf_p,
        nmos_name, pmos_name, l_um, nf, spec,
    )

    # Patch .param terminated flag in the header
    term_flag = 1 if use_termination else 0
    header = re.sub(r'\.param terminated=\d+',
                    f'.param terminated={term_flag}',
                    header)

    # Patch .param Cc_rx_pad_fF. When coupling is disabled we drop the cap
    # value to a negligible number so the wrapper's coupling caps (if any
    # were ever emitted) short to zero. When enabled we write the user value.
    cc_value = cc_rx_pad_fF if use_coupling_cap else 1e-6
    if re.search(r'\.param\s+Cc_rx_pad_fF\s*=', header):
        header = re.sub(r'\.param\s+Cc_rx_pad_fF\s*=\s*[\d.eE+\-]+',
                        f'.param Cc_rx_pad_fF={cc_value:g}',
                        header)

    # When termination is active, patch the R_bias_hi, R_bias_lo, and RT values
    # into the rx subckt (the Thevenin split-termination network on the PAD).
    if use_termination:
        unit_subckt = re.sub(
            r"(R_bias_hi\s+\S+\s+\S+\s+'terminated==1 \? )[\d.eE+]+",
            rf"\g<1>{r_bias_hi_ohm:.0f}",
            unit_subckt,
        )
        unit_subckt = re.sub(
            r"(R_bias_lo\s+\S+\s+\S+\s+'terminated==1 \? )[\d.eE+]+",
            rf"\g<1>{r_bias_lo_ohm:.0f}",
            unit_subckt,
        )
        unit_subckt = re.sub(
            r"(RT\s+\S+\s+\S+\s+'terminated==1 \? )[\d.]+",
            rf"\g<1>{r_rx_ohm:.1f}",
            unit_subckt,
        )

    wrapper = _gen_rxip_wrapper_scs(lane_count, use_coupling_cap=use_coupling_cap,
                                    signal_pairs=signal_pairs)
    return header + unit_subckt + "\n\n" + wrapper


# ---------------------------------------------------------------------------
# Liberate file generators
# ---------------------------------------------------------------------------


def _gen_char_tcl(vdd: float, temp: float) -> str:
    """Generate char.tcl for rxip with the given VDD and temperature."""
    return f"""\
# Liberate characterization script for rxip
set rundir $env(PWD)

exec mkdir -p ${{rundir}}/LDB
exec mkdir -p ${{rundir}}/LIBRARY
exec mkdir -p ${{rundir}}/DATASHEET

### VDD and temperature from config ###
set VDD_VALUE {vdd}
set_operating_condition -voltage $VDD_VALUE -temp {temp}

source ${{rundir}}/template.tcl

set_var extsim_model_include ${{rundir}}/model.sp

source ${{rundir}}/define_leafcell.tcl

read_spice -format spectre ${{rundir}}/rxip.scs

char_library -ccs -ecsm -cells ${{cells}}

write_library -overwrite ${{rundir}}/LIBRARY/rxip_nldm.lib
write_verilog ${{rundir}}/LIBRARY/rxip.v

write_datasheet -format text ${{rundir}}/DATASHEET/rxip
"""

_RUN_SH_CMD = "liberate char.tcl 2>&1 | tee char.log >/dev/null\n"


def _build_run_sh(cfg) -> str:
    """Return run.sh content. 'liberate' must already be on PATH (see README)."""
    return f"#!/bin/bash\n{_RUN_SH_CMD}"


# ---------------------------------------------------------------------------
# FreePDK45 model include (HSPICE BSIM4 PTM models + wrapper subckts)
# ---------------------------------------------------------------------------
# FreePDK45 ships HSPICE-format BSIM4 (level=54) PTM models, one .inc per Vt
# flavor, under <lib_path>/models_<corner>/.  They are included in SPICE
# language mode; each device is wrapped in a parameterized 4-terminal subckt
# (drain gate source bulk) so the pipeline instantiates it with x-prefix calls
# passing w / l / nf — the same device ABI as the generic subckt_wl path.
_FREEPDK45_FLAVORS = (
    ("nmos_vtg",   "NMOS_VTG"),   ("pmos_vtg",   "PMOS_VTG"),
    ("nmos_vtl",   "NMOS_VTL"),   ("pmos_vtl",   "PMOS_VTL"),
    ("nmos_vth",   "NMOS_VTH"),   ("pmos_vth",   "PMOS_VTH"),
    ("nmos_thkox", "NMOS_THKOX"), ("pmos_thkox", "PMOS_THKOX"),
)


def _freepdk45_model_sp(lib_path: str, lib_corner: str) -> str:
    """Return FreePDK45 model-include text (SPICE-lang PTM models + wrappers)."""
    corner = lib_corner or "nom"
    models_dir = os.path.join(lib_path, f"models_{corner}")
    lines = [
        "*** Model include file — FreePDK45 (open-source) ***",
        f"* BSIM4 (level=54) Predictive Technology Models, {corner} corner.",
        "simulator lang = spice",
        "",
        "* ---- Raw PTM device models (drawn L = 50 nm) ----",
    ]
    for _sub, model in _FREEPDK45_FLAVORS:
        lines.append(f'.include "{os.path.join(models_dir, model + ".inc")}"')
    lines += [
        "",
        "* ---- Parameterized 4-terminal wrappers (drain gate source bulk) ----",
    ]
    for sub, model in _FREEPDK45_FLAVORS:
        lines.append(f".subckt {sub} d g s b w=0.1u l=0.05u nf=1")
        lines.append(f"M0 d g s b {model} w=w l=l nf=nf")
        lines.append(f".ends {sub}")
    return "\n".join(lines) + "\n"


def _gen_model_sp(lib_path: str, lib_corner: str,
                  lib_corner2: Optional[str] = None,
                  fmt: str = "spice_lib") -> str:
    """Return the model-include (model.sp) text for the configured PDK.

    Format chosen by ``cfg.process.model_include_format`` (``fmt``):
      * ``hspice_ptm``      — HSPICE BSIM .inc models + wrapper subckts.
      * ``spectre_include`` — Spectre include of lib_path + allModels.scs.
      * ``spice_lib``       — .lib "<lib_path>" <corner> (default).
    """
    if fmt == "hspice_ptm":
        return _freepdk45_model_sp(lib_path, lib_corner)
    if fmt == "spectre_include":
        model_dir = os.path.dirname(lib_path)
        all_models = os.path.join(model_dir, "allModels.scs")
        return (
            "*** Model include file ***\n"
            "simulator lang=spectre\n"
            f'include "{lib_path}"\n'
            f'include "{all_models}"\n'
            "simulator lang = spice\n"
        )
    text = (
        "*** Model include file ***\n\n"
        "simulator lang = spice\n"
        f'.lib "{lib_path}" {lib_corner}\n'
    )
    if lib_corner2:
        # optional second .lib section (e.g. ESD diode / passive models)
        text += f'.lib "{lib_path}" {lib_corner2}\n'
    text += "\n"
    return text


def _gen_template_tcl(
    output_loads_pF:  List[float],
    num_lanes:        int,
    slew_lower_rise:  float, slew_upper_rise: float,
    slew_lower_fall:  float, slew_upper_fall: float,
    input_slews_ns:   List[float],
    vdd:              float,
) -> str:
    """
    Generate template.tcl for rxip with *num_lanes* lanes.

    index_1: input slew values from config
    index_2: output load sweep (multiple points — RX drives internal core logic,
             so we sweep fanout unlike TX which has a fixed channel load)
    """
    slew_str  = " ".join(str(s) for s in input_slews_ns)
    load_str  = " ".join(f"{v:g}" for v in output_loads_pF)
    n_slews   = len(input_slews_ns)
    n_loads   = len(output_loads_pF)

    # ---- pin name lists ----
    pad_pins = [f"PAD_{i}" for i in range(num_lanes)]
    out_pins = [f"OUT_{i}" for i in range(num_lanes)]

    def _pin_block(pins, indent=8, cols=4):
        """Format a list of pin names as a Tcl continuation block."""
        pad  = " " * indent
        rows = []
        for start in range(0, len(pins), cols):
            row = "  ".join(pins[start:start + cols])
            rows.append(pad + row)
        return " \\\n".join(rows)

    pad_block = _pin_block(pad_pins)
    out_block = _pin_block(out_pins)

    # ---- define_arc lines (diagonal, R+F per lane) ----
    full_pinlist = " ".join(pad_pins) + " " + " ".join(out_pins)
    arc_lines = []
    for i in range(num_lanes):
        for edge in ("R", "F"):
            pad_vec = " ".join(edge if j == i else "0" for j in range(num_lanes))
            out_vec = " ".join(edge if j == i else "X" for j in range(num_lanes))
            arc_lines.append(
                f"define_arc -type combinational "
                f"-related_pin PAD_{i} -pin OUT_{i} "
                f"-pinlist {{{full_pinlist}}} "
                f"-vector {{{pad_vec} {out_vec}}} rxip"
            )
    arcs = "\n".join(arc_lines)

    # set_pin_vdd/gnd — comma-separated PAD pin list
    pad_list_csv = ", ".join(pad_pins)

    # Leakage conditions: quiescent all-low and all-high states
    # Liberty 'when' syntax:  !PIN * !PIN ...  or  PIN * PIN ...
    leakage_low_cond  = " * ".join(f"!{p}" for p in pad_pins)
    leakage_high_cond = " * ".join(pad_pins)

    template_name_sfx = f"{n_slews}x{n_loads}"

    return f"""\
set_var slew_lower_rise {slew_lower_rise}
set_var slew_upper_rise {slew_upper_rise}
set_var slew_lower_fall {slew_lower_fall}
set_var slew_upper_fall {slew_upper_fall}
set_var measure_slew_lower_rise {slew_lower_rise}
set_var measure_slew_upper_rise {slew_upper_rise}
set_var measure_slew_lower_fall {slew_lower_fall}
set_var measure_slew_upper_fall {slew_upper_fall}

set_units -capacitance 1pF
set_units -leakage_power 1nW
set_units -timing 1ns

set cells {{ rxip }}

define_template -type delay \\
        -index_1        {{{slew_str}}} \\
        -index_2        {{{load_str}}} \\
        delay_template_{template_name_sfx}

define_template -type power \\
        -index_1        {{{slew_str}}} \\
        -index_2        {{{load_str}}} \\
        power_template_{template_name_sfx}

define_template -type constraint \\
        -index_1  {{0.250  0.750 1.500}} \\
        -index_2  {{0.250  0.750 1.500}} \\
        constraint_template_3x3

define_cell \\
        -input  {{{pad_block}}} \\
        -output {{{out_block}}} \\
        -pad    {{{pad_block}}} \\
        -bidi {{}} -clock {{}} -async {{}} \\
        -pinlist {{{pad_block} \\
                  {out_block} \\
                  VDD VSS}} \\
        -constraint  constraint_template_3x3 \\
        -delay       delay_template_{template_name_sfx} \\
        -power       power_template_{template_name_sfx} \\
        rxip

{arcs}

set VDD_VALUE {vdd}
set_vdd VDD $VDD_VALUE
set_gnd VSS 0.0

set_pin_vdd -supply_name VDD {{{pad_list_csv}}} rxip 1
set_pin_gnd -supply_name VSS {{{pad_list_csv}}} rxip 0

set_var init_pin_hidden_period 1e-6
set_var extsim_save_failed deck
set_var extsim_save_passed all

# Leakage states: quiescent all-low and all-high (required for -io mode)
# Must appear before read_spice and char_library
define_leakage -when "{leakage_low_cond}" rxip
define_leakage -when "{leakage_high_cond}" rxip
"""


# ---------------------------------------------------------------------------
# RX Sizing — Liberate-based delay sweep
# ---------------------------------------------------------------------------

@dataclass
class RxSizingResult:
    """Result of the RX sizing sweep."""
    w_preamp_n_um: float
    w_preamp_p_um: float
    w_buf_n_um:    float
    w_buf_p_um:    float
    num_stages:    int          # always 2 (preamp + buffer)
    beta_ratio:    float        # PMOS/NMOS width ratio
    stage_ratio:   float        # buffer_w / preamp_w sizing ratio
    delay_ps:      float        # Liberate PAD→OUT propagation delay (ps)
    power_uW:      float        # Liberate leakage power (uW)
    budget_ps:     float        # allowed delay budget (ps)
    meets_budget:  bool
    all_candidates: List[Dict] = field(default_factory=list)

    def report(self) -> str:
        status = "MEETS BUDGET" if self.meets_budget else "!! FASTEST (budget not met)"
        lines = [
            "=== RX Sizing (Liberate) ===",
            f"  Target delay   : {self.budget_ps:.1f} ps",
            f"  Selected delay : {self.delay_ps:.1f} ps  [{status}]",
            f"  Stages         : {self.num_stages}",
            f"  Beta ratio     : {self.beta_ratio:.2f}",
            f"  Stage ratio    : {self.stage_ratio:.2f}",
            f"  Pre-amp        : NMOS={self.w_preamp_n_um:.4f}u  PMOS={self.w_preamp_p_um:.4f}u",
            f"  Output buffer  : NMOS={self.w_buf_n_um:.4f}u  PMOS={self.w_buf_p_um:.4f}u",
            f"  Leakage power  : {self.power_uW:.3f} uW",
            f"  Candidates     : {len(self.all_candidates)} evaluated",
        ]
        return "\n".join(lines)


def _parse_sizing_datasheet(ds_path: str) -> Optional[Dict[str, float]]:
    """
    Parse a Liberate DATASHEET (text format) for delay and leakage.

    Returns dict with delay_rr_first_ns, delay_ff_first_ns, delay_avg_ps,
    leak_avg_nW, or None if parsing fails.
    """
    if not os.path.exists(ds_path):
        return None

    with open(ds_path) as f:
        content = f.read()

    result = {}

    # Leakage
    m = re.search(
        r'Leakage\(nW\).*?\|\s*rxip\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
        content, re.DOTALL
    )
    if m:
        result['leak_avg_nW'] = float(m.group(2))

    # Delay: PAD_0->OUT_0 (RR and FF) — first/mid/last
    for edge, key in (("RR", "delay_rr"), ("FF", "delay_ff")):
        rows = re.findall(
            rf'\|\s*rxip\s*\|\s*PAD_\d+->OUT_\d+\({edge}\)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
            content
        )
        if rows:
            # Average "first" delay across all lanes (should all be the same for 1-lane)
            result[f'{key}_first_ns'] = sum(float(r[0]) for r in rows) / len(rows)
            result[f'{key}_mid_ns']   = sum(float(r[1]) for r in rows) / len(rows)

    # Compute average delay in ps from the "first" values (fastest slew, lightest load)
    rr_first = result.get('delay_rr_first_ns', 0.0)
    ff_first = result.get('delay_ff_first_ns', 0.0)
    if rr_first > 0 and ff_first > 0:
        result['delay_avg_ps'] = (rr_first + ff_first) / 2.0 * 1000.0
    elif rr_first > 0:
        result['delay_avg_ps'] = rr_first * 1000.0
    elif ff_first > 0:
        result['delay_avg_ps'] = ff_first * 1000.0
    else:
        return None

    return result


def _run_liberate_candidate(
    cfg,
    cand_dir:       str,
    w_preamp_n:     float,
    w_preamp_p:     float,
    w_buf_n:        float,
    w_buf_p:        float,
    use_termination: bool,
    r_rx_ohm:       float,
    c_ac_pF:        float,
    input_slews_ns: List[float],
    ch_result=None,
) -> Optional[Dict[str, float]]:
    """
    Generate a Liberate characterization setup, run it, and parse
    the DATASHEET for delay and leakage.

    Returns dict from _parse_sizing_datasheet, or None on failure.
    """
    proc = cfg.process
    tx   = cfg.transistor
    lib  = cfg.liberate

    os.makedirs(cand_dir, exist_ok=True)

    template_scs_path = os.path.join(lib.template_dir, "rxip", "rxip.scs")

    cc_enabled, cc_rx_pad_fF = _coupling_cap_from_cfg(cfg)
    scs_text = _gen_rxip_scs(
        template_scs_path  = template_scs_path,
        lane_count         = cfg.link.lane_count,
        use_termination    = use_termination,
        r_rx_ohm           = r_rx_ohm,
        c_ac_pF            = c_ac_pF,
        w_preamp_n         = w_preamp_n,
        w_preamp_p         = w_preamp_p,
        w_buf_n            = w_buf_n,
        w_buf_p            = w_buf_p,
        nmos_name          = tx.nmos_name,
        pmos_name          = tx.pmos_name,
        l_um               = tx.l_um,
        nf                 = tx.nf,
        spec               = DeviceSpec.from_cfg(cfg),
        use_coupling_cap   = cc_enabled,
        cc_rx_pad_fF       = cc_rx_pad_fF,
        signal_pairs       = _rx_signal_pairs_from_cfg(cfg),
    )
    _write(cand_dir, "rxip.scs", scs_text)

    # 2. model.sp
    _write(cand_dir, "model.sp", _gen_model_sp(
        proc.lib_path, proc.lib_corner,
        getattr(proc, 'lib_corner2', None), fmt=proc.model_include_format
    ))

    template_text = _gen_template_tcl(
        output_loads_pF = lib.output_loads_pF,
        num_lanes       = cfg.link.lane_count,
        slew_lower_rise = lib.slew_lower_rise,
        slew_upper_rise = lib.slew_upper_rise,
        slew_lower_fall = lib.slew_lower_fall,
        slew_upper_fall = lib.slew_upper_fall,
        input_slews_ns  = input_slews_ns,
        vdd             = proc.vdd,
    )
    _write(cand_dir, "template.tcl", template_text)

    # 4. define_leafcell.tcl
    _write(cand_dir, "define_leafcell.tcl",
           device.define_leafcell_text(lib.template_dir, "rxip"))

    # 5. char.tcl
    _write(cand_dir, "char.tcl", _gen_char_tcl(proc.vdd, proc.temp))

    # 6. run.sh
    _write(cand_dir, "run.sh", _build_run_sh(cfg))
    os.chmod(os.path.join(cand_dir, "run.sh"), 0o755)

    # 7. Run Liberate
    shell = "bash"
    try:
        proc_run = subprocess.Popen(
            [shell, "run.sh"],
            cwd=cand_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        for line in proc_run.stdout:
            pass  # consume output silently
        proc_run.wait()
        if proc_run.returncode != 0:
            return None
    except Exception:
        return None

    # 8. Parse DATASHEET
    ds_path = os.path.join(cand_dir, "DATASHEET", "rxip.txt")
    return _parse_sizing_datasheet(ds_path)


def size_rx(cfg, term_result, run_dir: str, ch_result=None) -> RxSizingResult:
    """
    Find the minimum-power RX buffer configuration that meets the delay
    budget, using Liberate characterization for each candidate.

    For each (beta_ratio, stage_ratio) combination, a full 1-lane Liberate
    characterization is run.  The delay from the DATASHEET ("first" entry —
    fastest input slew, lightest output load) is used as the comparison
    metric, ensuring perfect consistency with the final RX characterization.

    The timing budget is:
        budget_ps = max_rx_delay_ui_fraction × UI
    where UI = 1e3 / data_rate_Gbps  (in ps).

    Selection:
        1. Among candidates meeting the delay budget, pick minimum leakage.
        2. If none meet the budget, pick the fastest and warn.
    """
    proc   = cfg.process
    tx     = cfg.transistor
    lib    = cfg.liberate
    rx_cfg = cfg.rx

    rx_sizing_cfg = getattr(cfg, 'rx_sizing', None)
    if rx_sizing_cfg is None:
        ui_ps = 1e3 / cfg.link.data_rate_Gbps
        return RxSizingResult(
            w_preamp_n_um = rx_cfg.w_preamp_n_um,
            w_preamp_p_um = rx_cfg.w_preamp_p_um,
            w_buf_n_um    = rx_cfg.w_buf_n_um,
            w_buf_p_um    = rx_cfg.w_buf_p_um,
            num_stages    = 2,
            beta_ratio    = rx_cfg.w_preamp_p_um / rx_cfg.w_preamp_n_um,
            stage_ratio   = rx_cfg.w_buf_n_um / rx_cfg.w_preamp_n_um,
            delay_ps      = 0.0,
            power_uW      = 0.0,
            budget_ps     = ui_ps * 0.1,
            meets_budget  = True,
        )

    max_frac  = rx_sizing_cfg.max_rx_delay_ui_fraction
    data_rate = cfg.link.data_rate_Gbps
    ui_ps     = 1e3 / data_rate  # UI in ps (correct: 16 Gb/s → 62.5 ps)
    budget_ps = max_frac * ui_ps

    # Base pre-amp NMOS width = PDK minimum (minimize input cap)
    w_base_n = tx.w_n_um

    use_term = term_result.use_termination
    r_rx     = term_result.r_term_ohm if use_term else cfg.termination_hidden.r_rx_ohm
    c_ac     = term_result.c_ac_pF

    sizing_dir = os.path.join(run_dir, "rx_sizing")
    os.makedirs(sizing_dir, exist_ok=True)

    # Input slews: use config defaults (TX hasn't run yet)
    input_slews_ns = lib.input_slews_ns

    # Automatic sweep grid
    beta_ratios  = [2.0, 3.0, 4.0]
    stage_ratios = [1.0, 2.0, 4.0]

    total = len(beta_ratios) * len(stage_ratios)
    print(f"  [RX Sizing] Budget: {budget_ps:.1f} ps "
          f"({max_frac*100:.0f}% of {ui_ps:.1f} ps UI @ {data_rate} Gb/s)")
    print(f"  [RX Sizing] Sweeping {total} configurations via Liberate "
          f"(1-lane, w_base_n={w_base_n}u)...")

    candidates = []
    idx = 0

    for beta in beta_ratios:
        for sratio in stage_ratios:
            idx += 1
            w_preamp_n = w_base_n
            w_preamp_p = w_base_n * beta
            w_buf_n    = w_base_n * sratio
            w_buf_p    = w_base_n * sratio * beta

            # Clamp to PDK limits
            w_preamp_n = max(tx.w_min_um, min(w_preamp_n, tx.w_max_um))
            w_preamp_p = max(tx.w_min_um, min(w_preamp_p, tx.w_max_um))
            w_buf_n    = max(tx.w_min_um, min(w_buf_n, tx.w_max_um))
            w_buf_p    = max(tx.w_min_um, min(w_buf_p, tx.w_max_um))

            tag = f"b{beta:.1f}_r{sratio:.1f}"
            cand_dir = os.path.join(sizing_dir, tag)

            print(f"    [{idx}/{total}] beta={beta:.1f} stage_ratio={sratio:.1f} "
                  f"(preamp={w_preamp_n:.3f}/{w_preamp_p:.3f}u, "
                  f"buf={w_buf_n:.3f}/{w_buf_p:.3f}u) ...",
                  end=" ", flush=True)

            result = _run_liberate_candidate(
                cfg             = cfg,
                cand_dir        = cand_dir,
                w_preamp_n      = w_preamp_n,
                w_preamp_p      = w_preamp_p,
                w_buf_n         = w_buf_n,
                w_buf_p         = w_buf_p,
                use_termination = use_term,
                r_rx_ohm        = r_rx,
                c_ac_pF         = c_ac,
                input_slews_ns  = input_slews_ns,
                ch_result       = ch_result,
            )

            if result is None:
                print("FAILED")
                continue

            delay_ps  = result['delay_avg_ps']
            leak_nW   = result.get('leak_avg_nW', 0.0)
            power_uW  = leak_nW / 1000.0

            entry = {
                'num_stages':    2,
                'beta_ratio':    beta,
                'stage_ratio':   sratio,
                'w_preamp_n_um': w_preamp_n,
                'w_preamp_p_um': w_preamp_p,
                'w_buf_n_um':    w_buf_n,
                'w_buf_p_um':    w_buf_p,
                'delay_ps':      delay_ps,
                'power_uW':      power_uW,
                'meets_budget':  delay_ps <= budget_ps,
            }
            candidates.append(entry)
            status = "OK" if entry['meets_budget'] else "over budget"
            print(f"delay={delay_ps:.1f} ps, leak={leak_nW:.1f} nW [{status}]")

    if not candidates:
        warnings.warn("[RX Sizing] All Liberate runs failed. Using default RX sizing.")
        return RxSizingResult(
            w_preamp_n_um = rx_cfg.w_preamp_n_um,
            w_preamp_p_um = rx_cfg.w_preamp_p_um,
            w_buf_n_um    = rx_cfg.w_buf_n_um,
            w_buf_p_um    = rx_cfg.w_buf_p_um,
            num_stages    = 2,
            beta_ratio    = rx_cfg.w_preamp_p_um / rx_cfg.w_preamp_n_um,
            stage_ratio   = rx_cfg.w_buf_n_um / rx_cfg.w_preamp_n_um,
            delay_ps      = 0.0,
            power_uW      = 0.0,
            budget_ps     = budget_ps,
            meets_budget  = False,
        )

    # Write sweep results CSV
    _write_sizing_csv(candidates, sizing_dir)

    # Select: prefer candidates meeting the budget, sort by power; else fastest
    valid = [c for c in candidates if c['meets_budget']]
    if valid:
        best = min(valid, key=lambda c: c['power_uW'])
    else:
        best = min(candidates, key=lambda c: c['delay_ps'])
        warnings.warn(
            f"[RX Sizing] No configuration meets the {budget_ps:.1f} ps budget. "
            f"Selecting fastest: {best['delay_ps']:.1f} ps "
            f"(beta={best['beta_ratio']:.2f}, stage_ratio={best['stage_ratio']:.2f})"
        )

    print(f"  [RX Sizing] Selected: beta={best['beta_ratio']:.2f}, "
          f"stage_ratio={best['stage_ratio']:.2f}, "
          f"delay={best['delay_ps']:.1f} ps, leak={best['power_uW']*1000:.1f} nW")

    return RxSizingResult(
        w_preamp_n_um   = best['w_preamp_n_um'],
        w_preamp_p_um   = best['w_preamp_p_um'],
        w_buf_n_um      = best['w_buf_n_um'],
        w_buf_p_um      = best['w_buf_p_um'],
        num_stages      = 2,
        beta_ratio      = best['beta_ratio'],
        stage_ratio     = best['stage_ratio'],
        delay_ps        = best['delay_ps'],
        power_uW        = best['power_uW'],
        budget_ps       = budget_ps,
        meets_budget    = best['meets_budget'],
        all_candidates  = candidates,
    )


def _write_sizing_csv(candidates: List[Dict], sizing_dir: str) -> None:
    """Write all sizing sweep results to a CSV for analysis."""
    import csv
    csv_path = os.path.join(sizing_dir, "rx_sizing_sweep.csv")
    fieldnames = ['num_stages', 'beta_ratio', 'stage_ratio',
                  'w_preamp_n_um', 'w_preamp_p_um', 'w_buf_n_um', 'w_buf_p_um',
                  'delay_ps', 'power_uW', 'meets_budget']
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for c in candidates:
            writer.writerow(c)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _measure_rx_input_cap_spice(
    lib_path:        str,
    lib_corner:      str,
    w_n_um:          float,
    w_p_um:          float,
    l_um:            float,
    nf:              int,
    vdd:             float,
    nmos_name:       str,
    pmos_name:       str,
    work_dir:        str,
    temp:            float = 25,
    spec:            Optional[DeviceSpec] = None,
    model_include_format: str = "spice_lib",
    use_termination: bool  = False,
    r_term_ohm:      float = 50.0,
    c_ac_pF:         float = 5.0,
    r_bias_hi_ohm:   float = 1e6,
    r_bias_lo_ohm:   float = 1e6,
) -> Optional[float]:
    """
    Measure RX PAD-level input capacitance via Spectre SPICE Q/V method.

    Includes the Thevenin termination network (R_bias_hi to VDD, R_bias_lo
    to VSS, RT from PAD to vterm) when use_termination is True, so the
    measured capacitance matches what Liberate sees at the PAD pin of the
    full rxip cell.

    Returns capacitance in pF, or None if the simulation fails.
    """
    cap_dir = os.path.join(work_dir, "rx_input_cap")
    os.makedirs(cap_dir, exist_ok=True)
    filename = "rx_input_capacitance.scs"

    if spec is None:
        spec = DeviceSpec()

    # Model include comes from the config-declared format (no PDK strings here).
    model_header = _gen_model_sp(lib_path, lib_corner, None,
                                 fmt=model_include_format).rstrip()

    if spec.is_finfet:
        nfin_n = spec.w_to_nfin(w_n_um)
        nfin_p = spec.w_to_nfin(w_p_um)
        l_nm   = spec.l_nm()
        max_nfin = spec.nfin_max

        def _split_inst(prefix, drain, gate, src, bulk, model, nfin):
            lines = []
            if nfin <= max_nfin:
                lines.append(f'{prefix} {drain} {gate} {src} {bulk} {model} L={l_nm}n nfin={nfin}')
            else:
                rem, part = nfin, 0
                while rem > 0:
                    n = min(rem, max_nfin)
                    lines.append(f'{prefix}_{part} {drain} {gate} {src} {bulk} {model} L={l_nm}n nfin={n}')
                    rem -= n
                    part += 1
            return '\n'.join(lines)

        nmos_inst = _split_inst('xnm1', 'out', 'pad', '0',   '0',   nmos_name, nfin_n)
        pmos_inst = _split_inst('xpm1', 'out', 'pad', 'VDD', 'VDD', pmos_name, nfin_p)
    else:
        nf_tok = f' nf={nf}' if spec.include_nf else ''
        nmos_inst = f'xnm1 out pad 0 0 {nmos_name} w={w_n_um}u l={l_um}u{nf_tok}'
        pmos_inst = f'xpm1 out pad VDD VDD {pmos_name} w={w_p_um}u l={l_um}u{nf_tok}'

    # Termination network: Thevenin split with VTERM mid-rail, matching rxip.scs
    if use_termination:
        term_net = (
            f'R_bias_hi vterm VDD {r_bias_hi_ohm}\n'
            f'R_bias_lo vterm 0 {r_bias_lo_ohm}\n'
            f'RT pad vterm {r_term_ohm}'
        )
    else:
        term_net = '* No termination'

    # Gate node: PAD (Thevenin topology connects gate directly to PAD)
    gate_node = 'pad'

    # Thevenin termination is purely resistive — no large time constants.
    # Use the same timing as unterminated case.
    rise_ps      = 50
    delay_ps     = 200
    pulse_w      = 500
    period       = 1000
    sim_ps       = 1500
    meas_r_start = 200
    meas_r_end   = 300
    meas_f_start = 700
    meas_f_end   = 800

    netlist = f"""// RX PAD-level Input Capacitance Measurement - Q/V Method
{model_header}
.option post=1
.temp {temp}
VDD VDD 0 DC {vdd}
VSS 0 0 0
VIN pad_source 0 PULSE(0 {vdd} {delay_ps}p {rise_ps}p {rise_ps}p {pulse_w}p {period}p)
VMEAS pad_source pad 0
{term_net}
{nmos_inst}
{pmos_inst}
CL out 0 1f
.tran 0.1p {sim_ps}p
.measure tran q_rise_edge integ i(VMEAS) from={meas_r_start}p to={meas_r_end}p
.measure tran c_in_rise param='abs(q_rise_edge)/{vdd}'
.measure tran q_fall_edge integ i(VMEAS) from={meas_f_start}p to={meas_f_end}p
.measure tran c_in_fall param='abs(q_fall_edge)/{vdd}'
.measure tran c_in_avg param='(c_in_rise+c_in_fall)/2'
.print tran v(pad) v(out) i(VMEAS)
.end
"""

    netlist_path = os.path.join(cap_dir, filename)
    with open(netlist_path, "w") as f:
        f.write(netlist)

    spectre_cmd = f"spectre -64 {filename} -format psfascii"
    run_sh_path = os.path.join(cap_dir, "run.sh")
    with open(run_sh_path, "w") as f:
        f.write(f"#!/bin/bash\n{spectre_cmd}\n")
    os.chmod(run_sh_path, 0o755)

    orig_dir = os.getcwd()
    try:
        os.chdir(cap_dir)
        proc = subprocess.run(
            ["bash", "run.sh"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if proc.returncode != 0:
            return None

        measure_file = os.path.splitext(filename)[0] + ".measure"
        if not os.path.exists(measure_file):
            return None

        cap_pF = None
        with open(measure_file, "r") as f:
            for line in f:
                if "c_in_avg" in line:
                    parts = line.strip().split("=")
                    if len(parts) == 2:
                        cap_pF = float(parts[1].strip()) * 1e12
                        break
        return cap_pF

    finally:
        os.chdir(orig_dir)


def measure_input_cap(cfg, run_dir: str, term_result=None) -> float:
    """
    Public API: Measure RX PAD-level input capacitance via SPICE Q/V method.

    Called by main.py before TX characterization so the TX load can be set
    to the actual RX PAD input capacitance (including termination network).

    Returns capacitance in pF.  Falls back to analytical estimate on
    SPICE failure.
    """
    rx_cfg = cfg.rx
    tx     = cfg.transistor
    proc   = cfg.process
    lib    = cfg.liberate
    term_hid = cfg.termination_hidden

    rx_dir = os.path.join(run_dir, "rx")
    os.makedirs(rx_dir, exist_ok=True)

    # Resolve termination parameters
    r_bias_hi = getattr(term_hid, 'r_bias_hi_ohm', 1e6)
    r_bias_lo = getattr(term_hid, 'r_bias_lo_ohm', 1e6)
    if term_result is not None:
        use_term = term_result.use_termination
        r_term   = term_result.r_term_ohm if use_term else term_hid.r_rx_ohm
        c_ac     = term_result.c_ac_pF
    else:
        use_term = False
        r_term   = term_hid.r_rx_ohm
        c_ac     = getattr(term_hid, 'c_ac_pF', 0.0)

    print(f"  [RX] Measuring PAD-level input cap via SPICE (termination={'ON' if use_term else 'OFF'})...")
    cap_pF = _measure_rx_input_cap_spice(
        lib_path        = proc.lib_path,
        lib_corner      = proc.lib_corner,
        w_n_um          = rx_cfg.w_preamp_n_um,
        w_p_um          = rx_cfg.w_preamp_p_um,
        l_um            = tx.l_um,
        nf              = tx.nf,
        vdd             = proc.vdd,
        nmos_name       = tx.nmos_name,
        pmos_name       = tx.pmos_name,
        work_dir        = rx_dir,
        temp            = proc.temp,
        spec            = DeviceSpec.from_cfg(cfg),
        model_include_format = proc.model_include_format,
        use_termination = use_term,
        r_term_ohm      = r_term,
        c_ac_pF         = c_ac,
        r_bias_hi_ohm   = r_bias_hi,
        r_bias_lo_ohm   = r_bias_lo,
    )

    if cap_pF is None:
        # Fallback: analytical gate capacitance estimate
        COX_fF_per_um2 = 8.6
        cap_pF = (rx_cfg.w_preamp_n_um + rx_cfg.w_preamp_p_um) * tx.l_um * COX_fF_per_um2 * tx.nf / 1000.0
        warnings.warn(
            f"[RX] SPICE cap measurement failed — using analytical fallback: "
            f"{cap_pF:.5f} pF"
        )
    else:
        print(f"  [RX] cap_in = {cap_pF:.5f} pF  ({cap_pF*1000:.3f} fF)")

    return cap_pF


def parse_rx_pad_capacitance(rx_dir: str) -> Optional[float]:
    """
    Extract the PAD pin capacitance from the Liberate-generated rxip_nldm.lib.

    Looks for the first 'pin (PAD_0)' block and returns the 'capacitance'
    value in pF (the library already uses pF units per capacitive_load_unit).

    Returns None if the file doesn't exist or parsing fails.
    """
    lib_path = os.path.join(rx_dir, "LIBRARY", "rxip_nldm.lib")
    if not os.path.exists(lib_path):
        return None

    with open(lib_path) as f:
        content = f.read()

    # Match: pin (PAD_0) { ... capacitance : <value>; ... }
    pad_match = re.search(
        r'pin\s*\(\s*PAD_0\s*\)\s*\{(.*?)\n\s*\}',
        content, re.DOTALL
    )
    if not pad_match:
        return None

    cap_match = re.search(
        r'capacitance\s*:\s*([\d.eE+\-]+)\s*;',
        pad_match.group(1)
    )
    if not cap_match:
        return None

    return float(cap_match.group(1))


def _pick_3_slews(slews: List[float]) -> List[float]:
    """Reduce a sorted list of slew values to 3 representative points (min, median, max)."""
    if len(slews) <= 3:
        return slews
    mid = slews[len(slews) // 2]
    return sorted(set([slews[0], mid, slews[-1]]))


def _parse_tx_output_transition(tx_dir: str) -> Optional[List[float]]:
    """
    Parse the TX Liberate NLDM .lib file for output transition (slew) values.

    Extracts rise_transition and fall_transition values from PAD pin timing
    arcs in txip_nldm.lib.  Returns a sorted list of unique slew values (ns),
    or None if parsing fails.
    """
    lib_path = os.path.join(tx_dir, "LIBRARY", "txip_nldm.lib")
    if not os.path.exists(lib_path):
        return None

    with open(lib_path) as f:
        content = f.read()

    all_slews = []
    for table_type in ('rise_transition', 'fall_transition'):
        # Match: table_type(template) { ... values(...); ... }
        # [^}]* is safe since values() doesn't contain braces
        pattern = re.compile(
            table_type + r'\s*\([^)]*\)\s*\{([^}]*)\}',
            re.DOTALL
        )
        for match in pattern.finditer(content):
            block = match.group(1)
            v_match = re.search(r'values\s*\((.*?)\)\s*;', block, re.DOTALL)
            if v_match:
                values_str = v_match.group(1)
                # Extract numbers (may be quoted, comma-separated, multi-line)
                nums = re.findall(r'[\d]+\.[\d]+(?:[eE][+-]?\d+)?', values_str)
                for n in nums:
                    all_slews.append(float(n))

    if not all_slews:
        return None

    # Return unique sorted values, rounded to avoid float noise
    unique = sorted(set(round(v, 6) for v in all_slews))
    return unique


def parse_tx_output_transition(tx_dir: str) -> Optional[List[float]]:
    """Public accessor for co_opt_pareto to retrieve TX output slews after Liberate run."""
    return _parse_tx_output_transition(tx_dir)


def gen_netlist(cfg, ch_result, term_result, run_dir: str,
               tx_result=None) -> RxNetlistResult:
    """
    Generate RX netlist, write all Liberate scripts, and run characterization.

    Stages
    ------
    1. Determine input slews (from TX output slew or config override)
    2–7. Write files  → rxip.scs, model.sp, template.tcl,
                        define_leafcell.tcl, char.tcl, run.sh
    8. Run Liberate   → LIBRARY/rxip_nldm.lib, DATASHEET/rxip

    Parameters
    ----------
    cfg         : Config from main.py
    ch_result   : ChannelResult from channel.py
    term_result : TerminationResult from termination.py
    run_dir     : Top-level combo directory
    tx_result   : TxNetlistResult from tx.py (optional, for auto-detecting slews)

    Returns
    -------
    RxNetlistResult
    """
    lib    = cfg.liberate
    proc   = cfg.process
    term_hid = cfg.termination_hidden
    rx_cfg = cfg.rx
    tx     = cfg.transistor

    rx_dir = os.path.join(run_dir, "rx")
    os.makedirs(rx_dir, exist_ok=True)

    use_term   = term_result.use_termination
    r_rx       = term_result.r_term_ohm if use_term else term_hid.r_rx_ohm
    c_ac       = term_result.c_ac_pF
    lane_count = cfg.link.lane_count
    r_bias_hi  = getattr(term_hid, 'r_bias_hi_ohm', 1e6)
    r_bias_lo  = getattr(term_hid, 'r_bias_lo_ohm', 1e6)

    # ------------------------------------------------------------------
    # 1. Determine input slew values for RX characterization
    #    Priority: config override > TX output slew > config default
    # ------------------------------------------------------------------
    input_slews_ns = None
    slew_source = "config default"

    if rx_cfg.input_slews_ns_override:
        input_slews_ns = rx_cfg.input_slews_ns_override
        slew_source = "config override"
    elif tx_result is not None:
        # rx_slew_source selects which TX run provides the input slew:
        #   "tx_pad"  → TX output at pad (tx_only run, before channel RC)
        #   "channel" → signal at far end of channel (main TX run)
        slew_pref = getattr(rx_cfg, 'rx_slew_source', 'tx_pad')
        if slew_pref == 'tx_pad' and getattr(tx_result, 'tx_only_dir', None):
            slew_dir = tx_result.tx_only_dir
            slew_tag = "TX pad (tx_only)"
        else:
            slew_dir = tx_result.tx_dir
            slew_tag = "channel far-end (tx)"
        tx_slews = _parse_tx_output_transition(slew_dir)
        if tx_slews and len(tx_slews) >= 2:
            input_slews_ns = _pick_3_slews(tx_slews)
            slew_source = f"{slew_tag} (3-point)"

    if input_slews_ns is None:
        input_slews_ns = lib.input_slews_ns
        slew_source = "config default"

    print(f"  [RX] Input slews ({slew_source}): {input_slews_ns}")

    # ------------------------------------------------------------------
    # 2. Generate rxip.scs from template + N-lane wrapper
    # ------------------------------------------------------------------
    template_scs_path = os.path.join(lib.template_dir, "rxip", "rxip.scs")
    cc_enabled, cc_rx_pad_fF = _coupling_cap_from_cfg(cfg)
    scs_text = _gen_rxip_scs(
        template_scs_path  = template_scs_path,
        lane_count         = lane_count,
        use_termination    = use_term,
        r_rx_ohm           = r_rx,
        c_ac_pF            = c_ac,
        w_preamp_n         = rx_cfg.w_preamp_n_um,
        w_preamp_p         = rx_cfg.w_preamp_p_um,
        w_buf_n            = rx_cfg.w_buf_n_um,
        w_buf_p            = rx_cfg.w_buf_p_um,
        nmos_name          = tx.nmos_name,
        pmos_name          = tx.pmos_name,
        l_um               = tx.l_um,
        nf                 = tx.nf,
        spec               = DeviceSpec.from_cfg(cfg),
        r_bias_hi_ohm      = r_bias_hi,
        r_bias_lo_ohm      = r_bias_lo,
        use_coupling_cap   = cc_enabled,
        cc_rx_pad_fF       = cc_rx_pad_fF,
        signal_pairs       = _rx_signal_pairs_from_cfg(cfg),
    )
    _write(rx_dir, "rxip.scs", scs_text)

    # ------------------------------------------------------------------
    # 3. model.sp
    # ------------------------------------------------------------------
    _write(rx_dir, "model.sp", _gen_model_sp(
        proc.lib_path, proc.lib_corner,
        getattr(proc, 'lib_corner2', None), fmt=proc.model_include_format
    ))

    # ------------------------------------------------------------------
    # 4. template.tcl  (output loads sweep; ports for lane_count;
    #                   input slews from TX output or config)
    # ------------------------------------------------------------------
    template_text = _gen_template_tcl(
        output_loads_pF = lib.output_loads_pF,
        num_lanes       = lane_count,
        slew_lower_rise = lib.slew_lower_rise,
        slew_upper_rise = lib.slew_upper_rise,
        slew_lower_fall = lib.slew_lower_fall,
        slew_upper_fall = lib.slew_upper_fall,
        input_slews_ns  = input_slews_ns,
        vdd             = proc.vdd,
    )
    _write(rx_dir, "template.tcl", template_text)

    # ------------------------------------------------------------------
    # 5. define_leafcell.tcl
    # ------------------------------------------------------------------
    _write(rx_dir, "define_leafcell.tcl",
           device.define_leafcell_text(lib.template_dir, "rxip"))

    # ------------------------------------------------------------------
    # 6. char.tcl
    # ------------------------------------------------------------------
    _write(rx_dir, "char.tcl", _gen_char_tcl(proc.vdd, proc.temp))

    # ------------------------------------------------------------------
    # 7. run.sh
    # ------------------------------------------------------------------
    _write(rx_dir, "run.sh", _build_run_sh(cfg))
    os.chmod(os.path.join(rx_dir, "run.sh"), 0o755)

    # ------------------------------------------------------------------
    # 8. Run Liberate characterization
    # ------------------------------------------------------------------
    print(f"  [RX] Running Liberate in {rx_dir} ...")
    shell = "bash"
    proc_run = subprocess.Popen(
        [shell, "run.sh"],
        cwd=rx_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    for line in proc_run.stdout:
        print(line, end="")
    proc_run.wait()
    if proc_run.returncode != 0:
        raise RuntimeError(
            f"[RX] Liberate failed with exit code {proc_run.returncode} in {rx_dir}"
        )
    print(f"  [RX] Liberate complete.")

    # Extract PAD input capacitance from the Liberate-generated .lib
    cap_pF = 0.0
    cap_source = ""
    lib_cap = parse_rx_pad_capacitance(rx_dir)
    if lib_cap is not None:
        cap_pF = lib_cap
        cap_source = "liberate"
        print(f"  [RX] PAD input cap from .lib: {cap_pF:.5f} pF")
    else:
        # Fallback: analytical estimate
        COX_fF_per_um2 = 8.6
        cap_pF = (rx_cfg.w_preamp_n_um + rx_cfg.w_preamp_p_um) * tx.l_um * COX_fF_per_um2 * tx.nf / 1000.0
        cap_source = "analytical_fallback"
        warnings.warn(f"[RX] Could not parse .lib cap — analytical fallback: {cap_pF:.5f} pF")

    return RxNetlistResult(
        rx_dir              = rx_dir,
        use_termination     = use_term,
        r_rx_ohm            = r_rx,
        c_ac_pF             = c_ac,
        r_bias_hi_ohm       = r_bias_hi,
        r_bias_lo_ohm       = r_bias_lo,
        w_preamp_n_um       = rx_cfg.w_preamp_n_um,
        w_preamp_p_um       = rx_cfg.w_preamp_p_um,
        w_buf_n_um          = rx_cfg.w_buf_n_um,
        w_buf_p_um          = rx_cfg.w_buf_p_um,
        cap_in_pF           = cap_pF,
        cap_in_source       = cap_source,
        input_slews_ns_used = input_slews_ns,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(directory: str, filename: str, content: str) -> None:
    path = os.path.join(directory, filename)
    with open(path, "w") as f:
        f.write(content)