"""
co_opt_pareto.py — joint TX/RX sizing with Pareto-frontier search.

Characterizes each TX config over a load sweep and each RX config over an
input-slew sweep, then matches all TX/RX pairs by interpolation to build the
(energy/bit, delay) Pareto frontier in O(N_tx + N_rx) Liberate/CharLib runs.
"""

import contextlib
import copy
import csv
import io
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Ensure scripts/ is importable regardless of CWD
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# ---------------------------------------------------------------------------
# Physical / algorithmic constants
# ---------------------------------------------------------------------------
_COX_FF_PER_UM2 = 8.6        # Gate-oxide cap density for a 65nm-class node (fF/µm²)
_F_PARASITIC    = 3.59        # Optimal stage fan-out including parasitic caps
_GOLDEN         = (math.sqrt(5) - 1) / 2   # ≈ 0.618

_N_MIN, _N_MAX  = 2, 12
_F_LO,  _F_HI   = 1.5, 8.0
_B_LO,  _B_HI   = 1.0, 4.0
_MAX_TX_TRANSISTORS = 400   # baseline finger-count limit (calibrated for a 65nm-class node, w_min=0.12 µm)
# The effective limit scales with process: _MAX_TX_TRANSISTORS is the cap when
# w_n_min ≥ 0.12 µm.  For FinFET nodes with much smaller w_min (e.g. 0.01 µm on
# a 16nm-class node), the limit is raised proportionally so the maximum achievable
# final-stage drive width stays constant across processes.

# RX search bounds
_PS_LO, _PS_HI = 0.5, 16.0   # preamp_scale range  (×w_n_min)
_SR_LO, _SR_HI = 1.0,  8.0   # RX stage ratio range
_BR_LO, _BR_HI = 1.0,  4.0   # RX beta range


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class CoOptCandidate:
    """
    One (TX configuration, RX configuration) pair evaluated via full Liberate/CharLib run.

    Parameters are populated at construction time; result fields are filled
    in-place by _run_one_pair() during Phase 2.
    """
    # TX parameters
    tx_num_stages:     int
    tx_beta_ratio:     float
    tx_stage_ratio:    float
    tx_inverter_sizes: List[Tuple[float, float]]   # [(w_n_um, w_p_um), ...]

    # RX parameters
    rx_preamp_scale:   float
    rx_stage_ratio_rx: float
    rx_beta_rx:        float
    rx_w_preamp_n_um:  float
    rx_w_preamp_p_um:  float
    rx_w_buf_n_um:     float
    rx_w_buf_p_um:     float

    # Results — populated progressively by _run_one_pair()
    rx_cap_in_pF:       float = 0.0   # from initial RX characterization
    tx_slew_ns:         float = 0.0   # max(rise, fall) TX output slew from NLDM .lib
    slew_ui_frac:       float = 0.0   # tx_slew_ns / UI_ns
    slew_feasible:      bool  = False  # True iff tx_slew_ns ≤ target_ns
    latency_feasible:   bool  = False  # True iff TX+RX delay ≤ max_latency_ns
    # Metrics from get_metrics.extract() (same as Stage 9)
    E_tx_pJ_per_bit:    float = 0.0
    E_rx_pJ_per_bit:    float = 0.0
    E_term_pJ_per_bit:  float = 0.0
    E_total_pJ_per_bit: float = 0.0
    total_delay_rr_ps:  float = 0.0
    total_delay_ff_ps:  float = 0.0
    # Status
    sim_success:        bool  = False
    on_pareto:          bool  = False
    is_recommended:     bool  = False
    pair_id:            str   = ""


@dataclass
class CoOptResult:
    """Result of the co-optimisation search."""
    pareto_front:   List[CoOptCandidate]
    all_feasible:   List[CoOptCandidate]
    all_candidates: List[CoOptCandidate]
    recommended_point: Optional[CoOptCandidate]
    tx_input_slew_ns: float
    ui_ns:          float
    max_latency_ns: float       # TX+RX latency budget (ns), 0 = unconstrained
    n_total:        int
    n_feasible:     int
    csv_path:       Optional[str] = None
    plot_path:      Optional[str] = None

    def report(self) -> str:
        sep = "=" * 62
        lines = [
            sep,
            "  TX/RX Co-Optimisation — Pareto Frontier",
            sep,
            f"  UI                : {self.ui_ns*1000:.2f} ps",
            f"  TX input slew     : {self.tx_input_slew_ns*1000:.2f} ps"
            f"  ({self.tx_input_slew_ns/self.ui_ns*100:.0f}% of UI)",
        ]
        if self.max_latency_ns > 0:
            lines.append(
                f"  Latency budget    : ≤ {self.max_latency_ns*1000:.2f} ps"
                f"  ({self.max_latency_ns/self.ui_ns:.0f} UI, TX+RX only)"
            )
        lines += [
            f"  Configurations    : {self.n_total}",
            f"  Feasible pairs    : {self.n_feasible}",
            f"  Pareto points     : {len(self.pareto_front)}",
            "",
            "  Pareto Frontier (sorted by worst-case delay):",
            f"  {'#':>3}  {'E/bit (pJ)':>12}  {'Delay-WC (ps)':>14}  "
            f"{'TX (N/f/β)':>18}  {'RX (ps/sr/β)':>18}",
            "  " + "-" * 75,
        ]
        if self.recommended_point is not None:
            p = self.recommended_point
            wc = max(p.total_delay_rr_ps, p.total_delay_ff_ps)
            lines += [
                "  Recommended default:",
                f"    pair={p.pair_id}  E/bit={p.E_total_pJ_per_bit:.4f} pJ  "
                f"Delay-WC={wc:.1f} ps",
                "",
            ]
        for i, p in enumerate(sorted(self.pareto_front,
                                     key=lambda x: max(x.total_delay_rr_ps, x.total_delay_ff_ps))):
            wc = max(p.total_delay_rr_ps, p.total_delay_ff_ps)
            lines.append(
                f"  {i+1:>3}  {p.E_total_pJ_per_bit:>12.4f}  "
                f"{wc:>14.1f}  "
                f"  N={p.tx_num_stages}/f={p.tx_stage_ratio:.2f}/β={p.tx_beta_ratio:.2f}"
                f"  ps={p.rx_preamp_scale:.2f}/sr={p.rx_stage_ratio_rx:.2f}/β={p.rx_beta_rx:.2f}"
            )
        if self.csv_path:
            lines.append(f"\n  Results CSV : {self.csv_path}")
        if self.plot_path:
            lines.append(f"  Pareto plot : {self.plot_path}")
        lines.append(sep)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Latin Hypercube Sampling (pure Python — no scipy dependency)
# ---------------------------------------------------------------------------

def _lhs(n_samples: int, n_dims: int, seed: int = 42) -> List[List[float]]:
    """Return an n_samples × n_dims Latin Hypercube sample in [0, 1]^n_dims."""
    import random
    rng = random.Random(seed)
    cols = []
    for _ in range(n_dims):
        perm = list(range(n_samples))
        rng.shuffle(perm)
        cols.append([(perm[i] + rng.random()) / n_samples for i in range(n_samples)])
    return [[cols[d][i] for d in range(n_dims)] for i in range(n_samples)]


def _scale(u: float, lo: float, hi: float) -> float:
    return lo + u * (hi - lo)


# ---------------------------------------------------------------------------
# Analytical helpers (shared with sizing logic)
# ---------------------------------------------------------------------------

