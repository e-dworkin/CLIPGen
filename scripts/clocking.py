"""
clocking.py — UCIe forwarded-clock power-overhead model.

Computes the energy overhead of the forwarded clock and on-die phase
generation, normalized per data bit per data lane.

Public API (called by main.py):
    get_clocking(cfg, E_lane_R_pJ, link_energy_pJ=...)
"""

import os
import re
import math
import argparse
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from device import DeviceSpec


# ---------------------------------------------------------------------------
# Default circuit constants  (used as ClockingHiddenConfig defaults)
# ---------------------------------------------------------------------------
# MEASURED — transient Spectre, temp/ddr_overhead/ (FreePDK45, VDD=1.1 V, 8 GHz):
_SERIALIZER_FJ_PER_STAGE = 2.44     # one 2:1 serializer stage, alpha=0.5 (real txip load)
_DCC_POWER_UW            = 182.0    # duty-cycle corrector, per clock domain (~49% lock)
# ESTIMATED by analogy to the measured DCC (a duty/phase circuit of similar
# class) — NOT directly measured.  Override per PDK with characterized values.
_DESERIALIZER_FJ_PER_STAGE = 2.44   # assume symmetric to TX serializer
_DLL_POWER_UW              = 182.0  # RX phase generator, per clock domain (~DCC class)
_PI_POWER_UW_PER_LANE      = 182.0  # per-lane phase interpolator (deskew), ~DCC class

_FCK_CAP_GHZ              = 8.0     # UCIe forwarded-clock frequency cap
_DESKEW_REQUIRED_MIN_GTS = 12.0    # per-lane deskew Required at/above this rate


# A lane-energy sample with provenance.  provenance: 'reused' | 'proxied'.
LaneEnergy = namedtuple("LaneEnergy", ["pJ_per_bit", "provenance"])
LaneEnergyProvider = Callable[[float], LaneEnergy]


# ---------------------------------------------------------------------------
# Config dataclasses (parsed by main.load_config via the load_* helpers below)
# ---------------------------------------------------------------------------
@dataclass
class ClockingConfig:
    """User-facing clocking knobs (no hidden parameters)."""
    enabled:      bool = True
    n_clocks:     int  = 1                 # clocks in the IP (UCIe: 1)
    ratio:        Optional[object] = None  # 'sdr'/'ddr'/'qdr' or M=1/2/4; None/'ucie' = rule
    deskew:       Optional[object] = None  # True/False, or None/'ucie' = rule (>=12 GT/s)


@dataclass
class ClockingHiddenConfig:
    """Clock-circuit energy constants (serializer/DCC measured; rest estimated)."""
    serializer_fj_per_stage:   float = _SERIALIZER_FJ_PER_STAGE
    deserializer_fj_per_stage: float = _DESERIALIZER_FJ_PER_STAGE
    dcc_power_uW:              float = _DCC_POWER_UW
    dll_power_uW:              float = _DLL_POWER_UW
    pi_power_uW_per_lane:      float = _PI_POWER_UW_PER_LANE
    fck_cap_GHz:              float = _FCK_CAP_GHZ
    deskew_required_min_GTs:  float = _DESKEW_REQUIRED_MIN_GTS


# Scheme name <-> data:clock ratio M.  'ucie' (alias 'auto') = pick from rate.
_SCHEME_TO_M = {"sdr": 1, "ddr": 2, "qdr": 4}
_M_TO_SCHEME = {1: "sdr", 2: "ddr", 4: "qdr"}
_UCIE_SENTINELS = ("ucie", "auto")


def parse_ratio(v):
    """Parse a 'ratio' value -> M (1/2/4) or None (UCIe rule from data rate).

    Accepts 'sdr'/'ddr'/'qdr', 'ucie' (or 'auto'), or ints 1/2/4.
    """
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _UCIE_SENTINELS:
            return None
        if s in _SCHEME_TO_M:
            return _SCHEME_TO_M[s]
        raise ValueError(f"clocking.ratio must be sdr/ddr/qdr or ucie, got {v!r}")
    M = int(v)
    if M not in (1, 2, 4):
        raise ValueError(f"clocking.ratio must be 1/2/4 or sdr/ddr/qdr/ucie, got {v!r}")
    return M


def parse_deskew(v):
    """Parse a 'deskew' value -> True/False or None (UCIe rule)."""
    if v is None:
        return None
    if isinstance(v, str) and v.strip().lower() in _UCIE_SENTINELS:
        return None
    return bool(v)


def load_clocking_config(raw: dict) -> ClockingConfig:
    """Build a ClockingConfig from the raw 'clocking' JSON section (or {})."""
    raw = raw or {}
    return ClockingConfig(
        enabled      = bool(raw.get("enabled", True)),
        n_clocks     = int(raw.get("n_clocks", 1)),
        ratio        = parse_ratio(raw.get("ratio", "ucie")),
        deskew       = parse_deskew(raw.get("deskew", "ucie")),
    )


