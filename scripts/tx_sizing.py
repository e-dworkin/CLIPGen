"""
tx_sizing.py — TX inverter-chain sizing via adaptive search.

Finds the inverter chain (num_stages, beta_ratio, stage_ratio) that meets the
rise/fall target with minimum power, using a small adaptive SPICE search.

Public API:
    sweep_tx_sizing(cfg, ch_result, cap_in_pF, run_dir) -> TxSizingResult or None
"""

import math
import os
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

import device
from device import DeviceSpec

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MAX_SIMS_DEFAULT = 20
_GOLDEN = (math.sqrt(5) - 1) / 2          # ≈ 0.618
_COX_FF_PER_UM2 = 8.6                     # approximate Cox for a 65nm-class node
_F_PARASITIC = 3.59                        # optimal fan-out with parasitics

# Bounds for continuous parameters
_F_LO, _F_HI = 1.5, 8.0                   # stage ratio bounds
_BETA_LO, _BETA_HI = 1.0, 4.0             # PMOS/NMOS width ratio bounds
_N_MIN, _N_MAX = 2, 12                     # stage count bounds


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TxSizingCandidate:
    num_stages:    int
    beta_ratio:    float       # w_p / w_n ratio
    stage_ratio:   float       # C_{i+1}/C_i  (geometric scaling factor)
    inverter_sizes: List[Tuple[float, float]]  # [(w_n, w_p), ...]
    rise_time_ns:  float = 0.0
    fall_time_ns:  float = 0.0
    avg_power_mW:  float = 0.0
    sim_success:   bool  = False