def _analytical_rx_cap_pF(w_preamp_n: float, w_preamp_p: float,
                           l_um: float) -> float:
    """Estimate RX pre-amp input gate cap (fF → pF)."""
    c_fF = (w_preamp_n + w_preamp_p) * l_um * _COX_FF_PER_UM2
    return max(c_fF / 1000.0, 1e-6)


def _analytical_tx_init(w_n: float, w_p: float, l: float,
                         cap_load_pF: float,
                         tx_cap_in_pF: Optional[float] = None) -> Tuple[int, float, float]:
    """Return (N_init, f_init, beta_init) via logical-effort theory.

    If tx_cap_in_pF is provided (e.g. from a SPICE measurement) it is used
    directly instead of the Cox-based analytical estimate.  The analytical
    formula underestimates FinFET gate cap by ~20x and should be overridden
    whenever a measured value is available.
    """
    if tx_cap_in_pF is not None and tx_cap_in_pF > 0:
        c_in_pF = tx_cap_in_pF
    else:
        c_in_fF = (w_n + w_p) * l * _COX_FF_PER_UM2
        c_in_pF = max(c_in_fF / 1000.0, 1e-6)
    FO = max(cap_load_pF / c_in_pF, 1.0)
    N_real = math.log(FO) / math.log(_F_PARASITIC)
    N = max(_N_MIN, round(N_real))
    if N % 2 != 0:
        N = N + 1 if N_real > N else max(_N_MIN, N - 1)
    if N % 2 != 0:
        N += 1
    f    = FO ** (1.0 / N) if N > 0 else _F_PARASITIC
    beta = w_p / w_n if w_n > 0 else 2.0
    return N, f, beta


def _compute_inverter_sizes(
    w_n_min: float, w_p_min: float,
    num_stages: int, beta_ratio: float, stage_ratio: float,
) -> List[Tuple[float, float]]:
    """Compute geometrically-scaled inverter chain widths."""
    return [(round(w_n_min * (stage_ratio ** i), 4),
             round(w_n_min * (stage_ratio ** i) * beta_ratio, 4))
            for i in range(num_stages)]


def _tx_finger_count(sizes: List[Tuple[float, float]], w_max_um: float) -> int:
    """Estimate total parallel-finger count across all inverter stages."""
    import math as _math
    total = 0
    for w_n, w_p in sizes:
        total += _math.ceil(w_n / w_max_um)
        total += _math.ceil(w_p / w_max_um)
    return total


# ---------------------------------------------------------------------------
# TX configuration search space generation
# ---------------------------------------------------------------------------

@dataclass
class _TxConfig:
    num_stages:     int
    beta_ratio:     float
    stage_ratio:    float
    inverter_sizes: List[Tuple[float, float]]


def _gen_tx_configs(ch_shunt_cap_pF: float, tx_obj,
                    budget: int,
                    rx_cap_in_pF: float = 0.0,
                    tx_cap_in_pF: Optional[float] = None) -> List[_TxConfig]:
    """
    Generate `budget` diverse TX configurations centred on the analytical
    logical-effort optimum for the effective TX load.

    The first ~10 configs are hand-picked perturbations around the
    analytical optimum (priority candidates).  If `budget` exceeds that
    count, additional configs are generated via Latin Hypercube Sampling
    over the (N, f, beta) design space so the full budget is honoured.

    When the channel RC is embedded in the TX netlist, the TX last stage
    drives the channel RC ladder with the RX input cap as the far-end
    load.  The effective sizing target is the channel shunt cap plus the
    RX input cap (which the TX must charge through the channel).

    tx_cap_in_pF : measured unit-inverter input capacitance (pF).  When
        provided, it replaces the Cox-based analytical estimate for N_opt
        computation.  Critical for FinFET nodes where the analytical
        formula underestimates C_in by ~20x.
    """
    sizing_cap_pF = ch_shunt_cap_pF + rx_cap_in_pF
    N_opt, f_opt, beta_opt = _analytical_tx_init(
        tx_obj.w_n_um, tx_obj.w_p_um, tx_obj.l_um, sizing_cap_pF,
        tx_cap_in_pF=tx_cap_in_pF,
    )
    if N_opt % 2 != 0:
        N_opt += 1
    N_opt = max(_N_MIN, min(_N_MAX, N_opt))

    # Compute a process-adaptive finger-count limit so that the maximum
    # achievable final-stage drive width is constant across technology nodes.
    # Baseline: 400 fingers × 0.12 µm = 48 µm for a 65nm-class planar node.
    # FinFET nodes with smaller w_min (e.g. 0.01 µm) can pack the same drive
    # width in proportionally more (but physically smaller) fingers, so the
    # limit scales inversely with w_n_um.
    _W_MIN_BASELINE_UM = 0.12   # 65nm-class reference
    max_tx_transistors = max(
        _MAX_TX_TRANSISTORS,
        int(_MAX_TX_TRANSISTORS * _W_MIN_BASELINE_UM / tx_obj.w_n_um),
    )

    # Pre-compute the overall gain ratio so we can derive f for any N
    c_in_pF = max((tx_obj.w_n_um + tx_obj.w_p_um) * tx_obj.l_um
                  * _COX_FF_PER_UM2 / 1000.0, 1e-6)
    FO = max(sizing_cap_pF / c_in_pF, 1.0)

    def f_for_n(N: int) -> float:
        return max(_F_LO, min(_F_HI, FO ** (1.0 / max(N, 1))))

    def _make(N: int, f: float, beta: float) -> Optional[_TxConfig]:
        N    = max(_N_MIN, min(_N_MAX, int(round(N))))
        if N % 2 != 0:
            N = min(_N_MAX, N + 1)
        f    = max(_F_LO, min(_F_HI, f))
        beta = max(_B_LO, min(_B_HI, beta))
        sizes = _compute_inverter_sizes(tx_obj.w_n_um, tx_obj.w_p_um, N, beta, f)
        if _tx_finger_count(sizes, tx_obj.w_max_um) > max_tx_transistors:
            return None
        return _TxConfig(N, beta, f, sizes)

    def _key(cfg: _TxConfig):
        return (cfg.num_stages,
                round(cfg.stage_ratio, 2),
                round(cfg.beta_ratio, 2))

    # --- Phase 1: hand-picked priority candidates around the optimum ------
    raw = [
        (N_opt,                      f_opt,                  beta_opt),        # 1 — analytical optimum
        (max(_N_MIN, N_opt - 2),     f_for_n(N_opt - 2),     beta_opt),        # 2 — fewer stages
        (min(_N_MAX, N_opt + 2),     f_for_n(N_opt + 2),     beta_opt),        # 3 — more stages
        (N_opt,                      min(_F_HI, f_opt * 1.5), beta_opt),       # 4 — stronger last stage
        (N_opt,                      max(_F_LO, f_opt * 0.75), beta_opt),      # 5 — weaker last stage
        (N_opt,                      f_opt,                  min(_B_HI, beta_opt * 1.3)),  # 6 — higher β
        (N_opt,                      f_opt,                  max(_B_LO, beta_opt * 0.8)),  # 7 — lower β
        (min(_N_MAX, N_opt + 4),     f_for_n(N_opt + 4),     beta_opt),        # 8 — many stages
        (N_opt,                      min(_F_HI, f_opt * 2.0), beta_opt),       # 9 — very strong last stage
        (max(_N_MIN, N_opt - 4),     f_for_n(N_opt - 4),     beta_opt),        # 10 — very few stages
    ]

    seen = set()
    result = []
    for N, f, beta in raw[:budget]:
        cfg_i = _make(N, f, beta)
        if cfg_i is None:
            continue
        key   = _key(cfg_i)
        if key not in seen:
            seen.add(key)
            result.append(cfg_i)

    # --- Phase 2: LHS fill when budget exceeds hand-picked count ----------
    if len(result) < budget:
        import random as _rng
        rng = _rng.Random(42)          # reproducible across runs

        remaining = budget - len(result)
        # Even N values in [_N_MIN, _N_MAX], sorted by distance from N_opt so
        # LHS fills start from the most promising stage counts rather than N_MIN.
        n_vals = list(range(_N_MIN, _N_MAX + 1, 2))
        n_vals_lhs = sorted(n_vals, key=lambda n: abs(n - N_opt))

        # Latin Hypercube Sampling over (N_index, f, beta)
        n_dim = len(n_vals_lhs)
        # Generate `remaining` stratified samples for f and beta
        f_lo, f_hi     = _F_LO, _F_HI
        b_lo, b_hi     = _B_LO, _B_HI

        # Stratified intervals for each dimension
        f_intervals    = [(f_lo + (f_hi - f_lo) * i / remaining,
                           f_lo + (f_hi - f_lo) * (i + 1) / remaining)
                          for i in range(remaining)]
        b_intervals    = [(b_lo + (b_hi - b_lo) * i / remaining,
                           b_lo + (b_hi - b_lo) * (i + 1) / remaining)
                          for i in range(remaining)]
        # Shuffle columns independently for LHS
        rng.shuffle(f_intervals)
        rng.shuffle(b_intervals)

        for i in range(remaining):
            if len(result) >= budget:
                break
            N    = n_vals_lhs[i % n_dim]
            f    = rng.uniform(*f_intervals[i])
            beta = rng.uniform(*b_intervals[i])
            cfg_i = _make(N, f, beta)
            if cfg_i is None:
                continue
            key   = _key(cfg_i)
            if key not in seen:
                seen.add(key)
                result.append(cfg_i)

        # If LHS collisions reduced count, fill with purely random samples
        max_attempts = budget * 3
        attempts = 0
        while len(result) < budget and attempts < max_attempts:
            N    = rng.choice(n_vals_lhs)
            f    = rng.uniform(f_lo, f_hi)
            beta = rng.uniform(b_lo, b_hi)
            cfg_i = _make(N, f, beta)
            if cfg_i is None:
                attempts += 1
                continue
            key   = _key(cfg_i)
            if key not in seen:
                seen.add(key)
                result.append(cfg_i)
            attempts += 1

    return result[:budget]