def load_clocking_hidden(raw: dict) -> ClockingHiddenConfig:
    """Build a ClockingHiddenConfig from the raw 'clocking_hidden' JSON section (or {})."""
    raw = raw or {}
    d = ClockingHiddenConfig()
    return ClockingHiddenConfig(
        serializer_fj_per_stage   = float(raw.get("serializer_fj_per_stage",   d.serializer_fj_per_stage)),
        deserializer_fj_per_stage = float(raw.get("deserializer_fj_per_stage", d.deserializer_fj_per_stage)),
        dcc_power_uW              = float(raw.get("dcc_power_uW",              d.dcc_power_uW)),
        dll_power_uW              = float(raw.get("dll_power_uW",             d.dll_power_uW)),
        pi_power_uW_per_lane      = float(raw.get("pi_power_uW_per_lane",     d.pi_power_uW_per_lane)),
        fck_cap_GHz              = float(raw.get("fck_cap_GHz",             d.fck_cap_GHz)),
        deskew_required_min_GTs  = float(raw.get("deskew_required_min_GTs", d.deskew_required_min_GTs)),
    )


# ---------------------------------------------------------------------------
# UCIe spec lookup: data_rate -> clocking modes  (configs/bump_maps + spec table)
# ---------------------------------------------------------------------------
# Each rate maps to a list of (fCK_GHz, M, deskew_required) options.  Rates whose
# half-rate clock would exceed the fCK cap (24, 32) also get a quarter-rate option.
UCIE_MODES = {
    32: [(16.0, 2, True),  (8.0, 4, True)],
    24: [(12.0, 2, True),  (6.0, 4, True)],
    16: [(8.0,  2, True)],
    12: [(6.0,  2, True)],
    8:  [(4.0,  2, False)],
    4:  [(2.0,  2, False)],
}


def ucie_modes(data_rate_GTs: float,
               fck_cap_GHz: float = _FCK_CAP_GHZ,
               deskew_min_GTs: float = _DESKEW_REQUIRED_MIN_GTS
               ) -> List[Tuple[float, int, bool]]:
    """UCIe (fCK_GHz, M, deskew_required) options for a data rate.

    Off-table rates derive half-rate (M=2), plus quarter-rate (M=4) if half-rate
    fCK exceeds the cap; deskew_required follows the >= deskew_min rule.
    """
    key = int(round(data_rate_GTs))
    if key in UCIE_MODES:
        return UCIE_MODES[key]
    deskew = data_rate_GTs >= deskew_min_GTs
    modes = [(data_rate_GTs / 2.0, 2, deskew)]
    if data_rate_GTs / 2.0 > fck_cap_GHz:
        modes.append((data_rate_GTs / 4.0, 4, deskew))
    return modes


def ucie_default_mode(data_rate_GTs: float,
                      fck_cap_GHz: float = _FCK_CAP_GHZ,
                      deskew_min_GTs: float = _DESKEW_REQUIRED_MIN_GTS
                      ) -> Tuple[float, int, bool]:
    """Recommended UCIe mode: half-rate if its fCK is under the cap, else quarter."""
    modes = ucie_modes(data_rate_GTs, fck_cap_GHz, deskew_min_GTs)
    for fCK, M, deskew in modes:
        if fCK <= fck_cap_GHz:
            return (fCK, M, deskew)
    return modes[-1]


# ---------------------------------------------------------------------------
# Lane-energy providers
# ---------------------------------------------------------------------------
def make_proxy_provider(E_lane_R_pJ: float, R_data_Gbps: float,
                        tol: float = 0.05) -> LaneEnergyProvider:
    """E_lane(rate) provider from a single data-rate run.

    Exact (reuse) when the requested rate is within `tol` of R — this is the
    half-rate clock case (equiv = R).  Otherwise the same number flagged
    'proxied' (quarter-rate equiv = R/2: kept as the rate-independent proxy by
    project decision — no extra Liberate run; slightly over-counts).
    """
    def provider(rate_Gbps: float) -> LaneEnergy:
        if R_data_Gbps > 0 and abs(rate_Gbps - R_data_Gbps) / R_data_Gbps <= tol:
            return LaneEnergy(E_lane_R_pJ, "reused")
        return LaneEnergy(E_lane_R_pJ, f"proxied (wanted {rate_Gbps:g}G, used {R_data_Gbps:g}G)")
    return provider


_E_TOTAL_RE = re.compile(r"E_total_pJ_per_bit:\s*([0-9.eE+-]+)")


def lane_energy_from_result_dir(result_dir: str, point: str = "recommended") -> float:
    """Parse a framework run's E_total_pJ_per_bit (alpha=0.5) from results/."""
    fname = {
        "recommended": "recommended_point.txt",
        "best_power":  "best_power_point.txt",
        "best_delay":  "best_delay_point.txt",
    }[point]
    path = os.path.join(result_dir, fname)
    with open(path) as fh:
        m = _E_TOTAL_RE.search(fh.read())
    if not m:
        raise ValueError(f"E_total_pJ_per_bit not found in {path}")
    return float(m.group(1))