@dataclass
class TxSizingResult:
    chosen:            TxSizingCandidate
    all_candidates:    List[TxSizingCandidate]
    target_rise_ns:    float
    target_fall_ns:    float
    target_met:        bool
    message:           str

    def report(self) -> str:
        c = self.chosen
        lines = [
            "=== TX Sizing (Adaptive Search) ===",
            f"  Target rise/fall : <= {self.target_rise_ns:.4f} ns  "
            f"({self.target_rise_ns * 1000:.2f} ps)",
            f"  Candidates tried : {len(self.all_candidates)}",
            f"  Target met       : {'YES' if self.target_met else 'NO — using fastest candidate'}",
            f"  Chosen config    :",
            f"    Stages         : {c.num_stages}",
            f"    Beta ratio     : {c.beta_ratio:.2f}",
            f"    Stage ratio    : {c.stage_ratio:.2f}",
            f"    Rise time      : {c.rise_time_ns:.4f} ns  ({c.rise_time_ns*1000:.2f} ps)",
            f"    Fall time      : {c.fall_time_ns:.4f} ns  ({c.fall_time_ns*1000:.2f} ps)",
            f"    Avg power      : {c.avg_power_mW:.4f} mW",
        ]
        if c.inverter_sizes:
            for i, (wn, wp) in enumerate(c.inverter_sizes):
                lines.append(f"    Stage {i+1}: NMOS={wn:.4f}u  PMOS={wp:.4f}u")
        if not self.target_met:
            lines.append(f"  WARNING: {self.message}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Objective function for ranking candidates
# ---------------------------------------------------------------------------
def _objective(cand: Optional[TxSizingCandidate],
               target_ns: float) -> Tuple[int, float]:
    """Scalar objective for candidate comparison (lower is better).

    Returns a (rank, value) tuple so that:
      rank 0 — feasible candidates, ordered by ascending power
      rank 1 — infeasible candidates, ordered by ascending max(tr, tf)
      rank 2 — failed simulations (always worst)
    """
    if cand is None or not cand.sim_success:
        return (2, float('inf'))
    max_rf = max(cand.rise_time_ns, cand.fall_time_ns)
    if max_rf <= target_ns:
        return (0, cand.avg_power_mW)
    return (1, max_rf)


# ---------------------------------------------------------------------------
# Analytical warm-start from logical effort
# ---------------------------------------------------------------------------
def _analytical_init(w_n: float, w_p: float, l: float,
                     C_load_pF: float) -> Tuple[int, float, float]:
    """Return (N_init, f_init, beta_init) from logical-effort theory."""
    C_in_fF = (w_n + w_p) * l * _COX_FF_PER_UM2
    C_in_pF = max(C_in_fF / 1000.0, 1e-6)

    FO = max(C_load_pF / C_in_pF, 1.0)
    N_real = math.log(FO) / math.log(_F_PARASITIC)
    N = max(_N_MIN, round(N_real))
    # Force even (non-inverting polarity)
    if N % 2 != 0:
        N = N + 1 if N_real > N else max(_N_MIN, N - 1)
    if N % 2 != 0:          # safety: ensure even
        N += 1

    f = FO ** (1.0 / N) if N > 0 else _F_PARASITIC
    beta = w_p / w_n if w_n > 0 else 2.0
    return N, f, beta


# ---------------------------------------------------------------------------
# Inverter sizing helper
# ---------------------------------------------------------------------------
def _compute_inverter_sizes(
    w_n_min:     float,
    w_p_min:     float,
    num_stages:  int,
    beta_ratio:  float,
    stage_ratio: float,
) -> List[Tuple[float, float]]:
    """Compute inverter sizes for given parameters.

    Stage 0 uses (w_n_min, w_n_min * beta_ratio).
    Each subsequent stage scales by stage_ratio.
    """
    sizes = []
    for i in range(num_stages):
        w_n = w_n_min * (stage_ratio ** i)
        w_p = w_n * beta_ratio
        sizes.append((round(w_n, 4), round(w_p, 4)))
    return sizes


# ---------------------------------------------------------------------------
# SPICE netlist generation & simulation (unchanged infrastructure)
# ---------------------------------------------------------------------------
# FreePDK45: SPICE-lang HSPICE BSIM4 PTM models + 4-terminal wrapper subckts.
# Mirrors tx._freepdk45_model_sp so a standalone sizing sweep loads identically.
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


def _gen_sizing_spice(
    candidate:     TxSizingCandidate,
    lib_path:      str,
    lib_corner:    str,
    nmos_name:     str,
    pmos_name:     str,
    l_um:          float,
    nf:            int,
    vdd:           float,
    temp:          float,
    load_cap_pF:   float,
    spec:          Optional[DeviceSpec] = None,
    model_include_format: str = "spice_lib",
) -> str:
    """Generate a SPICE netlist for transient simulation of one candidate."""
    if spec is None:
        spec = DeviceSpec()

    # Model include — chosen by the config-declared format (no PDK strings here).
    if model_include_format == "hspice_ptm":
        model_header = _freepdk45_model_sp(lib_path, lib_corner).rstrip()
    elif model_include_format == "spectre_include":
        model_dir = os.path.dirname(lib_path)
        all_models = os.path.join(model_dir, "allModels.scs")
        model_header = (
            f'simulator lang=spectre\n'
            f'include "{lib_path}"\n'
            f'include "{all_models}"\n'
            f'simulator lang=spice'
        )
    else:
        model_header = (
            f'simulator lang=spice\n'
            f'.lib "{lib_path}" {lib_corner}'
        )

    # Build inverter chain instances
    inv_lines = []
    for i, (w_n, w_p) in enumerate(candidate.inverter_sizes):
        if i == 0:
            in_node = "in"
        else:
            in_node = f"n{i-1}"
        out_node = f"n{i}" if i < candidate.num_stages - 1 else "out"

        if spec.is_finfet:
            l_nm = spec.l_nm()
            nfin_n = spec.w_to_nfin(w_n)
            nfin_p = spec.w_to_nfin(w_p)
            inv_lines.append(
                f'xnm{i} {out_node} {in_node} 0 0 {nmos_name} L={l_nm}n nfin={nfin_n}'
            )
            inv_lines.append(
                f'xpm{i} {out_node} {in_node} VDD VDD {pmos_name} L={l_nm}n nfin={nfin_p}'
            )
        else:
            # x-prefix wrapper subckts (drain gate source bulk), pass w/l (+nf).
            nf_tok = f' nf={nf}' if spec.include_nf else ''
            inv_lines.append(
                f'xnm{i} {out_node} {in_node} 0 0 {nmos_name} w={w_n}u l={l_um}u{nf_tok}'
            )
            inv_lines.append(
                f'xpm{i} {out_node} {in_node} VDD VDD {pmos_name} w={w_p}u l={l_um}u{nf_tok}'
            )

    inv_block = "\n".join(inv_lines)

    sim_time = "20n"
    period = "10n"
    rise_fall = "50p"

    netlist = f"""\
// TX Sizing Transient Simulation
// Stages={candidate.num_stages} Beta={candidate.beta_ratio} StageRatio={candidate.stage_ratio}
{model_header}

.option post=1
.temp {temp}

VDD VDD 0 DC {vdd}
VSS 0 0 0

// Input: fast-edge pulse
VIN in 0 PULSE(0 {vdd} 1n {rise_fall} {rise_fall} 4.95n {period})

// Inverter chain
{inv_block}

// Output load (channel capacitance)
CL out 0 {load_cap_pF}p

// Transient analysis
.tran 1p {sim_time}

// Measure rise time (20% to 80% of VDD)
.measure tran t_rise trig v(out) val='{vdd}*0.2' rise=1 targ v(out) val='{vdd}*0.8' rise=1
.measure tran t_fall trig v(out) val='{vdd}*0.8' fall=1 targ v(out) val='{vdd}*0.2' fall=1

// Measure average supply current for power
.measure tran i_avg avg i(VDD) from=1n to={sim_time}
.measure tran p_avg param='abs(i_avg)*{vdd}'

.end
"""
    return netlist


def _run_sizing_sim(
    candidate:     TxSizingCandidate,
    work_dir:      str,
    cfg,
    load_cap_pF:   float,
) -> None:
    """Run SPICE transient for one candidate and populate its results."""
    cand_dir = os.path.join(
        work_dir,
        f"s{candidate.num_stages}_b{candidate.beta_ratio:.2f}_f{candidate.stage_ratio:.2f}"
    )
    os.makedirs(cand_dir, exist_ok=True)

    netlist_text = _gen_sizing_spice(
        candidate   = candidate,
        lib_path    = cfg.process.lib_path,
        lib_corner  = cfg.process.lib_corner,
        nmos_name   = cfg.transistor.nmos_name,
        pmos_name   = cfg.transistor.pmos_name,
        l_um        = cfg.transistor.l_um,
        nf          = cfg.transistor.nf,
        vdd         = cfg.process.vdd,
        temp        = cfg.process.temp,
        load_cap_pF = load_cap_pF,
        spec        = DeviceSpec.from_cfg(cfg),
        model_include_format = cfg.process.model_include_format,
    )

    netlist_path = os.path.join(cand_dir, "sizing_sim.scs")
    with open(netlist_path, "w") as f:
        f.write(netlist_text)

    spectre_cmd = "spectre -64 sizing_sim.scs -format psfascii"
    run_sh_path = os.path.join(cand_dir, "run.sh")
    with open(run_sh_path, "w") as f:
        f.write(f"#!/bin/bash\n{spectre_cmd}\n")
    os.chmod(run_sh_path, 0o755)

    orig_dir = os.getcwd()
    try:
        os.chdir(cand_dir)
        proc = subprocess.run(
            ["bash", "run.sh"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120,
        )
        if proc.returncode != 0:
            return

        measure_file = "sizing_sim.measure"
        if not os.path.exists(measure_file):
            return

        with open(measure_file, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("t_rise") and "=" in line:
                    val = line.split("=")[1].strip()
                    if val.lower() != "failed":
                        candidate.rise_time_ns = float(val) * 1e9
                elif line.startswith("t_fall") and "=" in line:
                    val = line.split("=")[1].strip()
                    if val.lower() != "failed":
                        candidate.fall_time_ns = float(val) * 1e9
                elif line.startswith("p_avg") and "=" in line:
                    val = line.split("=")[1].strip()
                    if val.lower() != "failed":
                        candidate.avg_power_mW = float(val) * 1e3

        if candidate.rise_time_ns > 0 and candidate.fall_time_ns > 0:
            candidate.sim_success = True

    except subprocess.TimeoutExpired:
        pass
    finally:
        os.chdir(orig_dir)


# ---------------------------------------------------------------------------
# Public API — adaptive search
# ---------------------------------------------------------------------------
def sweep_tx_sizing(cfg, ch_result, cap_in_pF: float,
                    run_dir: str) -> Optional[TxSizingResult]:
    """
    Adaptively search for a TX inverter chain sizing that meets the
    rise/fall time target with minimum power.

    The search proceeds in five phases (see module docstring) and is
    bounded by ``max_iterations`` SPICE simulations.

    Parameters
    ----------
    cfg : Config
        Full configuration (must have tx_sizing section with *enabled*
        and *rise_fall_pct_ui*).
    ch_result : ChannelResult
        Channel results for load capacitance.
    cap_in_pF : float
        Measured unit inverter input capacitance (informational; the
        algorithm recomputes C_in from transistor geometry).
    run_dir : str
        Top-level run directory.

    Returns
    -------
    TxSizingResult or None if tx_sizing is not enabled.
    """
    sizing_cfg = getattr(cfg, 'tx_sizing', None)
    if sizing_cfg is None or not sizing_cfg.enabled:
        return None

    tx = cfg.transistor
    data_rate_Hz = cfg.link.data_rate_Gbps * 1e9
    ui_ns = 1.0 / data_rate_Hz * 1e9
    target_ns = sizing_cfg.rise_fall_pct_ui * ui_ns
    load_cap_pF = ch_result.total_shunt_C_fF / 1000.0

    max_sims = getattr(sizing_cfg, 'max_iterations', _MAX_SIMS_DEFAULT)

    print(f"  [TX Sizing] UI = {ui_ns:.4f} ns, "
          f"target rise/fall = {target_ns:.4f} ns "
          f"({sizing_cfg.rise_fall_pct_ui*100:.0f}% of UI)")

    work_dir = os.path.join(run_dir, "tx", "sizing_sweep")
    os.makedirs(work_dir, exist_ok=True)

    # ---- bookkeeping ----
    all_cands: List[TxSizingCandidate] = []
    sims_left = max_sims

    def _eval(N: int, f: float, beta: float) -> Optional[TxSizingCandidate]:
        """Create, simulate, and record one candidate.  Returns None if
        the simulation budget is exhausted."""
        nonlocal sims_left
        if sims_left <= 0:
            return None
        sims_left -= 1
        sizes = _compute_inverter_sizes(tx.w_n_um, tx.w_p_um, N, beta, f)
        cand = TxSizingCandidate(
            num_stages=N, beta_ratio=beta, stage_ratio=f,
            inverter_sizes=sizes,
        )
        _run_sizing_sim(cand, work_dir, cfg, load_cap_pF)
        all_cands.append(cand)
        tag = "OK" if cand.sim_success else "FAIL"
        idx = len(all_cands)
        if cand.sim_success:
            print(f"    [{idx}/{max_sims}] N={N} β={beta:.2f} f={f:.2f} "
                  f"→ tr={cand.rise_time_ns:.4f}ns "
                  f"tf={cand.fall_time_ns:.4f}ns "
                  f"P={cand.avg_power_mW:.4f}mW  [{tag}]")
        else:
            print(f"    [{idx}/{max_sims}] N={N} β={beta:.2f} f={f:.2f} "
                  f"→ [{tag}]")
        return cand

    def _best() -> TxSizingCandidate:
        """Return the best evaluated candidate so far."""
        return min(all_cands, key=lambda c: _objective(c, target_ns))

    # ==================================================================
    # Phase 1 — Analytical warm-start
    # ==================================================================
    N_init, f_init, beta_init = _analytical_init(
        tx.w_n_um, tx.w_p_um, tx.l_um, load_cap_pF)
    print(f"  [TX Sizing] Analytical init: N={N_init}, "
          f"f={f_init:.3f}, β={beta_init:.3f}")

    # ==================================================================
    # Phase 2 — Stage count (N) exploration              [~3 sims]
    # ==================================================================
    print("  [TX Sizing] Phase 2: stage-count exploration")
    N_candidates = sorted({N for N in
                           [max(_N_MIN, N_init - 2), N_init, N_init + 2]
                           if _N_MIN <= N <= _N_MAX and N % 2 == 0})
    for N in N_candidates:
        _eval(N, f_init, beta_init)

    cur = _best()
    N_best = cur.num_stages

    # ==================================================================
    # Phase 3 — Stage ratio (f) refinement via golden-section  [~5 sims]
    # ==================================================================
    print("  [TX Sizing] Phase 3: stage-ratio refinement (golden section)")
    f_lo = max(_F_LO, f_init * 0.4)
    f_hi = min(_F_HI, f_init * 2.5)
    beta_cur = cur.beta_ratio

    # Initial interior points
    fa = f_hi - _GOLDEN * (f_hi - f_lo)
    fb = f_lo + _GOLDEN * (f_hi - f_lo)
    ca = _eval(N_best, fa, beta_cur)
    cb = _eval(N_best, fb, beta_cur)

    for _ in range(3):                       # 3 more iterations
        if sims_left <= 5:                   # reserve budget for later phases
            break
        oa = _objective(ca, target_ns)
        ob = _objective(cb, target_ns)
        if oa <= ob:                         # left side is better → shrink right
            f_hi = fb
            fb, cb = fa, ca
            fa = f_hi - _GOLDEN * (f_hi - f_lo)
            ca = _eval(N_best, fa, beta_cur)
        else:                                # right side is better → shrink left
            f_lo = fa
            fa, ca = fb, cb
            fb = f_lo + _GOLDEN * (f_hi - f_lo)
            cb = _eval(N_best, fb, beta_cur)
        if ca is None or cb is None:
            break

    cur = _best()
    f_best = cur.stage_ratio

    # ==================================================================
    # Phase 4 — Beta ratio (β) refinement via golden-section  [~5 sims]
    # ==================================================================
    print("  [TX Sizing] Phase 4: beta-ratio refinement (golden section)")
    b_lo, b_hi = _BETA_LO, _BETA_HI

    ba = b_hi - _GOLDEN * (b_hi - b_lo)
    bb = b_lo + _GOLDEN * (b_hi - b_lo)
    ca = _eval(cur.num_stages, f_best, ba)
    cb = _eval(cur.num_stages, f_best, bb)

    for _ in range(3):
        if sims_left <= 2:
            break
        oa = _objective(ca, target_ns)
        ob = _objective(cb, target_ns)
        if oa <= ob:
            b_hi = bb
            bb, cb = ba, ca
            ba = b_hi - _GOLDEN * (b_hi - b_lo)
            ca = _eval(cur.num_stages, f_best, ba)
        else:
            b_lo = ba
            ba, ca = bb, cb
            bb = b_lo + _GOLDEN * (b_hi - b_lo)
            cb = _eval(cur.num_stages, f_best, bb)
        if ca is None or cb is None:
            break

    cur = _best()

    # ==================================================================
    # Phase 5 — Cross-validate N with refined (f, β)     [~2 sims]
    # ==================================================================
    print("  [TX Sizing] Phase 5: cross-validating stage count")
    for N in N_candidates:
        if N != cur.num_stages and sims_left > 0:
            _eval(N, cur.stage_ratio, cur.beta_ratio)

    # ==================================================================
    # Select final result
    # ==================================================================
    valid = [c for c in all_cands if c.sim_success]
    if not valid:
        print("  [TX Sizing] WARNING: No simulations succeeded. "
              "Using default sizing.")
        return None

    max_rise_fall = lambda c: max(c.rise_time_ns, c.fall_time_ns)
    meeting = [c for c in valid if max_rise_fall(c) <= target_ns]

    if meeting:
        chosen = min(meeting, key=lambda c: c.avg_power_mW)
        target_met = True
        message = "Target met."
    else:
        chosen = min(valid, key=max_rise_fall)
        target_met = False
        fastest_rf = max_rise_fall(chosen)
        message = (
            f"No candidate meets the {target_ns:.4f} ns target. "
            f"Using fastest: {fastest_rf:.4f} ns "
            f"(N={chosen.num_stages} β={chosen.beta_ratio:.2f} "
            f"f={chosen.stage_ratio:.2f})."
        )
        print(f"  [TX Sizing] WARNING: {message}")

    print(f"  [TX Sizing] Done — {len(all_cands)} sims total.")
    return TxSizingResult(
        chosen         = chosen,
        all_candidates = all_cands,
        target_rise_ns = target_ns,
        target_fall_ns = target_ns,
        target_met     = target_met,
        message        = message,
    )