# ---------------------------------------------------------------------------
# Pareto frontier
# ---------------------------------------------------------------------------

def _is_dominated(p: CoOptCandidate, others: List[CoOptCandidate]) -> bool:
    """Return True if p is dominated by any member of others.

    Dominance is judged on (E_total, worst-case delay) where
    worst-case delay = max(total_delay_rr_ps, total_delay_ff_ps).
    """
    p_wc = max(p.total_delay_rr_ps, p.total_delay_ff_ps)
    for q in others:
        if q is p:
            continue
        q_wc = max(q.total_delay_rr_ps, q.total_delay_ff_ps)
        if (q.E_total_pJ_per_bit <= p.E_total_pJ_per_bit and
                q_wc <= p_wc and
                (q.E_total_pJ_per_bit < p.E_total_pJ_per_bit or
                 q_wc < p_wc)):
            return True
    return False


def _find_pareto_front(candidates: List[CoOptCandidate]) -> List[CoOptCandidate]:
    """Return the non-dominated subset."""
    front = []
    for p in candidates:
        if not _is_dominated(p, candidates):
            p.on_pareto = True
            front.append(p)
    return front


def _select_recommended_point(pareto: List[CoOptCandidate],
                               selection: str = "balanced") -> Optional[CoOptCandidate]:
    """
    Choose a recommended Pareto point.

    selection:
      'balanced'   — knee-style: min Euclidean distance to ideal (0, 0) in
                     normalised (energy, delay) space.
      'best_power' — point with lowest total energy per bit.
      'best_delay' — point with lowest worst-case delay.
    Deterministic tie-break in all modes: lower energy, then lower delay.
    """
    if not pareto:
        return None
    if len(pareto) == 1:
        return pareto[0]

    if selection == "best_power":
        return min(pareto, key=lambda p: (
            p.E_total_pJ_per_bit,
            max(p.total_delay_rr_ps, p.total_delay_ff_ps),
        ))

    if selection == "best_delay":
        return min(pareto, key=lambda p: (
            max(p.total_delay_rr_ps, p.total_delay_ff_ps),
            p.E_total_pJ_per_bit,
        ))

    # default: 'balanced' — knee-style Euclidean distance
    e_vals = [p.E_total_pJ_per_bit for p in pareto]
    d_vals = [max(p.total_delay_rr_ps, p.total_delay_ff_ps) for p in pareto]

    e_min, e_max = min(e_vals), max(e_vals)
    d_min, d_max = min(d_vals), max(d_vals)

    e_span = max(e_max - e_min, 1e-12)
    d_span = max(d_max - d_min, 1e-12)

    def _score(p: CoOptCandidate) -> Tuple[float, float, float]:
        d_wc = max(p.total_delay_rr_ps, p.total_delay_ff_ps)
        e_n = (p.E_total_pJ_per_bit - e_min) / e_span
        d_n = (d_wc - d_min) / d_span
        dist = math.sqrt(e_n * e_n + d_n * d_n)
        return (dist, p.E_total_pJ_per_bit, d_wc)

    return min(pareto, key=_score)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _save_csv(candidates: List[CoOptCandidate], path: str) -> None:
    fieldnames = [
        "on_pareto", "is_recommended", "pair_id", "sim_success", "slew_feasible", "latency_feasible",
        "E_total_pJ_per_bit", "total_delay_rr_ps", "total_delay_ff_ps",
        "E_tx_pJ_per_bit", "E_rx_pJ_per_bit", "E_term_pJ_per_bit",
        "tx_slew_ns", "slew_ui_frac",
        "tx_num_stages", "tx_stage_ratio", "tx_beta_ratio",
        "rx_preamp_scale", "rx_stage_ratio_rx", "rx_beta_rx",
        "rx_w_preamp_n_um", "rx_w_preamp_p_um", "rx_w_buf_n_um", "rx_w_buf_p_um",
        "rx_cap_in_pF",
    ]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for p in candidates:
            w.writerow({
                "on_pareto":           p.on_pareto,
                "is_recommended":      p.is_recommended,
                "pair_id":             p.pair_id,
                "sim_success":         p.sim_success,
                "slew_feasible":       p.slew_feasible,
                "latency_feasible":    p.latency_feasible,
                "E_total_pJ_per_bit":  round(p.E_total_pJ_per_bit, 6),
                "total_delay_rr_ps":   round(p.total_delay_rr_ps, 2),
                "total_delay_ff_ps":   round(p.total_delay_ff_ps, 2),
                "E_tx_pJ_per_bit":     round(p.E_tx_pJ_per_bit, 6),
                "E_rx_pJ_per_bit":     round(p.E_rx_pJ_per_bit, 6),
                "E_term_pJ_per_bit":   round(p.E_term_pJ_per_bit, 6),
                "tx_slew_ns":          round(p.tx_slew_ns, 6),
                "slew_ui_frac":        round(p.slew_ui_frac, 4),
                "tx_num_stages":       p.tx_num_stages,
                "tx_stage_ratio":      round(p.tx_stage_ratio, 4),
                "tx_beta_ratio":       round(p.tx_beta_ratio, 4),
                "rx_preamp_scale":     round(p.rx_preamp_scale, 4),
                "rx_stage_ratio_rx":   round(p.rx_stage_ratio_rx, 4),
                "rx_beta_rx":          round(p.rx_beta_rx, 4),
                "rx_w_preamp_n_um":    round(p.rx_w_preamp_n_um, 4),
                "rx_w_preamp_p_um":    round(p.rx_w_preamp_p_um, 4),
                "rx_w_buf_n_um":       round(p.rx_w_buf_n_um, 4),
                "rx_w_buf_p_um":       round(p.rx_w_buf_p_um, 4),
                "rx_cap_in_pF":        round(p.rx_cap_in_pF, 6),
            })