# ---------------------------------------------------------------------------
def _power_to_pJ_per_bit(power_uW: float, total_bits_per_s: float) -> float:
    """uW over a bit-rate -> pJ per bit."""
    if total_bits_per_s <= 0:
        return 0.0
    return (power_uW * 1e-6 / total_bits_per_s) * 1e12


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class ClockingResult:
    enabled:           bool

    # echoed / derived
    R_Gbps:            float = 0.0
    N_lanes:           int   = 0
    n_clocks:          int   = 1
    M:                 int   = 2
    deskew_required:   bool  = False

    x_lanes_per_clock: float = 0.0
    f_clk_GHz:         float = 0.0
    equiv_rate_Gbps:   float = 0.0
    clock_wires:       int   = 0
    clock_bumps_total: int   = 0

    E_lane_equiv_pJ:   float = 0.0
    E_lane_provenance: str   = ""

    # breakdown (pJ per data bit, per data lane)
    E_clk_lane_pJ:     float = 0.0
    E_dll_pJ:          float = 0.0
    E_phase_int_pJ:    float = 0.0
    E_serializer_pJ:   float = 0.0
    E_deserializer_pJ: float = 0.0
    E_dcc_pJ:          float = 0.0
    E_clock_total_pJ:  float = 0.0     # <-- adds onto LinkMetrics.E_total

    fraction_of_link:  Optional[float] = None
    mode:              str = "disabled"
    warnings:          List[str] = field(default_factory=list)

    def report(self) -> str:
        if not self.enabled:
            return "=== Clocking ===\n  DISABLED (no clocking overhead added)."
        wires = "1 differential pair = 2 wires"
        dk = "Required" if self.deskew_required else "Optional/off"
        lines = [
            "=== Clocking (UCIe forwarded clock) ===",
            f"  Mode             : {self.mode}  (M={self.M}, fCK={self.f_clk_GHz:g} GHz)",
            f"  Link             : R={self.R_Gbps:g} Gb/s  N={self.N_lanes} data lanes  "
            f"n_clocks={self.n_clocks}",
            f"  Forwarded clock  : {wires}, shared by x={self.x_lanes_per_clock:g} data lanes",
            f"  Clock bumps      : {self.clock_bumps_total} (one direction)  "
            f"vs {self.N_lanes} data bumps",
            f"  Equiv clock lane : {self.equiv_rate_Gbps:g} Gb/s  "
            f"(E_lane={self.E_lane_equiv_pJ:.4f} pJ/bit, {self.E_lane_provenance})",
            f"  Per-lane deskew  : {dk}",
            "  -- Energy (pJ per data bit, per data lane) --",
            f"  Clock lane       : {self.E_clk_lane_pJ:.4f}",
            f"  DLL / phase gen  : {self.E_dll_pJ:.4f}",
            f"  Phase int (PI)   : {self.E_phase_int_pJ:.4f}",
            f"  Serializer       : {self.E_serializer_pJ:.4f}",
            f"  Deserializer     : {self.E_deserializer_pJ:.4f}",
            f"  DCC              : {self.E_dcc_pJ:.4f}",
            f"  TOTAL clocking   : {self.E_clock_total_pJ:.4f}",
        ]
        if self.fraction_of_link is not None:
            lines.append(f"  (= {100*self.fraction_of_link:.1f}% of the data-lane energy/bit)")
        if self.warnings:
            lines.append("  -- Notes --")
            for w in self.warnings:
                lines.append(f"  ! {w}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core model
# ---------------------------------------------------------------------------
def compute_clocking(R_Gbps: float, N_lanes: int, E_lane: LaneEnergyProvider,
                     clk: ClockingConfig, hid: ClockingHiddenConfig,
                     link_energy_pJ: Optional[float] = None) -> ClockingResult:
    """Compute the clocking overhead for one link (pure / no Liberate)."""
    if clk.n_clocks < 1:
        raise ValueError(f"clocking.n_clocks must be >= 1, got {clk.n_clocks}")

    fCK_d, M_d, deskew_d = ucie_default_mode(R_Gbps, hid.fck_cap_GHz, hid.deskew_required_min_GTs)
    M_req = parse_ratio(clk.ratio)            # int 1/2/4, or None -> UCIe rule
    M = M_d if M_req is None else M_req
    dk_req = parse_deskew(clk.deskew)         # True/False, or None -> UCIe rule
    deskew_required = bool(deskew_d) if dk_req is None else dk_req

    warnings: List[str] = []

    x = N_lanes / clk.n_clocks
    f_clk = R_Gbps / M
    equiv_rate = 2.0 * f_clk
    W = 2   # UCIe forwards a differential clock pair (2 wires)
    mode = {2: "half-rate", 4: "quarter-rate"}.get(M, "full-rate")

    if M == 2 and f_clk > hid.fck_cap_GHz:
        warnings.append(f"half-rate fCK={f_clk:g} GHz exceeds the ~{hid.fck_cap_GHz:g} GHz "
                        f"UCIe cap — a real design would use quarter-rate (M=4) here")

    # 1. clock lane (forwarded wire)
    le = E_lane(equiv_rate)
    E_clk_lane = W * 4.0 * le.pJ_per_bit / (x * M)
    if le.provenance.startswith("proxied"):
        warnings.append(f"clock-lane E_lane({equiv_rate:g}G) {le.provenance} — proxy kept "
                        f"(quarter-rate; no extra run), slightly over-counts")
    warnings.append("differential clock modeled as 2x a single-ended lane "
                    "(reduced-swing differential would be somewhat less)")

    # 2. DLL / phase generator (per clock domain, amortized over x)
    if M >= 2:
        E_dll = _power_to_pJ_per_bit(hid.dll_power_uW * (M / 2.0), x * R_Gbps * 1e9)
        warnings.append("DLL/phase-gen power estimated by analogy to the measured DCC (not measured)")
    else:
        E_dll = 0.0

    # 3. per-lane phase interpolator (deskew) — per lane, no amortization
    if deskew_required:
        E_pi = _power_to_pJ_per_bit(hid.pi_power_uW_per_lane, R_Gbps * 1e9)
        warnings.append("per-lane PI (deskew) power estimated by analogy to the measured DCC (not measured)")
    else:
        E_pi = 0.0

    # 4. serializer / deserializer (per lane): M:1 = log2(M) 2:1 stages
    n_stages = int(round(math.log2(M))) if M > 1 else 0
    E_ser   = n_stages * hid.serializer_fj_per_stage   * 1e-3   # fJ -> pJ
    E_deser = n_stages * hid.deserializer_fj_per_stage * 1e-3
    if n_stages > 0:
        warnings.append("RX deserializer assumed symmetric to TX serializer (not measured)")

    # 5. DCC (per clock domain, amortized over x): when both edges used
    E_dcc = _power_to_pJ_per_bit(hid.dcc_power_uW, x * R_Gbps * 1e9) if M >= 2 else 0.0

    E_total = E_clk_lane + E_dll + E_pi + E_ser + E_deser + E_dcc
    frac = (E_total / link_energy_pJ) if link_energy_pJ else None

    return ClockingResult(
        enabled=True,
        R_Gbps=R_Gbps, N_lanes=N_lanes, n_clocks=clk.n_clocks, M=M,
        deskew_required=deskew_required,
        x_lanes_per_clock=x, f_clk_GHz=f_clk, equiv_rate_Gbps=equiv_rate,
        clock_wires=W, clock_bumps_total=clk.n_clocks * W,
        E_lane_equiv_pJ=le.pJ_per_bit, E_lane_provenance=le.provenance,
        E_clk_lane_pJ=E_clk_lane, E_dll_pJ=E_dll, E_phase_int_pJ=E_pi,
        E_serializer_pJ=E_ser, E_deserializer_pJ=E_deser, E_dcc_pJ=E_dcc,
        E_clock_total_pJ=E_total, fraction_of_link=frac, mode=mode, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Public API (framework)
# ---------------------------------------------------------------------------
def get_clocking(cfg, E_lane_R_pJ: float,
                 link_energy_pJ: Optional[float] = None) -> ClockingResult:
    """Forwarded-clock overhead for cfg's link, reusing the data-lane energy.

    cfg            : Config (from main.py); reads cfg.link, cfg.clocking, cfg.clocking_hidden.
    E_lane_R_pJ    : data-lane energy/bit (alpha=0.5) at the link data rate
                     (LinkMetrics.E_total_pJ_per_bit).
    link_energy_pJ : optional data-lane E/bit for the %-of-link field
                     (defaults to E_lane_R_pJ).

    Quarter-rate keeps the rate-independent proxy (no extra Liberate run).
    Returns a disabled (zero-energy) result when clocking.enabled is False.
    """
    clk = getattr(cfg, "clocking", None) or ClockingConfig()
    hid = getattr(cfg, "clocking_hidden", None) or ClockingHiddenConfig()

    if not clk.enabled:
        return ClockingResult(enabled=False)

    R = cfg.link.data_rate_Gbps
    N = cfg.link.lane_count
    provider = make_proxy_provider(E_lane_R_pJ, R)
    link_e = link_energy_pJ if link_energy_pJ is not None else E_lane_R_pJ
    return compute_clocking(R, N, provider, clk, hid, link_energy_pJ=link_e)


# ---------------------------------------------------------------------------
# Standalone CLI (testing without the full pipeline)
# ---------------------------------------------------------------------------
def _compare(R, N, E_lane, clk, hid, link_energy_pJ=None) -> str:
    lines = [f"UCIe forwarded-clock overhead  (R={R:g} Gb/s, N={N} lanes, "
             f"n_clocks={clk.n_clocks}, x={N/clk.n_clocks:g})",
             "=" * 100,
             "  pJ per data bit, per data lane.  Forwarded clock = 1 diff pair (2 wires)."]
    hdr = (f"  {'mode':<13} {'M':>2} {'fCK':>6} {'equiv':>6} {'deskew':>7} | "
           f"{'clk-lane':>9} {'dll':>6} {'pi':>6} {'ser':>6} {'dcc':>7} = {'TOTAL':>8}")
    if link_energy_pJ:
        hdr += f" | {'%link':>6}"
    lines += [hdr, "  " + "-" * (len(hdr) - 2)]
    for fCK, M, deskew in ucie_modes(R, hid.fck_cap_GHz, hid.deskew_required_min_GTs):
        c = ClockingConfig(enabled=True, n_clocks=clk.n_clocks, ratio=M, deskew=deskew)
        r = compute_clocking(R, N, E_lane, c, hid, link_energy_pJ=link_energy_pJ)
        dk = "Req" if deskew else "Opt"
        row = (f"  {r.mode:<13} {M:>2} {r.f_clk_GHz:>5g}G {r.equiv_rate_Gbps:>5g}G {dk:>7} | "
               f"{r.E_clk_lane_pJ:>9.4f} {r.E_dll_pJ:>6.4f} {r.E_phase_int_pJ:>6.4f} "
               f"{r.E_serializer_pJ:>6.4f} {r.E_dcc_pJ:>7.4f} = {r.E_clock_total_pJ:>8.4f}")
        if link_energy_pJ:
            row += f" | {100*r.fraction_of_link:>5.1f}%"
        lines.append(row)
    return "\n".join(lines)


# ===========================================================================
# Circuit characterization: generate PDK-correct Spectre testbenches
# ===========================================================================
# Generate decks for the on-die clocking circuits so their energy/power can be
# MEASURED per the configured PDK (replacing the analogy estimates above).  The
# decks reuse the same config-driven device abstraction as tx.py/rx.py
# (cfg.transistor.style + cfg.process.model_include_format; see scripts/device.py).
# Each circuit -> <out>/<name>/<name>.scs + run.sh.  Run it (spectre must be on
# PATH), then parse with parse_iavg() and fold the numbers into clocking_hidden.

def _mos(uid: str, d: str, g: str, s: str, b: str, kind: str,
         w_um: float, cfg, l_um: Optional[float] = None) -> str:
    """One MOSFET line for the configured PDK (mirrors device.py conventions)."""
    t = cfg.transistor
    spec = DeviceSpec.from_cfg(cfg)
    name = t.nmos_name if kind == "n" else t.pmos_name
    L = t.l_um if l_um is None else l_um
    inst = f"x{kind}{uid}"
    if spec.is_finfet:                          # FinFET: width -> fin count
        # FinFET L is RDR-quantized: analog L-overrides are illegal, so the
        # nominal L is always used (longer-L gain trick only works on planar).
        nfin = spec.w_to_nfin(w_um)
        return f"{inst} {d} {g} {s} {b} {name} L={spec.l_nm()}n nfin={nfin}"
    nf_tok = f" nf={t.nf}" if spec.include_nf else ""
    return f"{inst} {d} {g} {s} {b} {name} w={w_um}u l={L}u{nf_tok}"


def _model_header(cfg) -> str:
    """PDK model-include text (reuses tx.py's generator)."""
    import tx
    p = cfg.process
    return tx._gen_model_sp(p.lib_path, p.lib_corner,
                            getattr(p, "lib_corner2", None),
                            fmt=p.model_include_format).rstrip()


def _widths(cfg):
    """(wn, wp, wnb, wpb) representative widths as multiples of the PDK min width."""
    wmin = cfg.transistor.w_min_um
    wn, wp = 4.0 * wmin, 8.0 * wmin     # ~2:1 P:N unit inverter
    return wn, wp, 2.0 * wn, 2.0 * wp


def _clk_period_ps(cfg, M: int) -> float:
    """Forwarded-clock period (ps) = 1000 / (R/M)."""
    return 1000.0 / (cfg.link.data_rate_Gbps / M)


def _cell_inv(cfg, wn, wp) -> str:
    return ("\n.subckt inv a y vdd vss\n"
            f"{_mos('p','y','a','vdd','vdd','p', wp, cfg)}\n"
            f"{_mos('n','y','a','vss','vss','n', wn, cfg)}\n.ends inv\n")


def _cell_tg(cfg, wn, wp) -> str:
    return ("\n.subckt tg a y en enb vdd vss\n"
            f"{_mos('n','a','en','y','vss','n', wn, cfg)}\n"
            f"{_mos('p','a','enb','y','vdd','p', wp, cfg)}\n.ends tg\n")


def _cell_triinv(cfg, wn, wp) -> str:
    return ("\n.subckt triinv a y en enb vdd vss\n"
            f"{_mos('p1','n1','a','vdd','vdd','p', wp, cfg)}\n"
            f"{_mos('p2','y','enb','n1','vdd','p', wp, cfg)}\n"
            f"{_mos('n2','y','en','n2','vss','n', wn, cfg)}\n"
            f"{_mos('n1','n2','a','vss','vss','n', wn, cfg)}\n.ends triinv\n")


def _tb_header(cfg, title: str) -> str:
    return (f"simulator lang=spice\n* {title}\n"
            f"* PDK={cfg.process.node}  VDD={cfg.process.vdd}V  L={cfg.transistor.l_um}um\n\n"
            f"{_model_header(cfg)}\n\nsimulator lang=spice\n")


def _supplies(cfg) -> str:
    return f"Vvdd vdd 0 {cfg.process.vdd}\nVvss vss 0 0\n"


def _clk_src(cfg, name, node, period_ps, high_ps=None) -> str:
    hi = (period_ps / 2.0 - 5.0) if high_ps is None else high_ps
    return f"{name} {node} 0 PULSE(0 {cfg.process.vdd} 0 5p 5p {hi:g}p {period_ps:g}p)\n"


def _deck_serializer(cfg, M: int) -> str:
    wn, wp, _, _ = _widths(cfg)
    per, vdd = _clk_period_ps(cfg, M), cfg.process.vdd
    return "".join([
        _tb_header(cfg, f"2:1 serializer, {cfg.link.data_rate_Gbps:g} Gb/s"),
        _cell_inv(cfg, wn, wp), _cell_tg(cfg, wn, wp),
        "\n.subckt ser d0 d1 clk y vdd vss\nxinvc clk clkb vdd vss inv\n"
        "xtg0 d0 yint clk clkb vdd vss tg\nxtg1 d1 yint clkb clk vdd vss tg\n"
        "xb1 yint yb vdd vss inv\nxb2 yb y vdd vss inv\n.ends ser\n",
        "\n* real load = txip first stage input\n.subckt txload y vdd vss\n"
        f"{_mos('n','dnc','y','vss','vss','n', 2*wn, cfg)}\n"
        f"{_mos('p','dnc','y','vdd','vdd','p', 2*wp, cfg)}\n.ends txload\n",
        "\n* ---- testbench ----\n", _supplies(cfg), _clk_src(cfg, "Vclk", "clk", per),
        f"Vd0 d0 0 {vdd}\nVd1 d1 0 0\nxdut d0 d1 clk y vdd vss ser\nxld y vdd vss txload\n\n",
        f".tran 0.1p {3*per:g}p\n.measure tran iavg avg i(vvdd) from={per:g}p to={3*per:g}p\n",
        f".alter\n* case B 1111: output static -> clock-path energy only\nVd1 d1 0 {vdd}\n.end\n",
    ])


def _deck_deserializer(cfg, M: int) -> str:
    wn, wp, _, _ = _widths(cfg)
    per, vdd = _clk_period_ps(cfg, M), cfg.process.vdd
    return "".join([
        _tb_header(cfg, f"1:2 deserializer, {cfg.link.data_rate_Gbps:g} Gb/s"),
        _cell_inv(cfg, wn, wp), _cell_tg(cfg, wn, wp),
        "\n.subckt des din clk q0 q1 vdd vss\nxinvc clk clkb vdd vss inv\n"
        "xtg0 din n0 clk clkb vdd vss tg\nxtg1 din n1 clkb clk vdd vss tg\n"
        "xh0 n0 q0 vdd vss inv\nxh1 n1 q1 vdd vss inv\n.ends des\n",
        "\n* ---- testbench ----\n", _supplies(cfg),
        f"Vdin din 0 PULSE(0 {vdd} 0 5p 5p {per/2-5:g}p {per:g}p)\n",
        _clk_src(cfg, "Vclk", "clk", 2 * per),
        "xdut din clk q0 q1 vdd vss des\nCl0 q0 0 2f\nCl1 q1 0 2f\n\n",
        f".tran 0.1p {4*per:g}p\n.measure tran iavg avg i(vvdd) from={per:g}p to={4*per:g}p\n.end\n",
    ])


def _deck_dcc(cfg, M: int) -> str:
    wn, wp, _, _ = _widths(cfg)
    per, vdd = _clk_period_ps(cfg, M), cfg.process.vdd
    la = 2.0 * cfg.transistor.l_um
    wbig = 20.0 * cfg.transistor.w_min_um
    return "".join([
        _tb_header(cfg, f"duty-cycle corrector, fCK={cfg.link.data_rate_Gbps/M:g} GHz"),
        _cell_inv(cfg, wn, wp),
        "\n.subckt dca clk vctrl clkout vdd vss\n"
        f"{_mos('p','n1','clk','vdd','vdd','p', wp, cfg)}\n"
        f"{_mos('n','n1','clk','vss','vss','n', wn, cfg)}\n"
        f"{_mos('nx','n1','vctrl','vss','vss','n', 2.5*wn, cfg)}\n"
        "xo n1 clkout vdd vss inv\n.ends dca\n",
        "\n.subckt ota vp vn vout vbias vdd vss\n"
        f"{_mos('t','nt','vbias','vss','vss','n', wbig, cfg, l_um=2*la)}\n"
        f"{_mos('1','d1','vp','nt','vss','n', wbig, cfg, l_um=la)}\n"
        f"{_mos('2','vout','vn','nt','vss','n', wbig, cfg, l_um=la)}\n"
        f"{_mos('3','d1','d1','vdd','vdd','p', 2*wbig, cfg, l_um=la)}\n"
        f"{_mos('4','vout','d1','vdd','vdd','p', 2*wbig, cfg, l_um=la)}\n.ends ota\n",
        "\n* ---- testbench ----\n", _supplies(cfg),
        f"Vclkin clkin 0 PULSE(0 {vdd} 0 5p 5p {0.36*per:g}p {per:g}p)\n",
        "Rbias vdd nbias 200k\n"
        f"{_mos('bias','nbias','nbias','vss','vss','n', wbig, cfg, l_um=2*la)}\n"
        "Rr1 vdd vref 200k\nRr2 vref vss 200k\nxdca clkin vctrl clkout vdd vss dca\n"
        "Cclk clkout 0 3f\nRdet clkout vavg 10k\nCdet vavg 0 0.2p\n"
        "xota vref vavg vctrl nbias vdd vss ota\nCint vctrl 0 0.3p\n"
        f".ic v(vctrl)={0.55*vdd:g} v(vavg)={0.5*vdd:g}\n\n",
        f".tran 0.1p {320*per:g}p\n"
        f".measure tran iavg avg i(vvdd) from={240*per:g}p to={320*per:g}p\n"
        f".measure tran vctrl_f find v(vctrl) at={312*per:g}p\n.end\n",
    ])


def _deck_dll(cfg, M: int) -> str:
    wn, wp, _, _ = _widths(cfg)
    per = _clk_period_ps(cfg, M)
    nstage = max(2, M)
    body = [_tb_header(cfg, f"DLL delay-line phase gen ({nstage} taps), fCK={cfg.link.data_rate_Gbps/M:g} GHz"),
            _cell_inv(cfg, wn, wp), "\n* ---- testbench ----\n",
            _supplies(cfg), _clk_src(cfg, "Vclk", "ck0", per)]
    for i in range(nstage):
        body.append(f"xd{i} ck{i} ck{i+1} vdd vss inv\n")
        body.append(f"xt{i} ck{i+1} tap{i} vdd vss inv\nCt{i} tap{i} 0 2f\n")
    body.append(f"\n.tran 0.1p {4*per:g}p\n"
                f".measure tran iavg avg i(vvdd) from={per:g}p to={4*per:g}p\n.end\n")
    return "".join(body)


def _deck_pi(cfg, M: int) -> str:
    wn, wp, _, _ = _widths(cfg)
    per, vdd = _clk_period_ps(cfg, M), cfg.process.vdd
    return "".join([
        _tb_header(cfg, f"phase interpolator (I/Q blend), fCK={cfg.link.data_rate_Gbps/M:g} GHz"),
        _cell_inv(cfg, wn, wp), _cell_triinv(cfg, wn, wp),
        "\n.subckt pi ckI ckQ y en enb vdd vss\nxtI ckI y en enb vdd vss triinv\n"
        "xtQ ckQ y en enb vdd vss triinv\nxbuf y yo vdd vss inv\n.ends pi\n",
        "\n* ---- testbench ----\n", _supplies(cfg),
        f"Ven en 0 {vdd}\nVenb enb 0 0\n", _clk_src(cfg, "VckI", "ckI", per),
        f"VckQ ckQ 0 PULSE(0 {vdd} {per/4:g}p 5p 5p {per/2-5:g}p {per:g}p)\n",
        "xdut ckI ckQ y en enb vdd vss pi\nCl yo 0 2f\n\n",
        f".tran 0.1p {4*per:g}p\n.measure tran iavg avg i(vvdd) from={per:g}p to={4*per:g}p\n.end\n",
    ])


_TB_DECKS = {
    "serializer":   _deck_serializer,
    "deserializer": _deck_deserializer,
    "dcc":          _deck_dcc,
    "dll":          _deck_dll,
    "pi":           _deck_pi,
}


def _run_sh(filename: str):
    cmd = f"spectre -64 {filename} -format psfascii"
    return f"#!/bin/bash\n{cmd}\n"


def generate_testbenches(cfg, out_dir: str, ratio=None, circuits=None) -> dict:
    """Write PDK-correct .scs + run.sh for each clocking circuit. -> {name: path}."""
    M = parse_ratio(ratio)
    if M is None:
        _, M, _ = ucie_default_mode(cfg.link.data_rate_Gbps)
    names = list(circuits) if circuits else list(_TB_DECKS)
    written = {}
    for name in names:
        cdir = os.path.join(out_dir, name)
        os.makedirs(cdir, exist_ok=True)
        scs = os.path.join(cdir, f"{name}.scs")
        with open(scs, "w") as fh:
            fh.write(_TB_DECKS[name](cfg, M))
        sh_text = _run_sh(f"{name}.scs")
        sh = os.path.join(cdir, "run.sh")
        with open(sh, "w") as fh:
            fh.write(sh_text)
        os.chmod(sh, 0o755)
        written[name] = scs
    return written


_IAVG_RE = re.compile(r"iavg\s*=?\s*([-0-9.eE+]+)")


def parse_iavg(mt0_or_measure_path: str) -> Optional[float]:
    """Return |iavg| in A from a Spectre .mt0 / .measure file (first instance)."""
    vals = _parse_all_iavg(mt0_or_measure_path)
    return vals[0] if vals else None


def _parse_all_iavg(measure_path: str):
    """All |iavg| values (A) from a Spectre .measure file (one per .alter run)."""
    if not os.path.exists(measure_path):
        return []
    with open(measure_path) as fh:
        return [abs(float(x)) for x in _IAVG_RE.findall(fh.read())]


def energy_fj_per_bit(iavg_A: float, vdd: float, data_rate_Gbps: float) -> float:
    """Average supply current -> energy per data bit (fJ)."""
    return (iavg_A * vdd) / (data_rate_Gbps * 1e9) * 1e15


def power_uW(iavg_A: float, vdd: float) -> float:
    return iavg_A * vdd * 1e6


def characterize(cfg, out_dir: str, run: bool = True, timeout_s: int = 1800):
    """Generate + run the clocking-circuit decks; return (ClockingHiddenConfig, report_lines).

    Measured Spectre results replace the config estimates per circuit.  Any
    circuit whose sim fails (no Cadence env, sim error, missing output) falls
    back to the config's clocking_hidden value, recorded in the report.

    Decks + run.sh + outputs land in out_dir/<circuit>/.
    """
    import subprocess
    clk  = getattr(cfg, "clocking", None) or ClockingConfig()
    hid0 = getattr(cfg, "clocking_hidden", None) or ClockingHiddenConfig()
    vdd, R = cfg.process.vdd, cfg.link.data_rate_Gbps

    decks = generate_testbenches(cfg, out_dir, ratio=clk.ratio)

    iavgs, status = {}, {}
    for name, scs in decks.items():
        cdir = os.path.dirname(scs)
        if run:
            try:
                proc = subprocess.run(["bash", "run.sh"], cwd=cdir,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      timeout=timeout_s)
                if proc.returncode != 0:
                    status[name] = "fallback: spectre rc!=0"
            except Exception as e:
                status[name] = f"fallback: {type(e).__name__}"
        vals = _parse_all_iavg(os.path.join(cdir, f"{name}.measure"))
        if vals:
            iavgs[name] = vals
            status.setdefault(name, "measured")
        else:
            status.setdefault(name, "fallback: no .measure output")

    # serializer typical (alpha=0.5): E(1111 clock-only) + 0.5*(E(1010 worst) - E(1111))
    def _ser_typ():
        v = iavgs.get("serializer")
        if not v:
            return None
        eA = energy_fj_per_bit(v[0], vdd, R)
        eB = energy_fj_per_bit(v[1], vdd, R) if len(v) > 1 else eA
        return eB + 0.5 * (eA - eB)

    ser = _ser_typ()
    des = energy_fj_per_bit(iavgs["deserializer"][0], vdd, R) if iavgs.get("deserializer") else None
    dcc = power_uW(iavgs["dcc"][0], vdd) if iavgs.get("dcc") else None
    dll = power_uW(iavgs["dll"][0], vdd) if iavgs.get("dll") else None
    pim = power_uW(iavgs["pi"][0], vdd)  if iavgs.get("pi")  else None

    hid = ClockingHiddenConfig(
        serializer_fj_per_stage   = ser if ser is not None else hid0.serializer_fj_per_stage,
        deserializer_fj_per_stage = des if des is not None else hid0.deserializer_fj_per_stage,
        dcc_power_uW              = dcc if dcc is not None else hid0.dcc_power_uW,
        dll_power_uW              = dll if dll is not None else hid0.dll_power_uW,
        pi_power_uW_per_lane      = pim if pim is not None else hid0.pi_power_uW_per_lane,
        fck_cap_GHz               = hid0.fck_cap_GHz,
        deskew_required_min_GTs   = hid0.deskew_required_min_GTs,
    )

    lines = ["  Clocking-circuit characterization (Spectre):"]
    for nm, unit, val in (("serializer",  "fJ/stage",  hid.serializer_fj_per_stage),
                          ("deserializer", "fJ/stage",  hid.deserializer_fj_per_stage),
                          ("dcc",          "uW/domain", hid.dcc_power_uW),
                          ("dll",          "uW/domain", hid.dll_power_uW),
                          ("pi",           "uW/lane",   hid.pi_power_uW_per_lane)):
        lines.append(f"    {nm:12s} {val:9.3f} {unit:9s} [{status.get(nm, '?')}]")
    return hid, lines


# ---------------------------------------------------------------------------
# Standalone CLI (testing without the full pipeline)
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="UCIe forwarded-clock power overhead.")
    ap.add_argument("--rate", type=float, default=16.0, help="data rate R per lane (Gb/s)")
    ap.add_argument("--lanes", type=int, default=8, help="total data lanes N")
    ap.add_argument("--n-clocks", type=int, default=1)
    ap.add_argument("--ratio", type=str, default=None,
                    help="sdr/ddr/qdr or ucie (omit to compare all modes)")
    ap.add_argument("--er", type=float, default=None, help="E_lane (alpha=0.5) at R [pJ/bit]")
    ap.add_argument("--result-dir", type=str, default=None)
    ap.add_argument("--link", type=float, default=None, help="data-lane E/bit for %%-of-link")
    ap.add_argument("--gen-testbenches", metavar="CONFIG", default=None,
                    help="generate PDK-correct clocking-circuit Spectre decks from a config, then exit")
    ap.add_argument("--characterize", metavar="CONFIG", default=None,
                    help="generate AND run the decks (needs Cadence env), report measured constants, then exit")
    ap.add_argument("--gen-out", default=None, help="output dir for --gen-testbenches / --characterize")
    args = ap.parse_args()

    # Testbench-generation mode: emit per-PDK .scs decks and exit.
    if args.gen_testbenches:
        import main as _m
        cfg = _m.load_config(args.gen_testbenches)
        out = args.gen_out or os.path.join(
            os.path.dirname(os.path.abspath(args.gen_testbenches)), "clocking_char")
        written = generate_testbenches(cfg, out, ratio=args.ratio)
        print(f"PDK={cfg.process.node}  R={cfg.link.data_rate_Gbps:g} Gb/s -> {out}")
        for _n, _p in written.items():
            print(f"  {_n:12s} -> {_p}")
        return

    # Characterize mode: generate + run + report measured constants.
    if args.characterize:
        import main as _m
        cfg = _m.load_config(args.characterize)
        out = args.gen_out or os.path.join(
            os.path.dirname(os.path.abspath(args.characterize)), "clocking_char")
        _hid, _lines = characterize(cfg, out)
        print("\n".join(_lines))
        return

    if args.er is not None:
        E_lane_R = args.er
    elif args.result_dir:
        E_lane_R = lane_energy_from_result_dir(args.result_dir)
    else:
        E_lane_R = 1.497046
    link = args.link if args.link is not None else E_lane_R
    provider = make_proxy_provider(E_lane_R, args.rate)
    hid = ClockingHiddenConfig()
    clk = ClockingConfig(enabled=True, n_clocks=args.n_clocks, ratio=args.ratio)
    if args.ratio is None:
        print(_compare(args.rate, args.lanes, provider, clk, hid, link_energy_pJ=link))
    else:
        print(compute_clocking(args.rate, args.lanes, provider, clk, hid, link_energy_pJ=link).report())


if __name__ == "__main__":
    main()
