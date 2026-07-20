"""
tx.py — TX netlist generation and Liberate characterization.

Builds the txip SPICE netlist and Liberate scripts for one link configuration
and runs the characterization.

Public API (called by main.py):
    gen_netlist(cfg, ch_result, eq_result, run_dir) -> TxNetlistResult
"""

import math
import os
import re
import subprocess
import warnings
from dataclasses import dataclass
from typing import List, Optional, Tuple

import device
from device import DeviceSpec


# ---------------------------------------------------------------------------
# Extract the inter-lane coupling-cap knobs from cfg. Absent config disables
# coupling (returns False, 0.4, 0.2) so the feature is a no-op.
# ---------------------------------------------------------------------------
def _coupling_cap_from_cfg(cfg) -> Tuple[bool, float, float]:
    cc = getattr(getattr(cfg, "channel", None), "coupling_cap", None)
    if cc is None:
        return (False, 0.4, 0.2)
    return (
        bool(getattr(cc, "enabled", True)),
        float(getattr(cc, "cc_ratio_trace", 0.4)),
        float(getattr(cc, "cc_ratio_pad",   0.2)),
    )


def _tx_signal_pairs_from_cfg(cfg) -> Optional[list]:
    """Return tx_signal_pairs from cfg.layout, or None if unavailable."""
    lay = getattr(cfg, "layout", None)
    if lay is None:
        return None
    return getattr(lay, "tx_signal_pairs", None)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TxNetlistResult:
    tx_dir:               str          # absolute path to run_dir/tx/
    num_stages:           int
    inverter_sizes:       List[Tuple[float, float]]   # [(w_n_um, w_p_um), ...]
    use_equalization:     bool
    R_eq_ohm:             float
    C_eq_fF:              float
    load_pF:              float        # external Liberate load (RX device cap when channel embedded)
    cap_in_pF:            float        # measured unit inverter input cap (pF)
    cap_in_source:        str          # "spice" or "analytical_fallback"
    backend:              str  = "liberate"  # "liberate" or "charlib"
    channel_rc_integrated: bool = False  # True → channel RC is inside txip.scs; Liberate
                                         # TX switching power already includes channel energy
                                         # and TX delay covers full TX+channel propagation.
    # TX-only (TX+EQ, no channel) Liberate run results.
    # Populated by gen_tx_only_run() when channel_rc_integrated=True.
    tx_only_dir:            Optional[str] = None  # path to run_dir/tx_only/
    tx_only_delay_rr_ns:    float = 0.0  # TX-only propagation delay (rise-rise)
    tx_only_delay_ff_ns:    float = 0.0  # TX-only propagation delay (fall-fall)
    tx_only_slew_rr_ns:     float = 0.0  # TX-only output slew (rise)
    tx_only_slew_ff_ns:     float = 0.0  # TX-only output slew (fall)
    # TX+channel (no RX bump/pad) Liberate run results.
    # Populated by gen_tx_channel_run() when channel_rc_integrated=True.
    tx_channel_dir:           Optional[str] = None  # path to run_dir/tx_channel/
    tx_channel_delay_rr_ns:   float = 0.0  # TX+channel propagation delay (rise-rise)
    tx_channel_delay_ff_ns:   float = 0.0  # TX+channel propagation delay (fall-fall)
    tx_channel_pwr_rise_pJ:   float = 0.0  # TX+channel switching energy per bit (rise)
    tx_channel_pwr_fall_pJ:   float = 0.0  # TX+channel switching energy per bit (fall)

    def report(self) -> str:
        ch_rc_label = "ENABLED (TX delay & switching power include channel)" if self.channel_rc_integrated else "DISABLED"
        lines = [
            "=== TX Netlist ===",
            f"  Output dir      : {self.tx_dir}",
            f"  Channel RC in netlist: {ch_rc_label}",
            f"  Inverter cap    : {self.cap_in_pF:.5f} pF  [{self.cap_in_source}]",
            f"  Inverter stages : {self.num_stages}",
            f"  Output load     : {self.load_pF:.4f} pF  (external Liberate/CharLib load)",
        ]
        for i, (wn, wp) in enumerate(self.inverter_sizes):
            lines.append(f"    Stage {i+1}: NMOS={wn:.4f}u  PMOS={wp:.4f}u")
        if self.use_equalization:
            lines += [
                f"  Equalization    : ENABLED",
                f"    R_eq          : {self.R_eq_ohm:.2f} Ohm",
                f"    C_eq          : {self.C_eq_fF:.2f} fF",
            ]
        else:
            lines.append("  Equalization    : DISABLED")
        if self.tx_only_dir:
            lines += [
                f"  TX-only run     : {self.tx_only_dir}",
                f"    Delay RR/FF   : {self.tx_only_delay_rr_ns*1000:.2f} / {self.tx_only_delay_ff_ns*1000:.2f} ps",
                f"    Slew  RR/FF   : {self.tx_only_slew_rr_ns*1000:.2f} / {self.tx_only_slew_ff_ns*1000:.2f} ps",
            ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Unit inverter input capacitance — SPICE Q/V method
# ---------------------------------------------------------------------------

def _measure_inv_cap_spice(
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
) -> Optional[float]:
    """
    Measure unit inverter input capacitance via Spectre SPICE simulation.

    Uses the Q/V method:
      - PULSE stimulus on input, 0V ammeter VMEAS in series
      - Integrate current over rising and falling edges
      - C_in = |Q| / VDD, average of both edges

    Returns capacitance in pF, or None if the simulation fails.
    The netlist and results are written to work_dir/inverter_cap/.
    """
    cap_dir  = os.path.join(work_dir, "inverter_cap")
    os.makedirs(cap_dir, exist_ok=True)
    filename = "inverter_capacitance.scs"

    if spec is None:
        spec = DeviceSpec()

    # Model include comes from the config-declared format (no PDK strings here).
    model_header = _gen_model_sp(lib_path, lib_corner, None,
                                 fmt=model_include_format).rstrip()

    if spec.is_finfet:
        # FinFET: quantise physical width to fin count.
        nfin_n = spec.w_to_nfin(w_n_um)
        nfin_p = spec.w_to_nfin(w_p_um)
        l_nm   = spec.l_nm()
        nmos_inst = f'xnm1 out in 0 0 {nmos_name} L={l_nm}n nfin={nfin_n}'
        pmos_inst = f'xpm1 out in VDD VDD {pmos_name} L={l_nm}n nfin={nfin_p}'
    else:
        nf_tok = f' nf={nf}' if spec.include_nf else ''
        nmos_inst = f'xnm1 out in 0 0 {nmos_name} w={w_n_um}u l={l_um}u{nf_tok}'
        pmos_inst = f'xpm1 out in VDD VDD {pmos_name} w={w_p_um}u l={l_um}u{nf_tok}'

    netlist = f"""// Inverter Input Capacitance Measurement - Q/V Method
{model_header}
// Simulation parameters
.option post=1
.temp {temp}
// Supply and ground definitions
VDD VDD 0 DC {vdd}
VSS 0 0 0
// Input signal with controlled slew
VIN in_source 0 PULSE(0 {vdd} 200p 50p 50p 500p 1000p)
// Voltage source with 0V for current measurement
VMEAS in_source in 0
// Inverter under test
{nmos_inst}
{pmos_inst}
// Small load capacitance
CL out 0 1f
// Transient analysis
.tran 0.1p 1500p
// Input capacitance measurement using Q/V method
// Rising edge measurement
.measure tran q_rise_edge integ i(VMEAS) from=200p to=300p
.measure tran c_in_rise param='abs(q_rise_edge)/{vdd}'
// Falling edge measurement
.measure tran q_fall_edge integ i(VMEAS) from=700p to=800p
.measure tran c_in_fall param='abs(q_fall_edge)/{vdd}'
// Average input capacitance from both transitions
.measure tran c_in_avg param='(c_in_rise+c_in_fall)/2'
// Print results
.print tran v(in) v(out) i(VMEAS)
.end
"""

    netlist_path = os.path.join(cap_dir, filename)
    with open(netlist_path, "w") as f:
        f.write(netlist)

    # Write run.sh (spectre must already be on PATH — see the README prerequisites).
    spectre_cmd = f"spectre -64 {filename} -format psfascii"
    run_sh_path = os.path.join(cap_dir, "run.sh")
    with open(run_sh_path, "w") as f:
        f.write(f"#!/bin/bash\n{spectre_cmd}\n")
    os.chmod(run_sh_path, 0o755)

    proc = subprocess.run(
        ["bash", "run.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cap_dir,
    )
    if proc.returncode != 0:
        return None

    measure_file = os.path.join(cap_dir, os.path.splitext(filename)[0] + ".measure")
    if not os.path.exists(measure_file):
        return None

    cap_pF = None
    with open(measure_file, "r") as f:
        for line in f:
            if "c_in_avg" in line:
                parts = line.strip().split("=")
                if len(parts) == 2:
                    # raw value is in Farads; convert to pF
                    cap_pF = float(parts[1].strip()) * 1e12
                    break
    return cap_pF


def _measure_inv_cap_ngspice(
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
    ngspice_exe:     str = "ngspice",
    temp:            float = 25,
    spec:            Optional[DeviceSpec] = None,
    model_include_format: str = "spice_lib",
) -> Optional[float]:
    """
    Measure unit inverter input capacitance via ngspice batch simulation.

    Uses the same Q/V method as _measure_inv_cap_spice().  Runs ngspice -b
    and parses .meas results from stdout.  Returns capacitance in pF, or
    None if the simulation fails or the result cannot be parsed.
    """
    cap_dir  = os.path.join(work_dir, "inverter_cap")
    os.makedirs(cap_dir, exist_ok=True)
    filename = "inverter_capacitance.sp"

    if spec is None:
        spec = DeviceSpec()

    model_header = _gen_model_sp(lib_path, lib_corner, None,
                                 fmt=model_include_format).rstrip()
    # ngspice does not understand the Spectre 'simulator lang' directive
    model_header = re.sub(r'^\s*simulator\s+lang\s*=.*$', '', model_header,
                          flags=re.MULTILINE)

    if spec.is_finfet:
        nfin_n = spec.w_to_nfin(w_n_um)
        nfin_p = spec.w_to_nfin(w_p_um)
        l_nm   = spec.l_nm()
        nmos_inst = f'xnm1 out in 0 0 {nmos_name} L={l_nm}n nfin={nfin_n}'
        pmos_inst = f'xpm1 out in VDD VDD {pmos_name} L={l_nm}n nfin={nfin_p}'
    else:
        nf_tok = f' nf={nf}' if spec.include_nf else ''
        nmos_inst = f'xnm1 out in 0 0 {nmos_name} w={w_n_um}u l={l_um}u{nf_tok}'
        pmos_inst = f'xpm1 out in VDD VDD {pmos_name} w={w_p_um}u l={l_um}u{nf_tok}'

    netlist = f"""\
Inverter Input Capacitance Measurement - Q/V Method
{model_header}
.option temp={temp}
VDD VDD 0 DC {vdd}
VIN in_source 0 PULSE(0 {vdd} 200p 50p 50p 500p 1000p)
VMEAS in_source in 0
{nmos_inst}
{pmos_inst}
CL out 0 1f
.tran 0.1p 1500p
.measure tran q_rise_edge integ i(VMEAS) from=200p to=300p
.measure tran c_in_rise param='abs(q_rise_edge)/{vdd}'
.measure tran q_fall_edge integ i(VMEAS) from=700p to=800p
.measure tran c_in_fall param='abs(q_fall_edge)/{vdd}'
.measure tran c_in_avg param='(c_in_rise+c_in_fall)/2'
.end
"""

    netlist_path = os.path.join(cap_dir, filename)
    with open(netlist_path, "w") as f:
        f.write(netlist)

    result = subprocess.run(
        [ngspice_exe, "-b", filename],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cap_dir,
    )
    if result.returncode != 0:
        return None

    output = result.stdout.decode("utf-8", errors="replace")
    for line in output.splitlines():
        m = re.match(r'\s*c_in_avg\s*=\s*([\d.eE+\-]+)', line, re.IGNORECASE)
        if m:
            return float(m.group(1)) * 1e12
    return None


# ---------------------------------------------------------------------------
# Inverter chain sizing
# ---------------------------------------------------------------------------

def _size_inv_chain(
    w_n_min:     float,
    w_p_min:     float,
    cap_in_pF:   float,   # unit inverter input cap in pF
    cap_load_pF: float,   # total channel load in pF
) -> Tuple[int, List[Tuple[float, float]]]:
    """
    Optimal inverter chain sizing by Logical Effort with f=3.59 (parasitics
    included), non-inverting polarity.

    Returns (num_stages, [(w_n_um, w_p_um), ...]).
    Sizes are NOT clamped here — the netlist generator handles splitting
    oversized transistors into parallel instances.
    """
    ratio = w_p_min / w_n_min

    # Optimal stage count (f=3.59, parasitic cap included)
    N_opt = math.log(cap_load_pF / cap_in_pF) / math.log(3.59)
    N = max(1, round(N_opt))

    # Force even for non-inverting polarity
    if N % 2 != 0:
        if N_opt > N or N == 1:
            N += 1
        else:
            N -= 1
    N = max(2, N)

    f = (cap_load_pF / cap_in_pF) ** (1.0 / N)

    sizes = []
    for i in range(N):
        if i == 0:
            w_n = w_n_min
            w_p = w_p_min
        else:
            # Scale preserving PMOS/NMOS ratio
            nfin_n = w_n_min * (f ** i)
            nfin_p = nfin_n * ratio
            w_n = nfin_n
            w_p = nfin_p
        sizes.append((round(w_n, 4), round(w_p, 4)))

    return N, sizes


# ---------------------------------------------------------------------------
# Template helpers: extract unit subckt, generate N-lane wrapper
# ---------------------------------------------------------------------------

def _find_subckt_start(content: str, unit_name: str) -> int:
    """Return the index of the line starting with '.subckt <unit_name>'.

    Only matches at the beginning of a line (or buffer start) so that
    ``.subckt`` substrings appearing inside SPICE comments (`*` / `//`) are
    ignored. Returns -1 if not found.
    """
    pat = re.compile(rf'(^|\n)\.subckt\s+{re.escape(unit_name)}(\s|$)')
    m = pat.search(content)
    if m is None:
        return -1
    # m.start() is at the '\n' (or start-of-string). Move past the newline.
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
    # Include the full '.ends <unit_name>' line
    end_line_end = content.find('\n', end)
    if end_line_end == -1:
        end_line_end = len(content)
    return content[start:end_line_end]


def _gen_char_tcl(vdd: float, temp: float) -> str:
    """Generate char.tcl for txip with the given VDD and temperature."""
    return f"""\
# Liberate characterization script for txip
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

read_spice -format spectre ${{rundir}}/txip.scs

char_library -ccs -ecsm -cells ${{cells}}

write_library -overwrite ${{rundir}}/LIBRARY/txip_nldm.lib
write_verilog ${{rundir}}/LIBRARY/txip.v

write_datasheet -format text ${{rundir}}/DATASHEET/txip
"""

_RUN_SH_CMD = "liberate char.tcl 2>&1 | tee -a char.log >/dev/null\n"


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
        lines.append(f"M0 d g s b {model} w=w l=l m=nf")
        lines.append(f".ends {sub}")
    return "\n".join(lines) + "\n"


def _gen_model_sp(lib_path: str, lib_corner: str,
                  lib_corner2: Optional[str] = None,
                  fmt: str = "spice_lib") -> str:
    """Return the model-include (model.sp) text for the configured PDK.

    The format is chosen by ``cfg.process.model_include_format`` (``fmt``):
      * ``hspice_ptm``      — HSPICE BSIM .inc models + wrapper subckts.
      * ``spectre_include`` — Spectre include of lib_path + allModels.scs.
      * ``spice_lib``       — .lib "<lib_path>" <corner> (default).
    No foundry-specific strings appear here; everything comes from the config.
    """
    if fmt == "hspice_ptm":
        return _freepdk45_model_sp(lib_path, lib_corner)
    if fmt == "spectre_include":
        # Spectre mixed-mode: include the model file + allModels.scs alongside it,
        # then switch back to SPICE language for Liberate.
        model_dir = os.path.dirname(lib_path)
        all_models = os.path.join(model_dir, "allModels.scs")
        return (
            "*** Model include file ***\n"
            "simulator lang=spectre\n"
            f'include "{lib_path}"\n'
            f'include "{all_models}"\n'
            "simulator lang = spice\n"
        )
    # spice_lib (default): one (or two) .lib sections.
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


def _gen_txip_wrapper_scs(lane_count: int,
                          use_coupling_cap: bool = False,
                          signal_pairs: Optional[list] = None) -> str:
    """
    Generate '.subckt txip ... .ends txip' for lane_count TX lanes.

    Topology
    --------
    For each lane i:
        xtx{i}  IN_{i}  VDD VSS  v{i}_txpad         tx
        xch{i}  v{i}_txpad v{i}_bump v{i}_ipad_tx
                v{i}_tr0 v{i}_tr1 v{i}_tr2 v{i}_tr3
                v{i}_rxbump v{i}_rxpad PAD_{i} VSS  channel

    When use_coupling_cap is True, adjacent-lane pairs are stitched together
    with Cc caps at 6 physically parallel nodes each:

        4 trace Pi-ladder nodes   : v{i}_tr0..v{i}_tr3  (weight = cc_ratio_trace)
        2 interposer pad nodes    : v{i}_ipad_tx, v{i}_rxbump (weight = cc_ratio_pad)

    Pair topology:
      - If signal_pairs is provided (from bump map), pairs come from physical
        adjacency with coupling scaled by 1/distance and shielded pairs
        suppressed.
      - Otherwise, sequential pairs (0-1, 1-2, ..., N-2 to N-1) at unit
        distance.

    All Cc caps are gated on channel_rc==1 so they auto-disable when the
    channel Pi-ladder is disabled (1e-30 F stub).

    Port order: IN_0..N-1 (4 per row)  VDD VSS  PAD_0..N-1 (4 per row)
    """
    lines = [".subckt txip \\"]  # opening line with continuation

    # IN pins — 4 per row, always continued with backslash
    for start in range(0, lane_count, 4):
        end  = min(start + 4, lane_count)
        pins = "  ".join(f"IN_{i}" for i in range(start, end))
        lines.append(f"    {pins} \\")

    lines.append("    VDD VSS \\")

    # PAD pins — 4 per row; last row has no trailing backslash
    pad_starts = list(range(0, lane_count, 4))
    for k, start in enumerate(pad_starts):
        end    = min(start + 4, lane_count)
        pins   = "  ".join(f"PAD_{i}" for i in range(start, end))
        suffix = "" if k == len(pad_starts) - 1 else " \\"
        lines.append(f"    {pins}{suffix}")

    lines.append("")

    # ---- Per-lane TX drivers ----
    lines.append("* ---- TX drivers (driver + EQ, one per lane) ----")
    for i in range(lane_count):
        lines.append(f"xtx{i}  IN_{i}  VDD VSS  v{i}_txpad  tx")

    lines.append("")
    lines.append("* ---- Per-lane channel instances ----")
    for i in range(lane_count):
        lines.append(
            f"xch{i}  v{i}_txpad  v{i}_bump  v{i}_ipad_tx  "
            f"v{i}_tr0 v{i}_tr1 v{i}_tr2 v{i}_tr3  "
            f"v{i}_rxbump v{i}_rxpad  PAD_{i}  VSS  channel"
        )

    # ---- Build coupling pair list ----
    # Each entry: (lane_i, lane_j, scale_factor)
    # scale_factor = 1/dist for bump-map pairs, 1.0 for sequential fallback
    _SHIELD_SUPPRESSION = 0.1
    cc_pairs = []
    if use_coupling_cap and lane_count >= 2:
        if signal_pairs is not None:
            for sp in signal_pairs:
                scale = (1.0 / sp.dist_pitches) if sp.dist_pitches > 0 else 1.0
                if sp.shielded:
                    scale *= _SHIELD_SUPPRESSION
                cc_pairs.append((sp.lane_i, sp.lane_j, scale))
        else:
            # Sequential fallback (no bump map)
            for i in range(lane_count - 1):
                cc_pairs.append((i, i + 1, 1.0))

    # ---- Inter-lane coupling capacitance ----
    if lane_count >= 2:
        lines.append("")
        lines.append(
            "* --------------------------------------------------------------"
            "------------"
        )
        if use_coupling_cap:
            if signal_pairs is not None:
                lines.append(
                    "* Inter-lane coupling capacitance (bump-map topology, "
                    f"{len(cc_pairs)} pairs)"
                )
                lines.append(
                    "*   Coupling scaled by 1/distance; shielded pairs "
                    f"suppressed to {_SHIELD_SUPPRESSION}x."
                )
            else:
                lines.append(
                    "* Inter-lane coupling capacitance (sequential topology, "
                    f"{len(cc_pairs)} pairs)"
                )
            lines.append(
                "*   6 nodes per pair: 4 trace Pi-ladder + 2 interposer pad."
            )
            lines.append(
                "*   All caps gated on channel_rc==1 (auto-disable when "
                "channel_rc=0)."
            )
        else:
            lines.append(
                "* Inter-lane coupling capacitance (DISABLED via "
                "channel.coupling_cap.enabled=false)"
            )
        lines.append(
            "* --------------------------------------------------------------"
            "------------"
        )

        # Helper: (cap name prefix, internal node suffix, C_param, ratio_param)
        # Order matches the physical Pi-ladder: near-TX -> far-TX.
        coupling_specs = [
            ("Cc_ipad_tx",  "ipad_tx",  "C_pad_ipos_tx_fF", "cc_ratio_pad"),
            ("Cc_tr_near",  "tr0",      "C_tr_near_fF",     "cc_ratio_trace"),
            ("Cc_tr1",      "tr1",      "C_tr1_fF",         "cc_ratio_trace"),
            ("Cc_tr2",      "tr2",      "C_tr2_fF",         "cc_ratio_trace"),
            ("Cc_tr_far",   "tr3",      "C_tr_far_fF",      "cc_ratio_trace"),
            ("Cc_rxbump",   "rxbump",   "C_pad_ipos_fF",    "cc_ratio_pad"),
        ]
        for prefix, nsfx, c_param, r_param in coupling_specs:
            lines.append(f"* ---- {prefix} ({nsfx}) ----")
            for li, lj, scale in cc_pairs:
                name = f"{prefix}_{li}_{lj}"
                na   = f"v{li}_{nsfx}"
                nb   = f"v{lj}_{nsfx}"
                if use_coupling_cap:
                    expr = f"'channel_rc==1 ? {scale:.4f}*{r_param}*{c_param}*1e-15 : 1e-30'"
                else:
                    expr = "1e-30"
                lines.append(f"{name} {na} {nb} {expr}")

    lines.append("")
    lines.append(".ends txip")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SPICE netlist generator (reads template, patches eq, builds N-lane wrapper)
# ---------------------------------------------------------------------------

def _rebuild_inv_chain(unit_subckt: str,
                       inv_sizes: List[Tuple[float, float]],
                       w_max_um: float = 900.0,
                       spec: Optional[DeviceSpec] = None,
                       l_um: float = 0.5) -> str:
    """Replace the inverter chain inside the unit 'tx' subcircuit.

    The existing inverter instances (from '* Inverter stage 1' up to,
    but not including, '* Equalization network') are replaced with new
    instances matching *inv_sizes*.  Node naming follows the template
    convention: stage outputs are out1, out2, ..., with the last stage
    driving eq_node.  Transistors wider than *w_max_um* are split into
    parallel instances each at most *w_max_um* wide.
    """
    if spec is None:
        spec = DeviceSpec()
    # Locate boundaries: start of first inverter comment → start of EQ comment.
    # Support both "* Inverter stage 1" and "* Stage 1 — ..." templates.
    inv_start = unit_subckt.find("* Inverter stage 1")
    if inv_start == -1:
        inv_start = unit_subckt.find("* Stage 1")
    eq_start  = unit_subckt.find("* Equalization network")
    if inv_start == -1 or eq_start == -1:
        raise ValueError(
            "Cannot find inverter chain boundaries in unit subcircuit"
        )

    # Extract the model names from the existing first NMOS/PMOS instance.
    nmos_match = re.search(r'xnm\d+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\S+)\s', unit_subckt)
    pmos_match = re.search(r'xpm\d+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\S+)\s', unit_subckt)
    nmos_model = nmos_match.group(1) if nmos_match else "nmos"
    pmos_model = pmos_match.group(1) if pmos_match else "pmos"

    # Build new inverter chain text
    n = len(inv_sizes)
    chain_lines = []
    for i, (w_n, w_p) in enumerate(inv_sizes):
        stage_num = i + 1
        in_node  = "in" if i == 0 else f"out{i}"
        out_node = "eq_node" if i == n - 1 else f"out{stage_num}"
        chain_lines.append(f"* Inverter stage {stage_num}")
        _append_mos(chain_lines, "n", stage_num, out_node, in_node,
                    w_n, l_um, 1, nmos_model, w_max_um, spec=spec)
        _append_mos(chain_lines, "p", stage_num, out_node, in_node,
                    w_p, l_um, 1, pmos_model, w_max_um, spec=spec)
    chain_text = "\n".join(chain_lines) + "\n"

    return unit_subckt[:inv_start] + chain_text + unit_subckt[eq_start:]


def _patch_transistor_l(subckt: str, l_um: float, spec: DeviceSpec) -> str:
    """Replace hardcoded gate length in transistor instance lines with l_um."""
    if not l_um:
        return subckt
    if spec.is_finfet:
        l_nm = int(round(l_um * 1000))
        return re.sub(r'\bL=\d+n\b', f'L={l_nm}n', subckt)
    return re.sub(r'\bl=[\d.]+u\b', f'l={l_um}u', subckt)


def _gen_txip_scs(
    template_scs_path: str,
    lane_count:        int,
    use_eq:            bool,
    R_eq_ohm:          float,
    C_eq_fF:           float,
    use_channel_rc:    bool  = True,
    use_rx_pad_bump_rc: bool = True,
    ch_result=None,
    l_um:              float = 0.0,
    spec:              Optional[DeviceSpec] = None,
    inv_sizes_override: Optional[List[Tuple[float, float]]] = None,
    w_max_um:          float = 900.0,
    use_coupling_cap:  bool  = False,
    cc_ratio_trace:    float = 0.4,
    cc_ratio_pad:      float = 0.2,
    signal_pairs:      Optional[list] = None,
) -> str:
    """
    Return the full txip.scs SPICE text.

    Reads the unit 'tx' subcircuit verbatim from *template_scs_path*, patches
    the .param flags and element values, then appends a freshly-generated
    'txip' wrapper for *lane_count* lanes.

    Header .param lines patched:
        equalization  — 0 or 1
        channel_rc    — 0 or 1
        C_pad_chip_fF, R_pad_chip_ohm, C_esd_fF,
        C_bump_fF,     R_bump_ohm,
        C_tr_near_fF,  R_tr1_ohm, C_tr1_fF,
                       R_tr2_ohm, C_tr2_fF,
                       R_tr3_ohm, C_tr_far_fF,
        R_pad_ipos_ohm, C_pad_ipos_fF         — from ch_result

    Structure of the output:
        <header from template (simulator/include/.param lines, all patched)>
        .subckt tx in VDD VSS PAD
          ... inverter chain from template ...
          Req / Ceq  (equalization flag + values)
          channel RC (channel_rc flag + component values)
        .ends tx

        .subckt txip IN_0..N-1 VDD VSS PAD_0..N-1
          xtx0..N-1  tx instances
        .ends txip
    """
    with open(template_scs_path) as fh:
        template = fh.read()

    header      = _extract_scs_header(template, "tx")
    unit_subckt = _extract_unit_subckt(template, "tx")
    channel_subckt = _extract_unit_subckt(template, "channel")

    if spec is None:
        spec = DeviceSpec()

    # --- Override inverter chain sizing when tx_sizing sweep provides one ---
    if inv_sizes_override:
        unit_subckt = _rebuild_inv_chain(unit_subckt, inv_sizes_override, w_max_um,
                                         spec=spec, l_um=l_um)

    # --- Patch transistor gate length from config ---
    unit_subckt = _patch_transistor_l(unit_subckt, l_um, spec)

    # --- Patch .param equalization flag ---
    eq_flag = 1 if use_eq else 0
    header  = re.sub(r'\.param equalization=\d+',
                     f'.param equalization={eq_flag}',
                     header)

    # --- Patch .param channel_rc flag ---
    ch_flag = 1 if use_channel_rc else 0
    header  = re.sub(r'\.param channel_rc=\d+',
                     f'.param channel_rc={ch_flag}',
                     header)

    # --- Patch .param rx_pad_bump_rc flag ---
    rxpad_flag = 1 if use_rx_pad_bump_rc else 0
    header = re.sub(r'\.param rx_pad_bump_rc=\d+',
                    f'.param rx_pad_bump_rc={rxpad_flag}',
                    header)

    # --- Patch .param cc_ratio_trace / cc_ratio_pad ---
    # Values are used by the inter-lane coupling caps emitted in the
    # txip wrapper (when use_coupling_cap=True).  Always patch the header
    # so the params exist for the wrapper's expressions to reference.
    header = re.sub(r'\.param cc_ratio_trace=[\d.]+',
                    f'.param cc_ratio_trace={cc_ratio_trace:.4f}',
                    header)
    header = re.sub(r'\.param cc_ratio_pad=[\d.]+',
                    f'.param cc_ratio_pad={cc_ratio_pad:.4f}',
                    header)

    # --- When equalization is active, patch computed Req/Ceq values ---
    if use_eq:
        unit_subckt = re.sub(
            r"(Req\s+\S+\s+\S+\s+'equalization==1 \? )[\d.]+",
            rf"\g<1>{R_eq_ohm:.1f}",
            unit_subckt,
        )
        unit_subckt = re.sub(
            r"(Ceq\s+\S+\s+\S+\s+'equalization==1 \? )[\d.]+f",
            rf"\g<1>{C_eq_fF:.1f}f",
            unit_subckt,
        )

    # --- When channel RC is active, patch all component .param values ---
    if use_channel_rc and ch_result is not None:
        # Map: .param name -> value string
        ch_params = [
            ("C_pad_chip_fF",   f"{ch_result.pad_chiplet_C_fF:.4f}"),
            ("R_pad_chip_ohm",  f"{ch_result.pad_chiplet_R_ohm:.6f}"),
            ("C_esd_fF",        f"{ch_result.esd_C_fF:.4f}"),
            ("C_bump_fF",       f"{ch_result.bump_C_fF:.4f}"),
            ("R_bump_ohm",      f"{ch_result.bump_R_ohm:.6f}"),
            # Trace Pi-ladder: 3 equal segments
            ("C_tr_near_fF",    f"{ch_result.trace_C_fF / 6.0:.4f}"),
            ("R_tr1_ohm",       f"{ch_result.trace_R_ohm / 3.0:.6f}"),
            ("C_tr1_fF",        f"{ch_result.trace_C_fF / 3.0:.4f}"),
            ("R_tr2_ohm",       f"{ch_result.trace_R_ohm / 3.0:.6f}"),
            ("C_tr2_fF",        f"{ch_result.trace_C_fF / 3.0:.4f}"),
            ("R_tr3_ohm",       f"{ch_result.trace_R_ohm / 3.0:.6f}"),
            ("C_tr_far_fF",     f"{ch_result.trace_C_fF / 6.0:.4f}"),
            ("R_pad_ipos_ohm",    f"{ch_result.pad_interposer_R_ohm:.6f}"),
            ("C_pad_ipos_fF",     f"{ch_result.pad_interposer_C_fF:.4f}"),
            ("R_pad_ipos_tx_ohm", f"{ch_result.pad_interposer_R_ohm:.6f}"),
            ("C_pad_ipos_tx_fF",  f"{ch_result.pad_interposer_C_fF:.4f}"),
            # RX bump, chiplet pad, and ESD clamp (at the far end of txip.scs)
            ("C_rx_bump_fF",      f"{ch_result.bump_C_fF:.4f}"),
            ("R_rx_bump_ohm",     f"{ch_result.bump_R_ohm:.6f}"),
            ("C_rx_pad_chip_fF",  f"{ch_result.pad_chiplet_C_fF:.4f}"),
            ("R_rx_pad_chip_ohm", f"{ch_result.pad_chiplet_R_ohm:.6f}"),
            ("C_rx_esd_fF",       f"{ch_result.esd_C_fF:.4f}"),
        ]
        for param_name, value_str in ch_params:
            header = re.sub(
                rf'\.param {param_name}=[\d.]+',
                f'.param {param_name}={value_str}',
                header,
            )

    wrapper = _gen_txip_wrapper_scs(lane_count, use_coupling_cap=use_coupling_cap,
                                    signal_pairs=signal_pairs)
    return (
        header
        + unit_subckt
        + "\n\n"
        + channel_subckt
        + "\n\n"
        + wrapper
    )


def _append_mos(lines, mos_type, stage_idx, out_node, in_node,
                width_um, l_um, nf, model_name, w_max_um, spec=None):
    """Append NMOS or PMOS instance(s), splitting if width > w_max_um."""
    if spec is None:
        spec = DeviceSpec()
    prefix = "xnm" if mos_type == "n" else "xpm"
    bulk   = "VSS" if mos_type == "n" else "VDD"

    if spec.is_finfet:
        # FinFET: width_um actually holds nfin count; L in nm
        nfin_max = spec.nfin_max
        l_nm = int(round(l_um * 1000))
        nfin_total = max(1, int(round(width_um)))
        if nfin_total <= nfin_max:
            lines.append(
                f"{prefix}{stage_idx} {out_node} {in_node} {bulk} {bulk} "
                f"{model_name} L={l_nm}n nfin={nfin_total}"
            )
        else:
            remaining = nfin_total
            part = 0
            while remaining > 0:
                nf_inst = min(remaining, nfin_max)
                lines.append(
                    f"{prefix}{stage_idx}_{part} {out_node} {in_node} {bulk} {bulk} "
                    f"{model_name} L={l_nm}n nfin={nf_inst}"
                )
                remaining -= nf_inst
                part += 1
    else:
        nf_tok = f" nf={nf}" if spec.include_nf else ""
        if width_um <= w_max_um:
            lines.append(
                f"{prefix}{stage_idx} {out_node} {in_node} {bulk} {bulk} "
                f"{model_name} w={width_um}u l={l_um}u{nf_tok}"
            )
        else:
            remaining = width_um
            part = 0
            while remaining > 0:
                w = min(remaining, w_max_um)
                lines.append(
                    f"{prefix}{stage_idx}_{part} {out_node} {in_node} {bulk} {bulk} "
                    f"{model_name} w={w}u l={l_um}u{nf_tok}"
                )
                remaining -= w
                part += 1


# ---------------------------------------------------------------------------
# Liberate file generators
# ---------------------------------------------------------------------------


def _gen_template_tcl(
    load_pF,
    num_lanes:       int,
    slew_lower_rise: float, slew_upper_rise: float,
    slew_lower_fall: float, slew_upper_fall: float,
    input_slews_ns:  list,
    vdd:             float,
) -> str:
    """
    Generate template.tcl with index_2 patched to *load_pF* and all
    port/pin lists generated for *num_lanes* lanes.

    index_1: input slew values from config
    index_2: capacitance point(s) applied by Liberate at PAD_i pins.
             Accepts a single float or a list of floats for multi-point
             load sweeps (used by co_opt lookup-table mode).
             When channel_rc=1 is embedded in the netlist, pass the RX
             device input cap only (output_loads_pF[0] from config, typically
             0.1867 pF).  When channel_rc=0, pass total_shunt_C_fF/1000.
    VDD/VSS set from config.
    """
    slew_str = " ".join(str(s) for s in input_slews_ns)
    if isinstance(load_pF, (list, tuple)):
        load_str = " ".join(f"{v:.6f}" for v in load_pF)
        n_loads  = len(load_pF)
    else:
        load_str = f"{load_pF:.4f}"
        n_loads  = 1
    n_slews  = len(input_slews_ns)

    # ---- pin name lists ----
    in_pins  = [f"IN_{i}"  for i in range(num_lanes)]
    pad_pins = [f"PAD_{i}" for i in range(num_lanes)]

    def _pin_block(pins, indent=8, cols=4):
        """Format a list of pin names as a Tcl continuation block."""
        pad  = " " * indent
        rows = []
        for start in range(0, len(pins), cols):
            row = "  ".join(pins[start:start + cols])
            rows.append(pad + row)
        return " \\\n".join(rows)

    in_block  = _pin_block(in_pins)
    pad_block = _pin_block(pad_pins)

    # ---- define_arc lines (diagonal, R+F per lane) ----
    full_pinlist = " ".join(in_pins) + " " + " ".join(pad_pins)
    arc_lines = []
    for i in range(num_lanes):
        for edge in ("R", "F"):
            in_vec  = " ".join(edge if j == i else "0" for j in range(num_lanes))
            pad_vec = " ".join(edge if j == i else "X" for j in range(num_lanes))
            arc_lines.append(
                f"define_arc -type combinational "
                f"-related_pin IN_{i} -pin PAD_{i} "
                f"-pinlist {{{full_pinlist}}} "
                f"-vector {{{in_vec} {pad_vec}}} txip"
            )
    arcs = "\n".join(arc_lines)

    template_name_sfx = f"{n_slews}x{n_loads}"

    # Leakage conditions: quiescent all-low and all-high states
    # Liberty 'when' syntax:  !PIN * !PIN ...  or  PIN * PIN ...
    leakage_low_cond  = " * ".join(f"!{p}" for p in in_pins)
    leakage_high_cond = " * ".join(in_pins)

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

set cells {{ txip }}

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
        -input  {{{in_block}}} \\
        -output {{{pad_block}}} \\
        -pad    {{{pad_block}}} \\
        -bidi {{}} -clock {{}} -async {{}} \\
        -pinlist {{{in_block} \\
                  {pad_block} \\
                  VDD VSS}} \\
        -constraint  constraint_template_3x3 \\
        -delay       delay_template_{template_name_sfx} \\
        -power       power_template_{template_name_sfx} \\
        txip

{arcs}

set_vdd VDD {vdd}
set_gnd VSS 0.0

set_var extsim_save_failed deck
set_var extsim_save_passed all

# Leakage states: quiescent all-low and all-high (required for -io mode)
# Must appear before read_spice and char_library
define_leakage -when "{leakage_low_cond}" txip
define_leakage -when "{leakage_high_cond}" txip
"""


# ---------------------------------------------------------------------------
# CharLib file generators
# ---------------------------------------------------------------------------

def _gen_txip_sp(
    lane_count:  int,
    inv_sizes:   List[Tuple[float, float]],   # [(w_n_um, w_p_um), ...] per stage
    use_eq:      bool,
    R_eq_ohm:    float,
    C_eq_fF:     float,
    l_um:        float,
    nmos_name:   str,
    pmos_name:   str,
    nf:          int,
    spec:        Optional[DeviceSpec] = None,
    w_max_um:    float = 900.0,
    nf_auto:     bool = False,
) -> str:
    """
    Generate a plain SPICE netlist for the TX cell (no channel RC).

    Produces two subckts:
      .subckt tx IN VDD VSS TXPAD   — unit driver + optional EQ network
      .subckt txip IN_0…N-1 PAD_0…N-1 VDD VSS — N-lane wrapper

    When nf_auto=True, gate fingers are split to ~1μm per finger so that the
    BSIM4 gate resistance (rshg * W_finger / (3*L)) stays below ~3ohm, avoiding
    numerical stiffness in transient simulations (CharLib backend only).
    """
    if spec is None:
        spec = DeviceSpec()

    num_stages = len(inv_sizes)
    lines = ["* TX unit driver + N-lane wrapper — generated by CLIPGen",
             "* simulator lang=spice",
             ""]
    lines.append(".subckt tx IN VDD VSS TXPAD")

    for s, (w_n, w_p) in enumerate(inv_sizes):
        in_node  = "IN"   if s == 0 else f"out{s}"
        out_node = f"out{s + 1}" if s < num_stages - 1 else "eq_in"
        if nf_auto and not spec.is_finfet:
            nf_n = max(1, math.ceil(w_n))
            nf_p = max(1, math.ceil(w_p))
            _append_mos(lines, "n", s + 1, out_node, in_node, w_n / nf_n, l_um,
                        nf_n, nmos_name, w_max_um, spec=spec)
            _append_mos(lines, "p", s + 1, out_node, in_node, w_p / nf_p, l_um,
                        nf_p, pmos_name, w_max_um, spec=spec)
        else:
            _append_mos(lines, "n", s + 1, out_node, in_node, w_n, l_um, nf,
                        nmos_name, w_max_um, spec=spec)
            _append_mos(lines, "p", s + 1, out_node, in_node, w_p, l_um, nf,
                        pmos_name, w_max_um, spec=spec)

    if use_eq:
        lines.append(f"Req eq_in TXPAD {R_eq_ohm:.4f}")
        lines.append(f"Ceq TXPAD VSS {C_eq_fF:.4f}f")
    else:
        lines.append("Req eq_in TXPAD 0.001")
        lines.append("Ceq TXPAD VSS 1e-30")

    lines += [".ends tx", ""]

    in_pins  = [f"IN_{i}"  for i in range(lane_count)]
    pad_pins = [f"PAD_{i}" for i in range(lane_count)]
    port_str = " ".join(in_pins) + " " + " ".join(pad_pins) + " VDD VSS"
    lines.append(f".subckt txip {port_str}")
    for i in range(lane_count):
        lines.append(f"xtx{i} IN_{i} VDD VSS PAD_{i} tx")
    lines += [".ends txip", ""]

    return "\n".join(lines)


def _gen_channel_spice_section(
    ch_result,
    include_rx_bump_pad: bool,
    node_in: str,
    node_out: str,
) -> List[str]:
    """Return SPICE element lines for the channel Pi-ladder.

    Topology from node_in (TXPAD) to node_out (RX device input):
      C_pad_chip (shunt) → R_bump → C_bump/C_tr_near (shunt) →
      R_tr1 → C_tr1 (shunt) → R_tr2 → C_tr2 (shunt) →
      R_tr3 → C_tr_far (shunt) → R_pad_ipos → C_pad_ipos (shunt) →
      [if include_rx_bump_pad: R_rx_bump → C_rx_bump (shunt) → R_rx_pad → C_rx_pad (shunt)] →
      C_rx_esd (shunt at node_out)

    Values match the Spectre parameter substitution in _gen_txip_scs:
      trace split into 3 equal R segments and Pi-ladder shunt caps
      (C_near = C_far = trace_C_fF/6; C_mid1 = C_mid2 = trace_C_fF/3).
    """
    lines = []

    # TX chiplet pad (shunt at input)
    lines.append(f"Cpad_chip {node_in} VSS {ch_result.pad_chiplet_C_fF:.4f}f")

    # TX bump
    lines.append(f"Rbump {node_in} n_bump {ch_result.bump_R_ohm:.6f}")
    lines.append(f"Cbump n_bump VSS {ch_result.bump_C_fF:.4f}f")

    # Trace Pi-ladder: near shunt + 3 R–C segments + far shunt
    lines.append(f"Ctr_near n_bump VSS {ch_result.trace_C_fF / 6.0:.4f}f")
    lines.append(f"Rtr1 n_bump n_tr1 {ch_result.trace_R_ohm / 3.0:.6f}")
    lines.append(f"Ctr1 n_tr1 VSS {ch_result.trace_C_fF / 3.0:.4f}f")
    lines.append(f"Rtr2 n_tr1 n_tr2 {ch_result.trace_R_ohm / 3.0:.6f}")
    lines.append(f"Ctr2 n_tr2 VSS {ch_result.trace_C_fF / 3.0:.4f}f")
    lines.append(f"Rtr3 n_tr2 n_tr3 {ch_result.trace_R_ohm / 3.0:.6f}")
    lines.append(f"Ctr_far n_tr3 VSS {ch_result.trace_C_fF / 6.0:.4f}f")

    # TX interposer pad → either terminates at node_out or continues through RX bump/pad
    if include_rx_bump_pad:
        lines.append(f"Rpad_ipos n_tr3 n_ipad {ch_result.pad_interposer_R_ohm:.6f}")
        lines.append(f"Cpad_ipos n_ipad VSS {ch_result.pad_interposer_C_fF:.4f}f")
        # RX bump
        lines.append(f"Rrx_bump n_ipad n_rxbump {ch_result.bump_R_ohm:.6f}")
        lines.append(f"Crx_bump n_rxbump VSS {ch_result.bump_C_fF:.4f}f")
        # RX chiplet pad → connects to output node
        lines.append(f"Rrx_pad n_rxbump {node_out} {ch_result.pad_chiplet_R_ohm:.6f}")
        lines.append(f"Crx_pad {node_out} VSS {ch_result.pad_chiplet_C_fF:.4f}f")
    else:
        lines.append(f"Rpad_ipos n_tr3 {node_out} {ch_result.pad_interposer_R_ohm:.6f}")
        lines.append(f"Cpad_ipos {node_out} VSS {ch_result.pad_interposer_C_fF:.4f}f")

    # RX ESD clamp at output node (always present)
    lines.append(f"Crx_esd {node_out} VSS {ch_result.esd_C_fF:.4f}f")

    return lines


def _gen_txip_sp_with_channel(
    lane_count:          int,
    inv_sizes:           List[Tuple[float, float]],
    use_eq:              bool,
    R_eq_ohm:            float,
    C_eq_fF:             float,
    l_um:                float,
    nmos_name:           str,
    pmos_name:           str,
    nf:                  int,
    ch_result,
    include_rx_bump_pad: bool,
    spec:                Optional[DeviceSpec] = None,
    w_max_um:            float = 900.0,
    nf_auto:             bool = False,
) -> str:
    """
    Generate a SPICE netlist for the TX cell with the channel Pi-ladder appended.

    Identical to _gen_txip_sp for the inverter chain and EQ section.  After the
    EQ, TXPAD is an internal intermediate node; _gen_channel_spice_section appends
    the Pi-ladder from TXPAD to RX_IN, which becomes the unit subckt output port.
    The N-lane txip wrapper maps PAD_i → the RX_IN port of each unit instance.
    """
    if spec is None:
        spec = DeviceSpec()

    num_stages = len(inv_sizes)
    lines = ["* TX unit driver + channel RC + N-lane wrapper — generated by CLIPGen",
             "* simulator lang=spice",
             ""]
    lines.append(".subckt tx IN VDD VSS RX_IN")

    for s, (w_n, w_p) in enumerate(inv_sizes):
        in_node  = "IN"   if s == 0 else f"out{s}"
        out_node = f"out{s + 1}" if s < num_stages - 1 else "eq_in"
        if nf_auto and not spec.is_finfet:
            nf_n = max(1, math.ceil(w_n))
            nf_p = max(1, math.ceil(w_p))
            _append_mos(lines, "n", s + 1, out_node, in_node, w_n / nf_n, l_um,
                        nf_n, nmos_name, w_max_um, spec=spec)
            _append_mos(lines, "p", s + 1, out_node, in_node, w_p / nf_p, l_um,
                        nf_p, pmos_name, w_max_um, spec=spec)
        else:
            _append_mos(lines, "n", s + 1, out_node, in_node, w_n, l_um, nf,
                        nmos_name, w_max_um, spec=spec)
            _append_mos(lines, "p", s + 1, out_node, in_node, w_p, l_um, nf,
                        pmos_name, w_max_um, spec=spec)

    if use_eq:
        lines.append(f"Req eq_in TXPAD {R_eq_ohm:.4f}")
        lines.append(f"Ceq TXPAD VSS {C_eq_fF:.4f}f")
    else:
        lines.append("Req eq_in TXPAD 0.001")
        lines.append("Ceq TXPAD VSS 1e-30")

    # Channel Pi-ladder: TXPAD (internal) → RX_IN (output port)
    lines += _gen_channel_spice_section(ch_result, include_rx_bump_pad, "TXPAD", "RX_IN")

    lines += [".ends tx", ""]

    # N-lane wrapper: PAD_i maps positionally to RX_IN of each unit instance
    in_pins  = [f"IN_{i}"  for i in range(lane_count)]
    pad_pins = [f"PAD_{i}" for i in range(lane_count)]
    port_str = " ".join(in_pins) + " " + " ".join(pad_pins) + " VDD VSS"
    lines.append(f".subckt txip {port_str}")
    for i in range(lane_count):
        lines.append(f"xtx{i} IN_{i} VDD VSS PAD_{i} tx")
    lines += [".ends txip", ""]

    return "\n".join(lines)


def _gen_charlib_yaml(
    lib_name:        str,
    results_dir:     str,
    netlist_path:    str,
    model_path:      str,
    input_slews_ns:  list,
    output_loads_pF: list,
    vdd:             float,
    temp:            float,
    lane_count:      int,
    cell_name:       str = "txip",
    logic_threshold_low:  float = 0.2,
    logic_threshold_high: float = 0.8,
) -> str:
    """Generate a CharLib YAML configuration for the tx or txip cell."""
    if cell_name == "tx":
        in_pins  = ["IN"]
        pad_pins = ["TXPAD"]
        funcs    = ["TXPAD = IN"]
    else:
        in_pins  = [f"IN_{i}"  for i in range(lane_count)]
        pad_pins = [f"PAD_{i}" for i in range(lane_count)]
        funcs    = [f"PAD_{i} = IN_{i}" for i in range(lane_count)]

    slew_str  = "[" + ", ".join(str(s) for s in input_slews_ns)  + "]"
    load_str  = "[" + ", ".join(str(l) for l in output_loads_pF) + "]"
    in_str    = "[" + ", ".join(in_pins)  + "]"
    out_str   = "[" + ", ".join(pad_pins) + "]"
    func_str  = "[" + ", ".join(f'"{f}"' for f in funcs) + "]"

    return f"""\
settings:
  lib_name: {lib_name}
  results_dir: {results_dir}
  units:
    leakage_power: nW
    energy: pJ
  named_nodes:
    primary_power:
      name: VDD
      voltage: {vdd}
    primary_ground:
      name: VSS
      voltage: 0.0
    nwell:
      name: VNW
      voltage: {vdd}
    pwell:
      name: VPW
      voltage: 0.0
  logic_thresholds:
    low: {logic_threshold_low}
    high: {logic_threshold_high}
  temperature: {temp}
  cell_defaults:
    netlist: {netlist_path}
    models:
      - {model_path}
    data_slews: {slew_str}
    loads: {load_str}
    plots: none
  simulation:
    backend: ngspice-shared
    combinational_leakage_procedure: combinational_leakage
    combinational_dynamic_power_procedure: combinational_dynamic_power
    input_capacitance_procedure: charge_integration
  debug: false

cells:
  {cell_name}:
    inputs: {in_str}
    outputs: {out_str}
    functions: {func_str}
"""


# ---------------------------------------------------------------------------
# TX-only (TX+EQ, no channel) run
# ---------------------------------------------------------------------------

def gen_tx_only_run(cfg, ch_result, eq_result, tx_result: TxNetlistResult,
                    run_dir: str,
                    rx_cap_in_pF: Optional[float] = None) -> TxNetlistResult:
    """Dispatch to the selected backend."""
    if cfg.backend == "charlib":
        return _gen_tx_only_run_charlib(cfg, ch_result, eq_result, tx_result,
                                        run_dir, rx_cap_in_pF=rx_cap_in_pF)
    return _gen_tx_only_run_liberate(cfg, ch_result, eq_result, tx_result,
                                     run_dir, rx_cap_in_pF=rx_cap_in_pF)


def _gen_tx_only_run_charlib(cfg, ch_result, eq_result, tx_result: TxNetlistResult,
                             run_dir: str,
                             rx_cap_in_pF: Optional[float] = None) -> TxNetlistResult:
    """
    CharLib backend: run the TX+EQ-only ("no channel") characterization.

    This produces a direct measurement of TX-only propagation delay and
    output slew, without the channel RC ladder. The result is used together
    with the RX delay to form the UCIe TX+RX latency constraint (excluding
    channel). Mirrors _gen_tx_only_run_liberate.

    Skips silently (no-op) if channel_rc_integrated is False, because the
    regular TX CharLib run (tx/, from _gen_netlist_charlib) already IS the
    no-channel characterization in that case.
    """
    if not tx_result.channel_rc_integrated:
        return tx_result

    tx   = cfg.transistor
    cl   = cfg.charlib
    proc = cfg.process

    tx_only_dir = os.path.join(run_dir, "tx_only")
    os.makedirs(tx_only_dir, exist_ok=True)
    tx_result.tx_only_dir = tx_only_dir

    rx_load_pF = rx_cap_in_pF if rx_cap_in_pF is not None else cl.output_loads_pF[0]
    rx_load_scalar = rx_load_pF[0] if isinstance(rx_load_pF, (list, tuple)) else rx_load_pF
    print(f"  [TX-only/charlib] Using load: {rx_load_scalar:.5f} pF")

    sp_text = _gen_txip_sp(
        lane_count  = cfg.link.lane_count,
        inv_sizes   = tx_result.inverter_sizes,
        use_eq      = tx_result.use_equalization,
        R_eq_ohm    = tx_result.R_eq_ohm,
        C_eq_fF     = tx_result.C_eq_fF,
        l_um        = tx.l_um,
        nmos_name   = tx.nmos_name,
        pmos_name   = tx.pmos_name,
        nf          = tx.nf,
        spec        = DeviceSpec.from_cfg(cfg),
        w_max_um    = tx.w_max_um,
        nf_auto     = True,
    )
    _write(tx_only_dir, "txip.sp", sp_text)

    model_text = _gen_model_sp(proc.lib_path, proc.lib_corner,
                               getattr(proc, 'lib_corner2', None),
                               fmt=proc.model_include_format)
    model_text = re.sub(r'^\s*simulator\s+lang\s*=.*$', '', model_text,
                        flags=re.MULTILINE)
    _write(tx_only_dir, "model.sp", model_text)

    yaml_text = _gen_charlib_yaml(
        lib_name        = "txip_only_nldm",
        results_dir     = "LIBRARY",
        netlist_path    = "txip.sp",
        model_path      = "model.sp",
        input_slews_ns  = cl.input_slews_ns,
        output_loads_pF = [rx_load_scalar],
        vdd             = proc.vdd,
        temp            = proc.temp,
        lane_count      = cfg.link.lane_count,
        logic_threshold_low  = cl.logic_threshold_low,
        logic_threshold_high = cl.logic_threshold_high,
    )
    _write(tx_only_dir, "charlib_only.yaml", yaml_text)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["NGSPICE_LIBRARY_PATH"] = cl.ngspice_library_path

    print(f"  [TX-only/charlib] Running CharLib in {tx_only_dir} ...")
    proc_only = subprocess.run(
        [cl.charlib_executable, "run", "charlib_only.yaml"],
        cwd=tx_only_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    print(proc_only.stdout)
    if proc_only.returncode != 0:
        raise RuntimeError(
            f"[TX-only/charlib] CharLib failed with exit code {proc_only.returncode} "
            f"in {tx_only_dir}"
        )
    print("  [TX-only/charlib] CharLib complete.")

    # Parse TX-only delay and slew straight from the .lib file (CharLib runs
    # have no DATASHEET, unlike Liberate's _parse_tx_only_results source).
    import lib_parser
    lib_path = os.path.join(tx_only_dir, "LIBRARY", "txip_only_nldm.lib")
    try:
        timing = lib_parser.parse_lib_timing(lib_path)
        tx_result.tx_only_delay_rr_ns = timing.get("avg_cell_rise_ns", 0.0)
        tx_result.tx_only_delay_ff_ns = timing.get("avg_cell_fall_ns", 0.0)
        tx_result.tx_only_slew_rr_ns  = timing.get("avg_rise_transition_ns", 0.0)
        tx_result.tx_only_slew_ff_ns  = timing.get("avg_fall_transition_ns", 0.0)
    except Exception as e:
        warnings.warn(f"[TX-only/charlib] failed to parse .lib timing: {e}")

    return tx_result


def _gen_tx_only_run_liberate(cfg, ch_result, eq_result, tx_result: TxNetlistResult,
                    run_dir: str,
                    rx_cap_in_pF: Optional[float] = None) -> TxNetlistResult:
    """
    Run a second Liberate characterization with TX+EQ only (no channel RC).

    This produces a direct measurement of TX-only propagation delay and
    output slew, without the channel RC ladder.  The result is used together
    with the RX delay to form the UCIe TX+RX latency constraint (excluding
    channel).

    The function:
      1. Creates run_dir/tx_only/
      2. Generates txip.scs with channel_rc=0 (same inv chain, same EQ)
      3. Generates template.tcl with load = RX input cap
      4. Copies model.sp, define_leafcell.tcl, char.tcl, run.sh
      5. Runs Liberate
      6. Parses TX-only delay/slew from the datasheet
      7. Populates tx_result.tx_only_* fields and returns it

    Skips silently (no error) if channel_rc_integrated is False, because
    the regular TX Liberate run already measures TX-only delay in that case.
    """
    if not tx_result.channel_rc_integrated:
        # No channel in the regular run → regular TX delay IS the TX-only delay.
        # Copy the values from the main run's datasheet (parsed later by
        # get_metrics).  Leave tx_only_dir = None as a sentinel.
        return tx_result

    tx = cfg.transistor
    lib = cfg.liberate
    proc = cfg.process

    tx_only_dir = os.path.join(run_dir, "tx_only")
    os.makedirs(tx_only_dir, exist_ok=True)
    tx_result.tx_only_dir = tx_only_dir

    template_scs_dir = os.path.join(lib.template_dir, "txip")
    template_scs_path = os.path.join(template_scs_dir, "txip.scs")

    # Determine inverter sizes override from the main run
    inv_sizes_override = tx_result.inverter_sizes if tx_result.inverter_sizes else None

    # 1. txip.scs — same TX+EQ, but channel_rc=0
    cc_enabled, cc_ratio_trace, cc_ratio_pad = _coupling_cap_from_cfg(cfg)
    scs_text = _gen_txip_scs(
        template_scs_path  = template_scs_path,
        lane_count         = cfg.link.lane_count,
        use_eq             = tx_result.use_equalization,
        R_eq_ohm           = tx_result.R_eq_ohm,
        C_eq_fF            = tx_result.C_eq_fF,
        use_channel_rc      = False,          # <-- no channel
        use_rx_pad_bump_rc  = False,          # <-- no RX parasitics (channel off anyway)
        ch_result           = ch_result,
        l_um               = tx.l_um,
        spec               = DeviceSpec.from_cfg(cfg),
        inv_sizes_override = inv_sizes_override,
        w_max_um           = tx.w_max_um,
        use_coupling_cap   = cc_enabled,
        cc_ratio_trace     = cc_ratio_trace,
        cc_ratio_pad       = cc_ratio_pad,
        signal_pairs       = _tx_signal_pairs_from_cfg(cfg),
    )
    _write(tx_only_dir, "txip.scs", scs_text)

    # 2. model.sp
    model_text = _gen_model_sp(proc.lib_path, proc.lib_corner,
                               getattr(proc, 'lib_corner2', None),
                               fmt=proc.model_include_format)
    _write(tx_only_dir, "model.sp", model_text)

    # 3. template.tcl — load = RX input cap (the actual far-end load)
    if rx_cap_in_pF is not None:
        load_pF = rx_cap_in_pF
    else:
        load_pF = lib.output_loads_pF[0]
    print(f"  [TX-only] Using load: {load_pF:.5f} pF")

    template_text = _gen_template_tcl(
        load_pF         = load_pF,
        num_lanes       = cfg.link.lane_count,
        slew_lower_rise = lib.slew_lower_rise,
        slew_upper_rise = lib.slew_upper_rise,
        slew_lower_fall = lib.slew_lower_fall,
        slew_upper_fall = lib.slew_upper_fall,
        input_slews_ns  = lib.input_slews_ns,
        vdd             = proc.vdd,
    )
    _write(tx_only_dir, "template.tcl", template_text)

    # 4. define_leafcell.tcl
    _write(tx_only_dir, "define_leafcell.tcl",
           device.define_leafcell_text(lib.template_dir, "txip"))

    # 5. char.tcl
    _write(tx_only_dir, "char.tcl", _gen_char_tcl(proc.vdd, proc.temp))

    # 6. run.sh
    _write(tx_only_dir, "run.sh", _build_run_sh(cfg))
    os.chmod(os.path.join(tx_only_dir, "run.sh"), 0o755)

    # 7. Run Liberate
    print(f"  [TX-only] Running Liberate in {tx_only_dir} ...")
    shell = "bash"
    proc_lib = subprocess.Popen(
        [shell, "run.sh"],
        cwd=tx_only_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    for line in proc_lib.stdout:
        print(line, end="")
    proc_lib.wait()
    if proc_lib.returncode != 0:
        raise RuntimeError(
            f"[TX-only] Liberate failed with exit code {proc_lib.returncode} "
            f"in {tx_only_dir}"
        )
    print(f"  [TX-only] Liberate complete.")

    # 8. Parse delay and slew from the TX-only datasheet
    ds_path = os.path.join(tx_only_dir, "DATASHEET", "txip.txt")
    _parse_tx_only_results(tx_result, ds_path,
                           os.path.join(tx_only_dir, "LIBRARY", "txip_nldm.lib"))

    return tx_result


def _parse_tx_only_results(tx_result: TxNetlistResult, ds_path: str,
                           lib_path: str) -> None:
    """Parse TX-only delay and slew from Liberate datasheet and .lib file."""
    if not os.path.exists(ds_path):
        warnings.warn(f"[TX-only] Datasheet not found: {ds_path}")
        return

    with open(ds_path) as f:
        content = f.read()

    # Delay: average across all lanes for RR and FF
    for edge, attr in (("RR", "tx_only_delay_rr_ns"), ("FF", "tx_only_delay_ff_ns")):
        rows = re.findall(
            rf'\|\s*txip\s*\|\s*IN_\d+->PAD_\d+\({edge}\)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
            content
        )
        if rows:
            all_vals = [float(v) for r in rows for v in r]
            setattr(tx_result, attr, sum(all_vals) / len(all_vals))

    # Output slew: try datasheet first, then .lib file
    for edge, attr in (("RR", "tx_only_slew_rr_ns"), ("FF", "tx_only_slew_ff_ns")):
        rows = re.findall(
            rf'\|\s*txip\s*\|\s*PAD_\d+\({edge}\)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
            content
        )
        if rows:
            all_vals = [float(v) for r in rows for v in r]
            setattr(tx_result, attr, sum(all_vals) / len(all_vals))

    # Fall back to .lib for slew if not in datasheet
    if tx_result.tx_only_slew_rr_ns == 0.0 and os.path.exists(lib_path):
        with open(lib_path) as f:
            lib_content = f.read()
        # Parse rise_transition table value
        m = re.search(r'rise_transition\s*\([^)]*\)\s*\{\s*values\s*\(\s*"([\d.]+)"', lib_content)
        if m:
            tx_result.tx_only_slew_rr_ns = float(m.group(1))
        m = re.search(r'fall_transition\s*\([^)]*\)\s*\{\s*values\s*\(\s*"([\d.]+)"', lib_content)
        if m:
            tx_result.tx_only_slew_ff_ns = float(m.group(1))


def gen_tx_channel_run(cfg, ch_result, eq_result, tx_result: TxNetlistResult,
                       run_dir: str,
                       rx_cap_in_pF: Optional[float] = None) -> TxNetlistResult:
    """Dispatch to the selected backend."""
    if cfg.backend == "charlib":
        return _gen_tx_channel_run_charlib(cfg, ch_result, eq_result, tx_result,
                                           run_dir, rx_cap_in_pF=rx_cap_in_pF)
    return _gen_tx_channel_run_liberate(cfg, ch_result, eq_result, tx_result,
                                        run_dir, rx_cap_in_pF=rx_cap_in_pF)


def _gen_tx_channel_run_charlib(cfg, ch_result, eq_result, tx_result: TxNetlistResult,
                                run_dir: str,
                                rx_cap_in_pF: Optional[float] = None) -> TxNetlistResult:
    """
    CharLib backend: run the TX+channel ("no RX bump/pad") characterization.

    Characterizes TX gate + EQ + channel Pi-ladder (TX bump, trace, interposer pad,
    RX ESD cap). The external load is the RX device input cap. Switching energy
    from this run minus the TX-only run (tx_only/, from _gen_tx_only_run_charlib)
    gives the channel dissipation.

    Written to run_dir/tx_channel/. Mirrors _gen_tx_channel_run_liberate, including
    the channel_rc_integrated gate. On return, tx_result.tx_channel_dir is set.
    """
    if not tx_result.channel_rc_integrated:
        return tx_result

    tx   = cfg.transistor
    cl   = cfg.charlib
    proc = cfg.process

    rx_load_pF = rx_cap_in_pF if rx_cap_in_pF is not None else cl.output_loads_pF[0]

    model_text = _gen_model_sp(proc.lib_path, proc.lib_corner,
                               getattr(proc, 'lib_corner2', None),
                               fmt=proc.model_include_format)
    model_text = re.sub(r'^\s*simulator\s+lang\s*=.*$', '', model_text,
                        flags=re.MULTILINE)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["NGSPICE_LIBRARY_PATH"] = cl.ngspice_library_path

    # TX + channel Pi-ladder, no RX bump/pad.
    tx_channel_dir = os.path.join(run_dir, "tx_channel")
    os.makedirs(tx_channel_dir, exist_ok=True)

    _write(tx_channel_dir, "txip_ch.sp", _gen_txip_sp_with_channel(
        lane_count          = cfg.link.lane_count,
        inv_sizes           = tx_result.inverter_sizes,
        use_eq              = tx_result.use_equalization,
        R_eq_ohm            = tx_result.R_eq_ohm,
        C_eq_fF             = tx_result.C_eq_fF,
        l_um                = tx.l_um,
        nmos_name           = tx.nmos_name,
        pmos_name           = tx.pmos_name,
        nf                  = tx.nf,
        ch_result           = ch_result,
        spec                = DeviceSpec.from_cfg(cfg),
        w_max_um            = tx.w_max_um,
        nf_auto             = True,
        include_rx_bump_pad = False,
    ))
    _write(tx_channel_dir, "model.sp", model_text)
    _write(tx_channel_dir, "charlib_ch.yaml", _gen_charlib_yaml(
        lib_name        = "txip_ch_nldm",
        results_dir     = "LIBRARY",
        netlist_path    = "txip_ch.sp",
        model_path      = "model.sp",
        input_slews_ns  = cl.input_slews_ns,
        output_loads_pF = [rx_load_pF],
        vdd             = proc.vdd,
        temp            = proc.temp,
        lane_count      = cfg.link.lane_count,
        logic_threshold_low  = cl.logic_threshold_low,
        logic_threshold_high = cl.logic_threshold_high,
    ))

    print(f"  [TX+channel/charlib] Running CharLib in {tx_channel_dir} ...")
    proc_ch = subprocess.run(
        [cl.charlib_executable, "run", "charlib_ch.yaml"],
        cwd=tx_channel_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    print(proc_ch.stdout)
    if proc_ch.returncode != 0:
        raise RuntimeError(
            f"[TX+channel/charlib] CharLib failed with exit code {proc_ch.returncode} "
            f"in {tx_channel_dir}"
        )
    print("  [TX+channel/charlib] CharLib complete.")

    tx_result.tx_channel_dir = tx_channel_dir
    return tx_result


def _gen_tx_channel_run_liberate(cfg, ch_result, eq_result, tx_result: TxNetlistResult,
                       run_dir: str,
                       rx_cap_in_pF: Optional[float] = None) -> TxNetlistResult:
    """
    Run a Liberate characterization with TX+EQ+channel but WITHOUT the RX
    bump and chiplet pad RC (rx_pad_bump_rc=0).

    This measures E_tx + E_channel in isolation.  Subtracting the tx_only
    energy from this result gives E_channel; subtracting this from the full
    tx result (rx_pad_bump_rc=1) gives E_rxpad_bump attributed to the RX.

    The function:
      1. Creates run_dir/tx_channel/
      2. Generates txip.scs with channel_rc=1, rx_pad_bump_rc=0
      3. Generates template.tcl with load = RX ESD+device cap (stripped load)
      4. Copies model.sp, define_leafcell.tcl, char.tcl, run.sh
      5. Runs Liberate
      6. Parses TX+channel delay and switching power
      7. Populates tx_result.tx_channel_* fields and returns it
    """
    if not tx_result.channel_rc_integrated:
        return tx_result

    tx = cfg.transistor
    lib = cfg.liberate
    proc = cfg.process

    tx_channel_dir = os.path.join(run_dir, "tx_channel")
    os.makedirs(tx_channel_dir, exist_ok=True)
    tx_result.tx_channel_dir = tx_channel_dir

    template_scs_dir = os.path.join(lib.template_dir, "txip")
    template_scs_path = os.path.join(template_scs_dir, "txip.scs")

    inv_sizes_override = tx_result.inverter_sizes if tx_result.inverter_sizes else None

    # 1. txip.scs — channel_rc=1 but rx_pad_bump_rc=0
    cc_enabled, cc_ratio_trace, cc_ratio_pad = _coupling_cap_from_cfg(cfg)
    scs_text = _gen_txip_scs(
        template_scs_path   = template_scs_path,
        lane_count          = cfg.link.lane_count,
        use_eq              = tx_result.use_equalization,
        R_eq_ohm            = tx_result.R_eq_ohm,
        C_eq_fF             = tx_result.C_eq_fF,
        use_channel_rc      = True,
        use_rx_pad_bump_rc  = False,         # <-- no RX bump/pad in this run
        ch_result           = ch_result,
        l_um                = tx.l_um,
        spec                = DeviceSpec.from_cfg(cfg),
        inv_sizes_override  = inv_sizes_override,
        w_max_um            = tx.w_max_um,
        use_coupling_cap    = cc_enabled,
        cc_ratio_trace      = cc_ratio_trace,
        cc_ratio_pad        = cc_ratio_pad,
        signal_pairs        = _tx_signal_pairs_from_cfg(cfg),
    )
    _write(tx_channel_dir, "txip.scs", scs_text)

    # 2. model.sp
    model_text = _gen_model_sp(proc.lib_path, proc.lib_corner,
                               getattr(proc, 'lib_corner2', None),
                               fmt=proc.model_include_format)
    _write(tx_channel_dir, "model.sp", model_text)

    # 3. template.tcl — load = RX input cap (ESD + device gate only, no bump/pad)
    if rx_cap_in_pF is not None:
        load_pF = rx_cap_in_pF
    else:
        load_pF = lib.output_loads_pF[0]
    print(f"  [TX-channel] Using load: {load_pF:.5f} pF")

    template_text = _gen_template_tcl(
        load_pF         = load_pF,
        num_lanes       = cfg.link.lane_count,
        slew_lower_rise = lib.slew_lower_rise,
        slew_upper_rise = lib.slew_upper_rise,
        slew_lower_fall = lib.slew_lower_fall,
        slew_upper_fall = lib.slew_upper_fall,
        input_slews_ns  = lib.input_slews_ns,
        vdd             = proc.vdd,
    )
    _write(tx_channel_dir, "template.tcl", template_text)

    # 4. define_leafcell.tcl
    _write(tx_channel_dir, "define_leafcell.tcl",
           device.define_leafcell_text(lib.template_dir, "txip"))

    # 5. char.tcl
    _write(tx_channel_dir, "char.tcl", _gen_char_tcl(proc.vdd, proc.temp))

    # 6. run.sh
    _write(tx_channel_dir, "run.sh", _build_run_sh(cfg))
    os.chmod(os.path.join(tx_channel_dir, "run.sh"), 0o755)

    # 7. Run Liberate
    print(f"  [TX-channel] Running Liberate in {tx_channel_dir} ...")
    shell = "bash"
    proc_lib = subprocess.Popen(
        [shell, "run.sh"],
        cwd=tx_channel_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    for line in proc_lib.stdout:
        print(line, end="")
    proc_lib.wait()
    if proc_lib.returncode != 0:
        raise RuntimeError(
            f"[TX-channel] Liberate failed with exit code {proc_lib.returncode} "
            f"in {tx_channel_dir}"
        )
    print(f"  [TX-channel] Liberate complete.")

    # 8. Parse delay and switching power from TX-channel datasheet
    ds_path = os.path.join(tx_channel_dir, "DATASHEET", "txip.txt")
    _parse_tx_channel_results(tx_result, ds_path)

    return tx_result


def _parse_tx_channel_results(tx_result: TxNetlistResult, ds_path: str) -> None:
    """Parse TX+channel delay and switching power from Liberate datasheet."""
    if not os.path.exists(ds_path):
        warnings.warn(f"[TX-channel] Datasheet not found: {ds_path}")
        return

    with open(ds_path) as f:
        content = f.read()

    # Delay: average across all lanes for RR and FF
    for edge, attr in (("RR", "tx_channel_delay_rr_ns"), ("FF", "tx_channel_delay_ff_ns")):
        rows = re.findall(
            rf'\|\s*txip\s*\|\s*IN_\d+->PAD_\d+\({edge}\)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
            content
        )
        if rows:
            all_vals = [float(v) for r in rows for v in r]
            setattr(tx_result, attr, sum(all_vals) / len(all_vals))

    # Switching power: average across all lanes (use energy = pwr/freq from datasheet)
    for edge, attr in (("rise", "tx_channel_pwr_rise_pJ"), ("fall", "tx_channel_pwr_fall_pJ")):
        rows = re.findall(
            rf'\|\s*txip\s*\|\s*IN_\d+\s*\|\s*{edge}\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
            content
        )
        if rows:
            all_vals = [float(v) for r in rows for v in r]
            setattr(tx_result, attr, sum(all_vals) / len(all_vals))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def gen_netlist(cfg, ch_result, eq_result, run_dir: str,
               rx_cap_in_pF=None,
               tx_sizing_result=None,
               cap_in_pF_override: Optional[float] = None) -> TxNetlistResult:
    """Dispatch to the selected backend."""
    if cfg.backend == "charlib":
        return _gen_netlist_charlib(cfg, ch_result, eq_result, run_dir,
                                    rx_cap_in_pF=rx_cap_in_pF,
                                    tx_sizing_result=tx_sizing_result,
                                    cap_in_pF_override=cap_in_pF_override)
    return _gen_netlist_liberate(cfg, ch_result, eq_result, run_dir,
                                 rx_cap_in_pF=rx_cap_in_pF,
                                 tx_sizing_result=tx_sizing_result,
                                 cap_in_pF_override=cap_in_pF_override)


def _gen_netlist_charlib(cfg, ch_result, eq_result, run_dir: str,
                          rx_cap_in_pF=None,
                          tx_sizing_result=None,
                          cap_in_pF_override: Optional[float] = None) -> TxNetlistResult:
    """
    CharLib + ngspice backend: 
    1. ngspice Q/V input cap simulation
    2. Chain sizing
    3. Write netlist
    4. Write model.sp
    5. Write CharLib YAML
    6. Run CharLib
    7. Return results
    """
    tx   = cfg.transistor
    cl   = cfg.charlib
    proc = cfg.process

    tx_dir = os.path.join(run_dir, "tx")
    os.makedirs(tx_dir, exist_ok=True)

    # 1. Measure unit inverter input cap via ngspice
    if cap_in_pF_override is not None:
        cap_in_pF     = cap_in_pF_override
        cap_in_source = "override"
        print(f"  [TX/charlib] cap_in = {cap_in_pF:.5f} pF  (reused from caller)")
    else:
        print("  [TX/charlib] Measuring unit inverter input cap via ngspice...")
        cap_in_pF = _measure_inv_cap_ngspice(
            lib_path             = proc.lib_path,
            lib_corner           = proc.lib_corner,
            w_n_um               = tx.w_n_um,
            w_p_um               = tx.w_p_um,
            l_um                 = tx.l_um,
            nf                   = tx.nf,
            vdd                  = proc.vdd,
            nmos_name            = tx.nmos_name,
            pmos_name            = tx.pmos_name,
            work_dir             = tx_dir,
            ngspice_exe          = cl.ngspice_executable,
            temp                 = proc.temp,
            spec                 = DeviceSpec.from_cfg(cfg),
            model_include_format = proc.model_include_format,
        )
        if cap_in_pF is None:
            COX_fF_per_um2 = 8.6
            cap_in_pF = (tx.w_n_um + tx.w_p_um) * tx.l_um * COX_fF_per_um2 * tx.nf / 1000.0
            cap_in_source = "analytical_fallback"
            warnings.warn(
                f"[TX/charlib] ngspice cap measurement failed — using analytical fallback: "
                f"{cap_in_pF:.5f} pF"
            )
        else:
            cap_in_source = "ngspice"
            print(f"  [TX/charlib] cap_in = {cap_in_pF:.5f} pF  ({cap_in_pF*1000:.3f} fF)")

    # 2. Chain sizing.  When the channel RC ladder + RX bump/pad will be
    #    embedded in this netlist (tx_include_channel_rc=True), the driver
    #    also charges the RX chiplet parasitics beyond the channel itself —
    #    include that load in sizing, mirroring _gen_netlist_liberate.
    use_channel_rc = getattr(cl, 'tx_include_channel_rc', False)
    cap_load_pF = ch_result.total_shunt_C_fF / 1000.0
    if use_channel_rc and rx_cap_in_pF is not None:
        rx_cap_scalar = rx_cap_in_pF[0] if isinstance(rx_cap_in_pF, (list, tuple)) else rx_cap_in_pF
        cap_load_pF += rx_cap_scalar
        print(f"  [TX/charlib] Chain sizing load: {ch_result.total_shunt_C_fF:.1f} fF (channel) "
              f"+ {rx_cap_scalar*1000:.1f} fF (RX pad cap) = {cap_load_pF*1000:.1f} fF total")
    num_stages, inv_sizes = _size_inv_chain(
        w_n_min     = tx.w_n_um,
        w_p_min     = tx.w_p_um,
        cap_in_pF   = cap_in_pF,
        cap_load_pF = cap_load_pF,
    )
    if tx_sizing_result is not None:
        chosen     = tx_sizing_result.chosen
        num_stages = chosen.num_stages
        inv_sizes  = chosen.inverter_sizes
        print(f"  [TX/charlib] Using TX sizing sweep result: {num_stages} stages, "
              f"beta={chosen.beta_ratio:.2f}, stage_ratio={chosen.stage_ratio:.2f}")

    # 3. Write txip.sp. When tx_include_channel_rc is set, this is the full
    #    TX+channel+RX-bump/pad topology (matching _gen_netlist_liberate's
    #    use_channel_rc=True branch); otherwise the plain no-channel netlist.
    if use_channel_rc:
        sp_text = _gen_txip_sp_with_channel(
            lane_count          = cfg.link.lane_count,
            inv_sizes           = inv_sizes,
            use_eq              = eq_result.use_equalization,
            R_eq_ohm            = eq_result.R_eq_ohm,
            C_eq_fF             = eq_result.C_eq_fF,
            l_um                = tx.l_um,
            nmos_name           = tx.nmos_name,
            pmos_name           = tx.pmos_name,
            nf                  = tx.nf,
            ch_result           = ch_result,
            spec                = DeviceSpec.from_cfg(cfg),
            w_max_um            = tx.w_max_um,
            nf_auto             = True,
            include_rx_bump_pad = True,
        )
    else:
        sp_text = _gen_txip_sp(
            lane_count  = cfg.link.lane_count,
            inv_sizes   = inv_sizes,
            use_eq      = eq_result.use_equalization,
            R_eq_ohm    = eq_result.R_eq_ohm,
            C_eq_fF     = eq_result.C_eq_fF,
            l_um        = tx.l_um,
            nmos_name   = tx.nmos_name,
            pmos_name   = tx.pmos_name,
            nf          = tx.nf,
            spec        = DeviceSpec.from_cfg(cfg),
            w_max_um    = tx.w_max_um,
            nf_auto     = True,
        )
    _write(tx_dir, "txip.sp", sp_text)

    # 4. Write model.sp (strip Spectre-only 'simulator lang' directive)
    model_text = _gen_model_sp(proc.lib_path, proc.lib_corner,
                               getattr(proc, 'lib_corner2', None),
                               fmt=proc.model_include_format)
    model_text = re.sub(r'^\s*simulator\s+lang\s*=.*$', '', model_text,
                        flags=re.MULTILINE)
    _write(tx_dir, "model.sp", model_text)


    # 5. Write charlib.yaml. Channel-embedded mode loads with the RX device
    #    cap (single point, or the caller's full sweep — e.g. co_opt_pareto's
    #    tx_load_sweep — preserved as-is), matching _gen_netlist_liberate's
    #    use_channel_rc branch. The plain no-channel netlist keeps
    #    sweeping the full config load list to build a general-purpose
    #    Liberty NLDM table.
    if use_channel_rc:
        if rx_cap_in_pF is not None:
            if isinstance(rx_cap_in_pF, (list, tuple)):
                output_loads_for_yaml = list(rx_cap_in_pF)
                print(f"  [TX/charlib] Using RX cap sweep as load: "
                      f"{len(output_loads_for_yaml)} points "
                      f"[{output_loads_for_yaml[0]:.5f} .. {output_loads_for_yaml[-1]:.5f}] pF")
            else:
                output_loads_for_yaml = [rx_cap_in_pF]
                print(f"  [TX/charlib] Using measured RX input cap as load: {rx_cap_in_pF:.5f} pF")
        else:
            load_scalar = cl.output_loads_pF[0] if cl.output_loads_pF else 0.0
            print(f"  [TX/charlib] Using config output_loads_pF[0] as load: {load_scalar:.4f} pF")
            output_loads_for_yaml = [load_scalar]
    else:
        output_loads_for_yaml = cl.output_loads_pF
    yaml_text = _gen_charlib_yaml(
        lib_name        = "txip_nldm",
        results_dir     = "LIBRARY",
        netlist_path    = "txip.sp",
        model_path      = "model.sp",
        input_slews_ns  = cl.input_slews_ns,
        output_loads_pF = output_loads_for_yaml,
        vdd             = proc.vdd,
        temp            = proc.temp,
        lane_count      = cfg.link.lane_count,
        logic_threshold_low  = cl.logic_threshold_low,
        logic_threshold_high = cl.logic_threshold_high,
    )
    _write(tx_dir, "charlib.yaml", yaml_text)

    # 6. Run CharLib
    print(f"  [TX/charlib] Running CharLib in {tx_dir} ...")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["NGSPICE_LIBRARY_PATH"] = cl.ngspice_library_path
    proc_cl = subprocess.run(
        [cl.charlib_executable, "run", "charlib.yaml"],
        cwd=tx_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    print(proc_cl.stdout)
    if proc_cl.returncode != 0:
        raise RuntimeError(
            f"[TX/charlib] CharLib failed with exit code {proc_cl.returncode} in {tx_dir}"
        )
    print("  [TX/charlib] CharLib complete.")

    load_pF = output_loads_for_yaml[0] if output_loads_for_yaml else 0.0
    return TxNetlistResult(
        tx_dir                = tx_dir,
        num_stages            = num_stages,
        inverter_sizes        = inv_sizes,
        use_equalization      = eq_result.use_equalization,
        R_eq_ohm              = eq_result.R_eq_ohm,
        C_eq_fF               = eq_result.C_eq_fF,
        load_pF               = load_pF,
        cap_in_pF             = cap_in_pF,
        cap_in_source         = cap_in_source,
        backend               = "charlib",
        channel_rc_integrated = use_channel_rc,
    )


def _gen_netlist_liberate(cfg, ch_result, eq_result, run_dir: str,
               rx_cap_in_pF=None,
               tx_sizing_result=None,
               cap_in_pF_override: Optional[float] = None) -> TxNetlistResult:
    """
    Generate TX netlist, write all Liberate scripts, and run characterization.

    Stages
    ------
    1. SPICE Q/V sim   → cap_in_pF  (falls back to Cox estimate on failure)
    2. Chain sizing    → num_stages, inverter_sizes
    3–8. Write files   → txip.scs, model.sp, template.tcl,
                         define_leafcell.tcl, char.tcl, run.sh
    9. Run Liberate    → LIBRARY/txip_nldm.lib, DATASHEET/txip

    Parameters
    ----------
    cfg            : Config from main.py (uses process, transistor, liberate sections)
    ch_result      : ChannelResult from channel.py
    eq_result      : EqualizationResult from equalization.py
    run_dir        : Top-level combo directory
    rx_cap_in_pF   : Measured RX pre-amp input capacitance (pF).  When
                     provided and tx_include_channel_rc is True, this value
                     is used as the Liberate output load instead of the
                     fixed config value output_loads_pF[0].
    tx_sizing_result : TxSizingResult from tx_sizing.py.  When provided, the
                       chosen inverter sizes override the default Logical
                       Effort sizing and are written into the txip.scs netlist.

    Returns
    -------
    TxNetlistResult

    Raises
    ------
    RuntimeError if Liberate exits with a non-zero return code.
    """
    tx = cfg.transistor
    lib = cfg.liberate
    proc = cfg.process

    tx_dir = os.path.join(run_dir, "tx")
    os.makedirs(tx_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Measure unit inverter input capacitance via SPICE (Q/V method).
    #    Skipped when cap_in_pF_override is provided (e.g., reuse the measured
    #    value in co-opt to avoid re-running the SPICE measurement per candidate).
    # ------------------------------------------------------------------
    if cap_in_pF_override is not None:
        cap_in_pF  = cap_in_pF_override
        cap_in_source = "override"
        print(f"  [TX] cap_in = {cap_in_pF:.5f} pF  (reused from caller)")
    else:
        print("  [TX] Measuring unit inverter input cap via SPICE...")
        cap_in_pF = _measure_inv_cap_spice(
            lib_path        = proc.lib_path,
            lib_corner      = proc.lib_corner,
            w_n_um          = tx.w_n_um,
            w_p_um          = tx.w_p_um,
            l_um            = tx.l_um,
            nf              = tx.nf,
            vdd             = proc.vdd,
            nmos_name       = tx.nmos_name,
            pmos_name       = tx.pmos_name,
            work_dir        = tx_dir,
            temp            = proc.temp,
            spec            = DeviceSpec.from_cfg(cfg),
            model_include_format = proc.model_include_format,
        )

        if cap_in_pF is None:
            # Fallback: Cox geometric estimate (65nm-class Cox ≈ 8.6 fF/µm²)
            COX_fF_per_um2 = 8.6
            cap_in_pF = (tx.w_n_um + tx.w_p_um) * tx.l_um * COX_fF_per_um2 * tx.nf / 1000.0
            cap_in_source = "analytical_fallback"
            warnings.warn(
                f"[TX] SPICE cap measurement failed — using analytical fallback: "
                f"{cap_in_pF:.5f} pF"
            )
        else:
            cap_in_source = "spice"
            print(f"  [TX] cap_in = {cap_in_pF:.5f} pF  ({cap_in_pF*1000:.3f} fF)")

    # ------------------------------------------------------------------
    # 2. Inverter chain sizing (Logical Effort)
    # ------------------------------------------------------------------
    use_channel_rc = getattr(cfg.liberate, 'tx_include_channel_rc', False)

    # total_shunt_C_fF covers the TX-side channel network (TX pad, ESD, bump,
    # TX ipos pad, trace, RX ipos pad).  When channel_rc is embedded, the TX
    # driver also charges the RX chiplet parasitics (bump + pad + ESD) which
    # appear as the Liberate output load (rx_cap_in_pF = measured rxip PAD cap).
    # Include that load in chain sizing so the inverter is sized for the true
    # total capacitance it must drive.
    cap_load_pF = ch_result.total_shunt_C_fF / 1000.0
    if use_channel_rc and rx_cap_in_pF is not None:
        rx_cap_scalar = rx_cap_in_pF[0] if isinstance(rx_cap_in_pF, (list, tuple)) else rx_cap_in_pF
        cap_load_pF += rx_cap_scalar
        print(f"  [TX] Chain sizing load: {ch_result.total_shunt_C_fF:.1f} fF (channel) "
              f"+ {rx_cap_scalar*1000:.1f} fF (RX pad cap) = {cap_load_pF*1000:.1f} fF total")

    num_stages, inv_sizes = _size_inv_chain(
        w_n_min     = tx.w_n_um,
        w_p_min     = tx.w_p_um,
        cap_in_pF   = cap_in_pF,
        cap_load_pF = cap_load_pF,
    )

    # Override with TX sizing sweep result when available
    inv_sizes_override = None
    if tx_sizing_result is not None:
        chosen = tx_sizing_result.chosen
        num_stages = chosen.num_stages
        inv_sizes  = chosen.inverter_sizes
        inv_sizes_override = chosen.inverter_sizes
        print(f"  [TX] Using TX sizing sweep result: {num_stages} stages, "
              f"beta={chosen.beta_ratio:.2f}, stage_ratio={chosen.stage_ratio:.2f}")

    # ------------------------------------------------------------------
    # 3. Generate txip.scs from template + N-lane wrapper
    # ------------------------------------------------------------------
    lane_count       = cfg.link.lane_count
    template_scs_dir = os.path.join(lib.template_dir, "txip")
    template_scs_path = os.path.join(template_scs_dir, "txip.scs")
    cc_enabled, cc_ratio_trace, cc_ratio_pad = _coupling_cap_from_cfg(cfg)
    tx_sp = _tx_signal_pairs_from_cfg(cfg)
    scs_text = _gen_txip_scs(
        template_scs_path = template_scs_path,
        lane_count        = lane_count,
        use_eq            = eq_result.use_equalization,
        R_eq_ohm          = eq_result.R_eq_ohm,
        C_eq_fF           = eq_result.C_eq_fF,
        use_channel_rc    = use_channel_rc,
        ch_result         = ch_result,
        l_um              = tx.l_um,
        spec              = DeviceSpec.from_cfg(cfg),
        inv_sizes_override = inv_sizes_override,
        w_max_um           = tx.w_max_um,
        use_coupling_cap   = cc_enabled,
        cc_ratio_trace     = cc_ratio_trace,
        cc_ratio_pad       = cc_ratio_pad,
        signal_pairs       = tx_sp,
    )
    _write(tx_dir, "txip.scs", scs_text)

    # ------------------------------------------------------------------
    # 4. model.sp
    # ------------------------------------------------------------------
    model_text = _gen_model_sp(proc.lib_path, proc.lib_corner,
                               getattr(proc, 'lib_corner2', None),
                               fmt=proc.model_include_format)
    _write(tx_dir, "model.sp", model_text)

    # ------------------------------------------------------------------
    # 5. template.tcl  (index_2 = RX device cap when channel RC is embedded
    #                           = full channel cap otherwise)
    # ------------------------------------------------------------------
    # When the channel RC ladder is embedded in the netlist (channel_rc=1),
    # all channel shunt capacitance is already inside the subcircuit.  Only
    # the far-end RX device input cap should be applied externally by Liberate
    # (index_2 = measured RX pre-amp cap, or config fallback).
    # When channel_rc=0 the full channel shunt capacitance is the output load.
    if use_channel_rc:
        if rx_cap_in_pF is not None:
            load_pF = rx_cap_in_pF
            if isinstance(load_pF, (list, tuple)):
                print(f"  [TX] Using RX cap sweep as load: "
                      f"{len(load_pF)} points [{load_pF[0]:.5f} .. {load_pF[-1]:.5f}] pF")
            else:
                print(f"  [TX] Using measured RX input cap as load: {load_pF:.5f} pF")
        else:
            load_pF = lib.output_loads_pF[0]
            print(f"  [TX] Using config output_loads_pF[0] as load: {load_pF:.4f} pF")
    else:
        load_pF = ch_result.total_shunt_C_fF / 1000.0  # full channel cap
    template_text = _gen_template_tcl(
        load_pF         = load_pF,
        num_lanes       = lane_count,
        slew_lower_rise = lib.slew_lower_rise,
        slew_upper_rise = lib.slew_upper_rise,
        slew_lower_fall = lib.slew_lower_fall,
        slew_upper_fall = lib.slew_upper_fall,
        input_slews_ns  = lib.input_slews_ns,
        vdd             = proc.vdd,
    )
    _write(tx_dir, "template.tcl", template_text)

    # ------------------------------------------------------------------
    # 6. define_leafcell.tcl
    # ------------------------------------------------------------------
    _write(tx_dir, "define_leafcell.tcl",
           device.define_leafcell_text(lib.template_dir, "txip"))

    # ------------------------------------------------------------------
    # 7. char.tcl  (VDD and temperature taken from process config)
    # ------------------------------------------------------------------
    _write(tx_dir, "char.tcl", _gen_char_tcl(proc.vdd, proc.temp))

    # ------------------------------------------------------------------
    # 8. run.sh
    # ------------------------------------------------------------------
    _write(tx_dir, "run.sh", _build_run_sh(cfg))
    os.chmod(os.path.join(tx_dir, "run.sh"), 0o755)

    # ------------------------------------------------------------------
    # 9. Run Liberate characterization
    # ------------------------------------------------------------------
    print(f"  [TX] Running Liberate in {tx_dir} ...")
    shell = "bash"
    proc_lib = subprocess.Popen(
        [shell, "run.sh"],
        cwd=tx_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    for line in proc_lib.stdout:
        print(line, end="")
    proc_lib.wait()
    if proc_lib.returncode != 0:
        raise RuntimeError(
            f"[TX] Liberate failed with exit code {proc_lib.returncode} in {tx_dir}"
        )
    print(f"  [TX] Liberate complete.")

    return TxNetlistResult(
        tx_dir                = tx_dir,
        num_stages            = num_stages,
        inverter_sizes        = inv_sizes,
        use_equalization      = eq_result.use_equalization,
        R_eq_ohm              = eq_result.R_eq_ohm,
        C_eq_fF               = eq_result.C_eq_fF,
        load_pF               = load_pF,
        cap_in_pF             = cap_in_pF,
        cap_in_source         = cap_in_source,
        channel_rc_integrated = use_channel_rc,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(directory: str, filename: str, content: str) -> None:
    path = os.path.join(directory, filename)
    with open(path, "w") as f:
        f.write(content)