def _save_plot(feasible: List[CoOptCandidate],
               pareto:   List[CoOptCandidate],
               path:     str,
               recommended: Optional[CoOptCandidate] = None) -> None:
    """Save Pareto scatter plot.  Silently skips if matplotlib unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    all_eb = [p.E_total_pJ_per_bit for p in feasible]
    all_dl = [max(p.total_delay_rr_ps, p.total_delay_ff_ps) for p in feasible]
    par_eb = [p.E_total_pJ_per_bit for p in pareto]
    par_dl = [max(p.total_delay_rr_ps, p.total_delay_ff_ps) for p in pareto]

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.scatter(all_dl, all_eb, s=18, alpha=0.45, color="steelblue",
               label="All feasible")
    par_sorted = sorted(zip(par_dl, par_eb))
    ax.plot([d for d, _ in par_sorted], [e for _, e in par_sorted],
            "o-", color="tomato", linewidth=1.5, markersize=6,
            label="Pareto frontier")
    if recommended is not None:
        ax.scatter(
            [max(recommended.total_delay_rr_ps, recommended.total_delay_ff_ps)],
            [recommended.E_total_pJ_per_bit],
            s=180,
            marker="*",
            color="gold",
            edgecolors="black",
            linewidths=1.0,
            zorder=6,
            label="Recommended default",
        )
        ax.annotate(
            "Recommended",
            (max(recommended.total_delay_rr_ps, recommended.total_delay_ff_ps), recommended.E_total_pJ_per_bit),
            xytext=(10, 10),
            textcoords="offset points",
            fontsize=9,
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="black", alpha=0.85),
        )
    ax.set_xlabel("Total link delay — worst-case max(RR, FF) (ps)")
    ax.set_ylabel("Total energy/bit (pJ/bit)")
    ax.set_title("TX/RX Co-Optimisation: Energy/bit vs Delay Pareto Frontier")
    ax.legend(loc="upper right")
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Lookup-table helpers
# ---------------------------------------------------------------------------

def _interp1d(xs: List[float], ys: List[float], x_q: float) -> float:
    """1-D linear interpolation with clamping at boundary values."""
    if len(xs) == 1:
        return ys[0]
    if x_q <= xs[0]:
        return ys[0]
    if x_q >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x_q <= xs[i + 1]:
            t = (x_q - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[-1]


def _build_tx_lookup(lib_path: str) -> Optional[dict]:
    """
    Build TX lookup table from a .lib characterised with one input slew × K loads.

    Returns a dict with keys (all indexed by output-load):
        index_2          : list[float]  — load points (pF)
        delay_rr         : list[float]  — cell_rise[0][j] avg over arcs (ns)
        delay_ff         : list[float]  — cell_fall[0][j] avg over arcs (ns)
        slew_rise        : list[float]  — rise_transition[0][j] avg over arcs (ns)
        slew_fall        : list[float]  — fall_transition[0][j] avg over arcs (ns)
        power_rise       : list[float]  — rise_power_VDD[0][j] avg over arcs (pJ)
        power_fall       : list[float]  — fall_power_VDD[0][j] avg over arcs (pJ)
    Returns None if the .lib cannot be found or parsed.
    """
    import lib_parser as _lp
    if not os.path.exists(lib_path):
        return None
    try:
        timing = _lp.parse_lib_timing(lib_path)
        power  = _lp.parse_lib_power(lib_path, pg_pin="VDD")
    except Exception:
        return None

    def _avg_col(arcs, col):
        vals = []
        for arc in arcs:
            row0 = arc.get("values", [[]])[0] if arc.get("values") else []
            if col < len(row0):
                vals.append(row0[col])
        return sum(vals) / len(vals) if vals else 0.0

    # Determine number of load columns from the first arc
    n_loads = 0
    for arc in timing.get("cell_rise", []):
        if arc.get("values") and arc["values"][0]:
            n_loads = len(arc["values"][0])
            idx2 = arc["index_2"]
            break
    else:
        return None

    if n_loads == 0:
        return None

    result = {
        "index_2":    idx2,
        "delay_rr":   [_avg_col(timing["cell_rise"],        j) for j in range(n_loads)],
        "delay_ff":   [_avg_col(timing["cell_fall"],        j) for j in range(n_loads)],
        "slew_rise":  [_avg_col(timing["rise_transition"],  j) for j in range(n_loads)],
        "slew_fall":  [_avg_col(timing["fall_transition"],  j) for j in range(n_loads)],
        "power_rise": [_avg_col(power["rise_power"],        j) for j in range(n_loads)],
        "power_fall": [_avg_col(power["fall_power"],        j) for j in range(n_loads)],
    }
    return result


def _build_rx_lookup(lib_path: str) -> Optional[dict]:
    """
    Build RX lookup table from a .lib characterised with K input slews × loads.

    The RX metrics are taken at the **first** output-load column (col=0), matching
    the convention in get_metrics._parse_rx_lib_delay_min (min-slew / min-load).

    Returns a dict with keys (indexed by input-slew):
        index_1     : list[float]  — input slew points (ns)
        delay_rr    : list[float]  — cell_rise[i][0] avg over arcs (ns)
        delay_ff    : list[float]  — cell_fall[i][0] avg over arcs (ns)
        power_rise  : list[float]  — rise_power_VDD[i][0] avg over arcs (pJ)
        power_fall  : list[float]  — fall_power_VDD[i][0] avg over arcs (pJ)
        cap_in_pF   : float        — average capacitance of PAD input pins (pF)
    Returns None if the .lib cannot be found or parsed.
    """
    import lib_parser as _lp
    if not os.path.exists(lib_path):
        return None
    try:
        timing = _lp.parse_lib_timing(lib_path)
        power  = _lp.parse_lib_power(lib_path, pg_pin="VDD")
        caps   = _lp.parse_pin_capacitance(lib_path, pin_pattern=r'PAD_?\d*')
    except Exception:
        return None

    def _avg_row_col0(arcs):
        """Average arcs at column 0 across all rows — returns list[float] of len=n_slews."""
        n_rows = max((len(arc.get("values", [])) for arc in arcs), default=0)
        if n_rows == 0:
            return []
        result = []
        for i in range(n_rows):
            vals = []
            for arc in arcs:
                rows = arc.get("values", [])
                if i < len(rows) and rows[i]:
                    vals.append(rows[i][0])
            result.append(sum(vals) / len(vals) if vals else 0.0)
        return result

    # Get index_1 from first arc
    idx1 = []
    for arc in timing.get("cell_rise", []):
        if arc.get("index_1"):
            idx1 = arc["index_1"]
            break
    if not idx1:
        return None

    cap_vals = list(caps.values())
    cap_in_pF = sum(cap_vals) / len(cap_vals) if cap_vals else 0.0

    return {
        "index_1":    idx1,
        "delay_rr":   _avg_row_col0(timing["cell_rise"]),
        "delay_ff":   _avg_row_col0(timing["cell_fall"]),
        "power_rise": _avg_row_col0(power["rise_power"]),
        "power_fall": _avg_row_col0(power["fall_power"]),
        "cap_in_pF":  cap_in_pF,
    }


def _match_pair_from_tables(
    tx_lut: dict,
    rx_lut: dict,
    max_latency_ns: float = 0.0,
    alpha: float = 0.5,
    vdd: float = 1.0,
) -> Optional[dict]:
    """
    Compute metrics for one (TX, RX) pair via lookup-table interpolation.

    Steps
    -----
    1. Determine RX input cap from rx_lut.
    2. Interpolate TX tables at load = rx_cap → TX delay, output slew, power.
    3. Compute TX output slew = max(slew_rise, slew_fall).
    4. Interpolate RX tables at input_slew = TX output slew → RX delay, power.
    5. Accumulate E_tx, E_rx, E_total.

    Note on energy
    --------------
    The .lib ``internal_power (VDD)`` captures the **total** energy from the
    VDD rail per output transition, including both internal node switching and
    the charging of the external output load capacitance (through PMOS when
    the output rises).  This is the same quantity as the "Internal Switching
    Power @VDD" in the Liberate text datasheet used by get_metrics.

    Therefore the per-bit energy formula mirrors _compute_energy:
        E_tx = alpha * (rise_power + fall_power) / 2
    with no separate load-charging term (it is already embedded in rise_power).

    Returns dict with: tx_delay_rr_ns, tx_delay_ff_ns, tx_slew_ns, rx_cap_in_pF,
                       E_tx_pJ, E_rx_pJ, E_total_pJ, total_delay_rr_ps,
                       total_delay_ff_ps, latency_feasible.
    Returns None if interpolation fails.
    """
    rx_cap = rx_lut["cap_in_pF"]
    if rx_cap <= 0:
        return None

    idx2             = tx_lut["index_2"]
    tx_delay_rr      = _interp1d(idx2, tx_lut["delay_rr"],   rx_cap)
    tx_delay_ff      = _interp1d(idx2, tx_lut["delay_ff"],   rx_cap)
    tx_slew_rise     = _interp1d(idx2, tx_lut["slew_rise"],  rx_cap)
    tx_slew_fall     = _interp1d(idx2, tx_lut["slew_fall"],  rx_cap)
    tx_pwr_rise      = _interp1d(idx2, tx_lut["power_rise"], rx_cap)
    tx_pwr_fall      = _interp1d(idx2, tx_lut["power_fall"], rx_cap)

    # TX output slew driving the RX
    tx_output_slew   = max(tx_slew_rise, tx_slew_fall)

    idx1             = rx_lut["index_1"]
    rx_delay_rr      = _interp1d(idx1, rx_lut["delay_rr"],   tx_output_slew)
    rx_delay_ff      = _interp1d(idx1, rx_lut["delay_ff"],   tx_output_slew)
    rx_pwr_rise      = _interp1d(idx1, rx_lut["power_rise"], tx_output_slew)
    rx_pwr_fall      = _interp1d(idx1, rx_lut["power_fall"], tx_output_slew)

    E_tx  = alpha * (tx_pwr_rise + tx_pwr_fall) / 2.0
    E_rx  = alpha * (rx_pwr_rise + rx_pwr_fall) / 2.0

    # Re-attribute RX load-cap energy: mirrors _compute_energy in get_metrics.
    # TX Liberate uses rx_cap as external load; energy to charge that cap per
    # bit (alpha × C_rx × VDD² / 2) is dissipated inside the RX device.
    # Subtract it from E_tx and add to E_rx so E_total is unchanged.
    E_rx_cap = alpha * rx_cap * (vdd ** 2) / 2.0
    E_tx = max(0.0, E_tx - E_rx_cap)
    E_rx += E_rx_cap

    latency_feasible = True
    if max_latency_ns > 0:
        tx_rx_delay_rr = tx_delay_rr + rx_delay_rr
        tx_rx_delay_ff = tx_delay_ff + rx_delay_ff
        latency_feasible = max(tx_rx_delay_rr, tx_rx_delay_ff) <= max_latency_ns

    return {
        "tx_delay_rr_ns":   tx_delay_rr,
        "tx_delay_ff_ns":   tx_delay_ff,
        "tx_slew_ns":       tx_output_slew,
        "rx_cap_in_pF":     rx_cap,
        "E_tx_pJ":          E_tx,
        "E_rx_pJ":          E_rx,
        "E_total_pJ":       E_tx + E_rx,
        "total_delay_rr_ps": (tx_delay_rr + rx_delay_rr) * 1000.0,
        "total_delay_ff_ps": (tx_delay_ff + rx_delay_ff) * 1000.0,
        "latency_feasible": latency_feasible,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_co_opt(
    cfg,
    ch_result,
    eq_result,
    term_result,
    run_dir:             str,
    rise_fall_pct_ui:    float = 0.35,
    max_latency_ui:      float = 0.0,
    n_tx_configs:        int   = 8,
    n_rx_configs:        int   = 8,
    cap_in_pF_override:  Optional[float] = None,
    max_parallel:        int   = 4,
    n_tx_load_points:    int   = 6,
    n_rx_slew_points:    int   = 6,
    pareto_selection:    str   = "balanced",
) -> Optional[CoOptResult]:
    """
    Run the TX/RX co-optimisation Pareto search using a lookup-table approach.

    Lookup-Table Methodology  (O(N_tx + N_rx) Liberate/CharLib runs)
    ---------------------------------------------------------
    Each TX configuration is characterised once with a **sweep of output
    loads** (covering the full range of possible RX input capacitances).
    This yields a 1×K NLDM .lib table: delay, output slew, and internal
    switching power as functions of load.

    Each RX configuration is characterised once with a **sweep of input
    slews** (covering the full range of possible TX output slews).
    This yields a K×1 NLDM .lib table: delay and internal switching power
    as functions of input slew, plus the measured input capacitance.

    For each (TX_i, RX_j) pair the metrics are obtained by interpolation:
      1. Obtain RX_j input cap from its .lib.
      2. Interpolate TX_i tables at load = RX_j.cap → TX delay, slew, power.
      3. Interpolate RX_j tables at input_slew = TX_i output slew → RX metrics.
      4. Sum delays and energies.

    This reduces N_tx × N_rx Liberate/CharLib invocations to N_tx + N_rx — a
    factor-of-min(N_tx, N_rx) speed-up.

    Parameters
    ----------
    cfg                : Config from main.py
    ch_result          : ChannelResult from channel.get_channel
    eq_result          : EqualizationResult from equalization.get_equalization
    term_result        : TerminationResult from termination.get_termination
    run_dir            : Base output directory for this run
    rise_fall_pct_ui   : TX input slew as fraction of UI (default 0.35)
    n_tx_configs       : Number of TX configurations to explore (default 8)
    n_rx_configs       : Number of RX configurations to explore (default 8)
    cap_in_pF_override : Unit inverter cap override (skips Q/V sim)
    max_parallel       : Maximum concurrent Liberate/CharLib runs (default 4)
    n_tx_load_points   : Number of load-sweep points for TX characterisation (default 6)
    n_rx_slew_points   : Number of slew-sweep points for RX characterisation (default 6)

    Returns
    -------
    CoOptResult or None if all characterization runs failed.
    """
    import rx as rx_mod
    import tx as tx_mod
    from tx_sizing import TxSizingCandidate, TxSizingResult

    co_opt_dir = os.path.join(run_dir, "co_opt")
    os.makedirs(co_opt_dir, exist_ok=True)

    data_rate_Hz    = cfg.link.data_rate_Gbps * 1e9
    ui_ns           = 1.0 / data_rate_Hz * 1e9
    target_ns       = rise_fall_pct_ui * ui_ns
    ch_shunt_cap_pF = ch_result.total_shunt_C_fF / 1000.0
    tx_obj          = cfg.transistor
    w_n_min         = tx_obj.w_n_um
    w_p_min         = tx_obj.w_p_um
    l_um            = tx_obj.l_um
    w_max           = tx_obj.w_max_um
    vdd             = cfg.process.vdd
    alpha           = 0.5

    if max_latency_ui > 0:
        max_latency_ns = max_latency_ui * ui_ns
    else:
        auto_ui = 12.0 if cfg.link.data_rate_Gbps <= 16 else 16.0
        max_latency_ns = auto_ui * ui_ns

    cfg_explore = copy.deepcopy(cfg)
    tx_input_slew_ns = rise_fall_pct_ui * ui_ns
    cfg_explore.liberate.input_slews_ns = [tx_input_slew_ns]

    total_pairs = n_tx_configs * n_rx_configs

    print(f"\n  [Co-opt] UI = {ui_ns*1000:.2f} ps  |  "
          f"TX input slew = {tx_input_slew_ns*1000:.2f} ps  "
          f"({rise_fall_pct_ui*100:.0f}% UI)")
    print(f"  [Co-opt] TX+RX latency budget ≤ {max_latency_ns*1000:.2f} ps  "
          f"({max_latency_ns/ui_ns:.0f} UI, excl. channel)")
    print(f"  [Co-opt] TX configs: {n_tx_configs}  |  RX configs: {n_rx_configs}  "
          f"|  Total pairs: {total_pairs}")
    print(f"  [Co-opt] {'Liberate' if cfg.backend == 'liberate' else 'CharLib'} runs: {n_tx_configs + n_rx_configs}  "
          f"(lookup-table, {max_parallel} parallel)")

    # ------------------------------------------------------------------
    # Pre-measurement: unit inverter input capacitance
    # This must happen before Phase 1 so _gen_tx_configs uses an accurate
    # C_in for N_opt.  The analytical Cox formula underestimates FinFET
    # gate cap by ~20x, causing N_opt to be capped at _N_MAX and pushing
    # most hand-picked configs past the finger-count limit into LHS fill.
    # ------------------------------------------------------------------
    if cap_in_pF_override is None:
        _cap_meas_dir = os.path.join(co_opt_dir, "cap_meas")
        os.makedirs(_cap_meas_dir, exist_ok=True)
        if cfg.backend == "charlib":
            _measured = tx_mod._measure_inv_cap_ngspice(
                lib_path             = cfg.process.lib_path,
                lib_corner           = cfg.process.lib_corner,
                w_n_um               = tx_obj.w_n_um,
                w_p_um               = tx_obj.w_p_um,
                l_um                 = tx_obj.l_um,
                nf                   = tx_obj.nf,
                vdd                  = cfg.process.vdd,
                nmos_name            = tx_obj.nmos_name,
                pmos_name            = tx_obj.pmos_name,
                work_dir             = _cap_meas_dir,
                ngspice_exe          = cfg.charlib.ngspice_executable,
                temp                 = cfg.process.temp,
                spec                 = tx_mod.DeviceSpec.from_cfg(cfg),
                model_include_format = cfg.process.model_include_format,
            )
        else:
            _measured = tx_mod._measure_inv_cap_spice(
                lib_path             = cfg.process.lib_path,
                lib_corner           = cfg.process.lib_corner,
                w_n_um               = tx_obj.w_n_um,
                w_p_um               = tx_obj.w_p_um,
                l_um                 = tx_obj.l_um,
                nf                   = tx_obj.nf,
                vdd                  = cfg.process.vdd,
                nmos_name            = tx_obj.nmos_name,
                pmos_name            = tx_obj.pmos_name,
                work_dir             = _cap_meas_dir,
                temp                 = cfg.process.temp,
                spec                 = tx_mod.DeviceSpec.from_cfg(cfg),
                model_include_format = cfg.process.model_include_format,
            )
        if _measured is not None:
            cap_in_pF_override = _measured
            print(f"  [Co-opt] TX unit-inverter cap: {_measured*1000:.3f} fF  ({"SPICE" if cfg.backend == "liberate" else "ngspice"} measurement)")
        else:
            print(f"  [Co-opt] TX unit-inverter cap: measurement failed, using analytical fallback")

    # ------------------------------------------------------------------
    # Phase 1 — Independent configuration generation
    # ------------------------------------------------------------------
    print("\n  [Co-opt] Phase 1: Generating TX and RX configuration pools...")

    def _make_rx_cfg(ps, sr, beta_rx):
        wn1 = max(w_n_min, min(w_max, w_n_min * ps))
        wp1 = max(w_n_min, min(w_max, wn1 * beta_rx))
        wn2 = max(w_n_min, min(w_max, wn1 * sr))
        wp2 = max(w_n_min, min(w_max, wn2 * beta_rx))
        return (ps, sr, beta_rx, wn1, wp1, wn2, wp2)

    rx_cfgs = []
    rx_cfgs.append(_make_rx_cfg(1.0,
                                max(1.0, w_p_min / w_n_min),
                                max(1.0, w_p_min / w_n_min)))
    if n_rx_configs > 1:
        lhs_rx = _lhs(n_rx_configs - 1, 3, seed=42)
        for pt in lhs_rx:
            rx_cfgs.append(_make_rx_cfg(
                _scale(pt[0], _PS_LO, _PS_HI),
                _scale(pt[1], _SR_LO, _SR_HI),
                _scale(pt[2], _BR_LO, _BR_HI),
            ))

    # RX chiplet parasitics that appear as part of the TX output load when
    # the channel RC network is embedded in txip.scs.  The TX must drive:
    #   channel shunt caps (total_shunt_C_fF) + RX bump + RX chiplet pad + RX ESD
    # All three RX parasitics are assumed to match the channel model entries.
    use_channel_rc = getattr(cfg.liberate, 'tx_include_channel_rc', False)
    rx_fixed_cap_pF = 0.0
    if use_channel_rc:
        rx_fixed_cap_pF = (
            ch_result.bump_C_fF
            + ch_result.pad_chiplet_C_fF
            + ch_result.esd_C_fF
        ) / 1000.0

    # Analytical gate cap of each RX candidate (device input only)
    cap_gate_analytical = [_analytical_rx_cap_pF(s[3], s[4], l_um) for s in rx_cfgs]
    # Full RX PAD cap estimate = device gate + known RX chiplet parasitics
    cap_analytical_full = [c + rx_fixed_cap_pF for c in cap_gate_analytical]

    # Use a representative full cap (median) to anchor TX chain sizing so the
    # inverter is designed for the true driving load including RX parasitics.
    sorted_full = sorted(cap_analytical_full)
    rx_cap_sizing_pF = sorted_full[len(sorted_full) // 2]

    tx_configs = _gen_tx_configs(ch_shunt_cap_pF, tx_obj, n_tx_configs,
                                 rx_cap_in_pF=rx_cap_sizing_pF,
                                 tx_cap_in_pF=cap_in_pF_override)

    cap_src = f"measured {cap_in_pF_override*1000:.3f} fF" if cap_in_pF_override else "analytical"
    print(f"  [Co-opt]   RX pool: {len(rx_cfgs)} configs  "
          f"(gate cap: {min(cap_gate_analytical)*1000:.1f}–{max(cap_gate_analytical)*1000:.1f} fF"
          f", full PAD cap est: {min(cap_analytical_full)*1000:.1f}–{max(cap_analytical_full)*1000:.1f} fF)")
    print(f"  [Co-opt]   RX fixed parasitics: {rx_fixed_cap_pF*1000:.1f} fF "
          f"(bump+pad+ESD from ch_result)")
    print(f"  [Co-opt]   TX pool: {len(tx_configs)} configs  "
          f"(sized for {rx_cap_sizing_pF*1000:.1f} fF RX cap, TX C_in: {cap_src})")

    # ------------------------------------------------------------------
    # Phase 2 — Determine sweep ranges and run all Liberate/CharLib in parallel
    # ------------------------------------------------------------------

    # TX load sweep: the RX PAD cap has two components with different certainty:
    #
    #  1. rx_fixed_cap_pF (bump + chiplet pad + ESD) — precisely known from
    #     ch_result for this package/process configuration.  It does NOT vary
    #     across RX candidates and defines the hard floor of the load.
    #
    #  2. Device gate cap (cap_gate_analytical) — estimated analytically from
    #     (wn + wp) × L × Cox; varies across the RX sizing search space.
    #
    # Apply margin only to the uncertain gate-cap component:
    #   load_lo = rx_fixed_cap_pF + 0.5 × gate_min   (never < 0.5 × gate_min)
    #   load_hi = rx_fixed_cap_pF + 2.0 × gate_max
    gate_cap_lo = min(cap_gate_analytical)
    gate_cap_hi = max(cap_gate_analytical)
    # rx_fixed_cap_pF is the exact floor (bump+pad+ESD from ch_result), so
    # use it directly as load_lo with a 5 % safety margin for simulation
    # variation. Margin only applies to the uncertain gate-cap upper bound.
    load_lo = max(1e-4, 0.95 * rx_fixed_cap_pF)
    load_hi = rx_fixed_cap_pF + 2.0 * gate_cap_hi
    if n_tx_load_points == 1:
        tx_load_sweep = [(load_lo + load_hi) / 2.0]
    else:
        tx_load_sweep = [
            load_lo * ((load_hi / load_lo) ** (k / (n_tx_load_points - 1)))
            for k in range(n_tx_load_points)
        ]

    # RX input slew sweep: must bracket the actual TX PAD output slew.
    # When channel RC is embedded in txip.scs, the PAD edge is heavily
    # dominated by the channel R×C product — not just the TX input slew.
    # Use the Elmore delay as an estimate of the output edge timescale.
    # Without channel RC the TX output slew ≈ the driver speed, which is
    # within ~8× of the TX input slew.
    elmore_ns = ch_result.elmore_delay_ps / 1000.0 if use_channel_rc else 0.0
    slew_lo = max(0.002, 0.05 * max(tx_input_slew_ns, elmore_ns))
    slew_hi = max(8.0 * tx_input_slew_ns, 10.0 * elmore_ns, 4.0 * ui_ns)
    if n_rx_slew_points == 1:
        rx_slew_sweep = [max(tx_input_slew_ns, elmore_ns)]
    else:
        rx_slew_sweep = [
            slew_lo * ((slew_hi / slew_lo) ** (k / (n_rx_slew_points - 1)))
            for k in range(n_rx_slew_points)
        ]

    print(f"\n  [Co-opt] Phase 2: Running {'Liberate' if cfg.backend == 'liberate' else 'CharLib'} characterisations "
          f"({len(tx_configs)} TX + {len(rx_cfgs)} RX, {max_parallel} parallel)...")
    print(f"  [Co-opt]   TX load sweep  : {[f'{v*1000:.2f}' for v in tx_load_sweep]} fF")
    print(f"  [Co-opt]   RX slew sweep  : {[f'{v*1000:.1f}' for v in rx_slew_sweep]} ps")

    # ---- TX characterisation worker ----
    def _tx_worker(tx_idx):
        tx_cfg   = tx_configs[tx_idx]
        tx_dir   = os.path.join(co_opt_dir, f"tx_{tx_idx:03d}")
        os.makedirs(tx_dir, exist_ok=True)
        cfg_tx   = copy.deepcopy(cfg_explore)

        tx_chosen = TxSizingCandidate(
            num_stages     = tx_cfg.num_stages,
            beta_ratio     = tx_cfg.beta_ratio,
            stage_ratio    = tx_cfg.stage_ratio,
            inverter_sizes = tx_cfg.inverter_sizes,
            sim_success    = True,
        )
        tx_sizing_override = TxSizingResult(
            chosen         = tx_chosen,
            all_candidates = [tx_chosen],
            target_rise_ns = target_ns,
            target_fall_ns = target_ns,
            target_met     = True,
            message        = "",
        )
        try:
            tx_result = tx_mod.gen_netlist(
                cfg_tx, ch_result, eq_result, tx_dir,
                rx_cap_in_pF       = tx_load_sweep,
                tx_sizing_result   = tx_sizing_override,
                cap_in_pF_override = cap_in_pF_override,
                co_opt_mode        = True,
            )
            lib_path = os.path.join(tx_dir, "tx", "LIBRARY", "txip_nldm.lib")
            return tx_idx, lib_path, True
        except Exception as exc:
            print(f"    TX {tx_idx}: {'Liberate' if cfg.backend == 'liberate' else 'CharLib'} FAILED — {exc}")
            return tx_idx, None, False

    # ---- RX characterisation worker ----
    def _rx_worker(rx_idx):
        ps, sr, beta_rx, wn1, wp1, wn2, wp2 = rx_cfgs[rx_idx]
        rx_dir  = os.path.join(co_opt_dir, f"rx_{rx_idx:03d}")
        os.makedirs(rx_dir, exist_ok=True)
        cfg_rx  = copy.deepcopy(cfg_explore)
        cfg_rx.rx.w_preamp_n_um = wn1
        cfg_rx.rx.w_preamp_p_um = wp1
        cfg_rx.rx.w_buf_n_um    = wn2
        cfg_rx.rx.w_buf_p_um    = wp2
        if cfg_rx.liberate is not None:
            cfg_rx.liberate.input_slews_ns = rx_slew_sweep
        if cfg_rx.charlib is not None:
            cfg_rx.charlib.input_slews_ns = rx_slew_sweep
        try:
            rx_result = rx_mod.gen_netlist(
                cfg_rx, ch_result, term_result, rx_dir, tx_result=None,
                co_opt_mode=True,
            )
            lib_path = os.path.join(rx_dir, "rx", "LIBRARY", "rxip_nldm.lib")
            return rx_idx, lib_path, True
        except Exception as exc:
            print(f"    RX {rx_idx}: {'Liberate' if cfg.backend == 'liberate' else 'CharLib'} FAILED — {exc}")
            return rx_idx, None, False

    # Submit all TX and RX jobs in parallel
    tx_lib_paths: dict  = {}
    rx_lib_paths: dict  = {}
    n_tx_ok = n_rx_ok  = 0

    jobs = (
        [("TX", i) for i in range(len(tx_configs))] +
        [("RX", i) for i in range(len(rx_cfgs))]
    )

    def _dispatch(job):
        kind, idx = job
        if kind == "TX":
            return ("TX", *_tx_worker(idx))
        else:
            return ("RX", *_rx_worker(idx))

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {pool.submit(_dispatch, job): job for job in jobs}
        for future in as_completed(futures):
            kind, idx, lib_path, ok = future.result()
            if kind == "TX":
                tx_lib_paths[idx] = lib_path
                if ok:
                    n_tx_ok += 1
                    print(f"    TX {idx+1:>2}/{len(tx_configs)}  "
                          f"N={tx_configs[idx].num_stages}/"
                          f"f={tx_configs[idx].stage_ratio:.2f}/"
                          f"β={tx_configs[idx].beta_ratio:.2f}  ✓")
                else:
                    print(f"    TX {idx+1:>2}/{len(tx_configs)}  FAILED")
            else:
                rx_lib_paths[idx] = lib_path
                if ok:
                    n_rx_ok += 1
                    ps, sr, beta_rx = rx_cfgs[idx][:3]
                    print(f"    RX {idx+1:>2}/{len(rx_cfgs)}  "
                          f"ps={ps:.2f}/sr={sr:.2f}/β={beta_rx:.2f}  ✓")
                else:
                    print(f"    RX {idx+1:>2}/{len(rx_cfgs)}  FAILED")

    print(f"\n  [Co-opt]   TX ok: {n_tx_ok}/{len(tx_configs)}  "
          f"RX ok: {n_rx_ok}/{len(rx_cfgs)}")

    if n_tx_ok == 0 or n_rx_ok == 0:
        print(f"  [Co-opt] ERROR: No successful {'Liberate' if cfg.backend == 'liberate' else 'CharLib'} runs.  Aborting.")
        return None

    # ------------------------------------------------------------------
    # Phase 3 — Build lookup tables from .lib files
    # ------------------------------------------------------------------
    print("\n  [Co-opt] Phase 3: Building lookup tables from .lib files...")

    tx_luts: dict = {}
    for idx, lib_path in tx_lib_paths.items():
        if lib_path is None:
            continue
        lut = _build_tx_lookup(lib_path)
        if lut is not None:
            tx_luts[idx] = lut
        else:
            print(f"    TX {idx}: failed to build lookup table from {lib_path}")

    rx_luts: dict = {}
    for idx, lib_path in rx_lib_paths.items():
        if lib_path is None:
            continue
        lut = _build_rx_lookup(lib_path)
        if lut is not None:
            rx_luts[idx] = lut
            print(f"    RX {idx}: cap_in = {lut['cap_in_pF']*1000:.2f} fF  "
                  f"slew_range = [{lut['index_1'][0]*1000:.1f} .. "
                  f"{lut['index_1'][-1]*1000:.1f}] ps")
        else:
            print(f"    RX {idx}: failed to build lookup table from {lib_path}")

    if not tx_luts or not rx_luts:
        print("  [Co-opt] ERROR: No lookup tables built.  Aborting.")
        return None

    # ------------------------------------------------------------------
    # Phase 4 — Match all N_tx × N_rx pairs via interpolation
    # ------------------------------------------------------------------
    print(f"\n  [Co-opt] Phase 4: Matching "
          f"{len(tx_luts)} × {len(rx_luts)} pairs via interpolation...")

    all_candidates: List[CoOptCandidate] = []
    n_feasible = 0

    for rx_idx, (ps, sr, beta_rx, wn1, wp1, wn2, wp2) in enumerate(rx_cfgs):
        if rx_idx not in rx_luts:
            continue
        rx_lut = rx_luts[rx_idx]

        for tx_idx, tx_cfg in enumerate(tx_configs):
            if tx_idx not in tx_luts:
                continue
            tx_lut = tx_luts[tx_idx]

            pair_id = (f"tx{tx_idx:03d}"
                       f"_N{tx_cfg.num_stages}"
                       f"_f{tx_cfg.stage_ratio:.2f}"
                       f"_b{tx_cfg.beta_ratio:.2f}"
                       f"__rx{rx_idx:03d}")

            cand = CoOptCandidate(
                tx_num_stages     = tx_cfg.num_stages,
                tx_beta_ratio     = tx_cfg.beta_ratio,
                tx_stage_ratio    = tx_cfg.stage_ratio,
                tx_inverter_sizes = tx_cfg.inverter_sizes,
                rx_preamp_scale   = ps,
                rx_stage_ratio_rx = sr,
                rx_beta_rx        = beta_rx,
                rx_w_preamp_n_um  = wn1,
                rx_w_preamp_p_um  = wp1,
                rx_w_buf_n_um     = wn2,
                rx_w_buf_p_um     = wp2,
                pair_id           = pair_id,
            )

            m = _match_pair_from_tables(
                tx_lut, rx_lut,
                max_latency_ns=max_latency_ns,
                vdd=vdd,
            )
            if m is not None:
                cand.rx_cap_in_pF       = m["rx_cap_in_pF"]
                cand.tx_slew_ns         = m["tx_slew_ns"]
                cand.slew_ui_frac       = m["tx_slew_ns"] / ui_ns
                cand.slew_feasible      = True
                cand.latency_feasible   = m["latency_feasible"]
                cand.E_tx_pJ_per_bit    = m["E_tx_pJ"]
                cand.E_rx_pJ_per_bit    = m["E_rx_pJ"]
                cand.E_term_pJ_per_bit  = 0.0
                cand.E_total_pJ_per_bit = m["E_total_pJ"]
                cand.total_delay_rr_ps  = m["total_delay_rr_ps"]
                cand.total_delay_ff_ps  = m["total_delay_ff_ps"]
                cand.sim_success        = True
                n_feasible += 1

            all_candidates.append(cand)

    print(f"  [Co-opt]   Matched {len(all_candidates)} pairs, "
          f"{n_feasible} feasible")

    feasible = [c for c in all_candidates if c.sim_success and c.latency_feasible]
    n_latency_dropped = sum(
        1 for c in all_candidates if c.sim_success and not c.latency_feasible
    )
    if n_latency_dropped:
        print(f"  [Co-opt]   Dropped {n_latency_dropped} pair(s) exceeding "
              f"latency budget ({max_latency_ns*1000:.0f} ps).")
    if not feasible:
        print("  [Co-opt] ERROR: No pairs within latency budget.  "
              "No Pareto result produced.")
        return None

    # ------------------------------------------------------------------
    # Phase 5 — Pareto frontier
    # ------------------------------------------------------------------
    print("\n  [Co-opt] Phase 5: Computing Pareto frontier...")
    pareto = _find_pareto_front(feasible)
    print(f"  [Co-opt]   Pareto points: {len(pareto)}")
    recommended = _select_recommended_point(pareto, pareto_selection)
    if recommended is not None:
        recommended.is_recommended = True
        rec_wc = max(recommended.total_delay_rr_ps, recommended.total_delay_ff_ps)
        print(
            "  [Co-opt]   Recommended default: "
            f"{recommended.pair_id}  "
            f"(E/bit={recommended.E_total_pJ_per_bit:.4f} pJ, "
            f"Delay-WC={rec_wc:.1f} ps)"
        )

    # ------------------------------------------------------------------
    # Save results
    # ------------------------------------------------------------------
    csv_path  = os.path.join(co_opt_dir, "co_opt_results.csv")
    plot_path = os.path.join(co_opt_dir, "co_opt_pareto.png")
    _save_csv(all_candidates, csv_path)
    _save_plot(feasible, pareto, plot_path, recommended=recommended)
    print(f"  [Co-opt]   Results saved to {csv_path}")
    if os.path.exists(plot_path):
        print(f"  [Co-opt]   Pareto plot   saved to {plot_path}")

    return CoOptResult(
        pareto_front     = pareto,
        all_feasible     = feasible,
        all_candidates   = all_candidates,
        recommended_point = recommended,
        tx_input_slew_ns = tx_input_slew_ns,
        ui_ns            = ui_ns,
        max_latency_ns   = max_latency_ns,
        n_total          = len(all_candidates),
        n_feasible       = n_feasible,
        csv_path         = csv_path,
        plot_path        = plot_path if os.path.exists(plot_path) else None,
    )
