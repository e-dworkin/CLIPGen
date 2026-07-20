"""
main.py — entry point for the chiplet link generator.

Usage:
    python3 main.py                       # uses ./config.json
    python3 main.py config.json           # explicit run config
    python3 main.py config.json <pdk>     # override the selected PDK config
"""

import copy
import csv
import dataclasses
import datetime
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Path anchoring — makes the script runnable from any working directory.
#
# SCRIPTS_DIR  → .../chiplet_link_gen/scripts/
# REPO_ROOT    → .../chiplet_link_gen/          (parent of scripts/)
# ---------------------------------------------------------------------------
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.dirname(SCRIPTS_DIR)

# Ensure scripts/ is on sys.path so sibling modules (channel, termination, …)
# are importable regardless of the working directory.
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import channel
import termination
import equalization
import clocking

# ---------------------------------------------------------------------------
# Default config path (resolved relative to repo root, not CWD)
# The default entry is the general config.json at the repo root, which selects
# a PDK via its "pdk" pointer (configs/freepdk45.json by default).
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")

# Project layout:
#   <REPO_ROOT>/configs/   — configuration files
#   <REPO_ROOT>/scripts/   — this file and all other scripts
#   <REPO_ROOT>/results/   — all run outputs (auto-created)


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ProcessConfig:
    node: str
    lib_path: str
    lib_corner: str
    vdd: float
    temp: float
    lib_corner2: Optional[str] = None  # optional second .lib corner (e.g. ESD/passive models)
    # How the model-include (model.sp) is built for this PDK:
    #   "hspice_ptm"      — HSPICE BSIM .inc models under <lib_path>/models_<corner>/
    #                       plus parameterized wrapper subckts (FreePDK45 style).
    #   "spice_lib"       — .lib "<lib_path>" <corner>   (single SPICE library, default).
    #   "spectre_include" — Spectre 'include' of <lib_path> + allModels.scs alongside it.
    model_include_format: str = "spice_lib"


@dataclass
class TransistorConfig:
    nmos_name: str
    pmos_name: str
    w_n_um: float
    w_p_um: float
    l_um: float
    nf: int
    w_min_um: float
    w_max_um: float
    # Device-instantiation style (see scripts/device.py):
    #   "subckt_wl"   — x-prefix subckt sized by w/l (+ optional nf).
    #   "finfet_nfin" — x-prefix subckt sized by fin count (width -> nfin).
    style:          str   = "subckt_wl"
    include_nf:     bool  = True     # subckt_wl: emit "nf=<nf>" on each instance
    nfin_w_min_um:  float = 0.01     # finfet_nfin: single-fin device width
    nfin_w_step_um: float = 0.048    # finfet_nfin: width added per extra fin
    nfin_max:       int   = 20       # finfet_nfin: max fins per instance (split above)


@dataclass
class LinkConfig:
    pkg_type: str
    reach_mm: float
    bump_pitch_um: float
    data_rate_Gbps: float
    lane_count: int


@dataclass
class CouplingCapConfig:
    """Inter-lane coupling-capacitance knobs (user-facing)."""
    enabled:        bool  = True     # master enable; when False, all Cc caps collapse to 1e-30 F
    cc_ratio_trace: float = 0.4      # Cc / C_trace shunt at each Pi-ladder node
    cc_ratio_pad:   float = 0.2      # Cc / C_pad_ipos at each interposer-pad node
    cc_rx_pad_fF:   float = 1.0      # absolute on-die PAD-to-PAD coupling on RX (fF)


@dataclass
class ChannelConfig:
    """User-facing channel geometry. Tune to match your physical link."""
    bump_diameter_scale: float      # bump_diameter = scale * bump_pitch (used when bump_diameter_um is null)
    bump_diameter_um: Optional[float]
    bump_height_um: Optional[float]
    trace_width_um: Optional[float]
    esd_type: Optional[str]
    esd_mode: Optional[str] = 'auto'  # 'auto' (based on reach_mm) or explicit level: 'minimal', 'moderate', 'standard'
    pad_cap_mode: Optional[str] = 'physical'  # 'physical' (geometry model) or 'ucie' (spec table lookup)
    coupling_cap: Optional[CouplingCapConfig] = None


@dataclass
class TraceHiddenConfig:
    c_per_mm_fF: float
    r_per_mm_ohm: float
    default_width_um: float
    default_eps_r: float


@dataclass
class ChannelHiddenConfig:
    """Literature-sourced physical constants. Only change when updating the RC model."""
    eps_sio2: float
    eps_fr4: float
    eps_polyimide: float
    tox_chiplet_um: float
    tox_silicon_interposer_um: float
    tox_organic_substrate_um: float
    r_pad_ref_ohm: float
    r_pad_ref_width_um: float
    bump_resistivity_ohm_um: float
    bump_relative_permeability: float
    trace_silicon: TraceHiddenConfig
    trace_organic: TraceHiddenConfig
    esd_silicon_fine_pitch_fF: float
    esd_fine_pitch_threshold_um: float
    esd_silicon_interposer_fF: float
    esd_organic_fF: float
    esd_levels: list = None  # [[max_reach_mm, multiplier, label], ...] for graduated ESD protection
    ucie_pad_cap_table: list = None  # [[max_data_rate_GTs, cap_fF], ...] for 'ucie' pad_cap_mode


@dataclass
class EqualizationConfig:
    """User-facing equalization parameters."""
    enabled:           bool
    loss_threshold_dB: float = 3.0


@dataclass
class EqualizationHiddenConfig:
    """Equalizer sizing constants."""
    eq_cap_fraction: float      # fallback (used if eq_levels absent)
    eq_levels: list = None      # graduated levels: [(max_loss_mult, frac, label), ...]


@dataclass
class TerminationConfig:
    """User-facing termination parameters."""
    enabled: object = "auto"   # "auto" = decide by reach; True = always; False = never


@dataclass
class TerminationHiddenConfig:
    """Datasheet boundary table and circuit constants."""
    termination_improvement_factor: float
    unterminated_limits: dict   # {swing_str: [[rate, reach], ...]}
    ac_coupled:     bool  = False
    r_tx_ohm:       float = 50.0
    r_rx_ohm:       float = 50.0
    r_bias_hi_ohm:  float = 1e6
    r_bias_lo_ohm:  float = 1e6
    term_levels: list = None    # graduated levels: [(max_ratio, r_scale, c_scale, label), ...]


@dataclass
class LiberateConfig:
    template_dir: str
    slew_lower_rise: float
    slew_upper_rise: float
    slew_lower_fall: float
    slew_upper_fall: float
    input_slews_ns: list
    output_loads_pF: list
    # When True the channel RC ladder is embedded inside txip.scs so that
    # Liberate characterizes TX+channel in one shot.  get_metrics will then
    # skip the separate analytical E_ch term to avoid double-counting.
    tx_include_channel_rc: bool = False


@dataclass
class CharLibConfig:
    charlib_executable:   str   # absolute path to the charlib binary
    ngspice_executable:   str   # absolute path to the ngspice batch binary
    ngspice_library_path: str   # absolute path to libngspice.so (for charlib shared backend)
    input_slews_ns:       list
    output_loads_pF:      list
    tx_include_channel_rc: bool = False


@dataclass
class OutputConfig:
    base_dir: str
    save_netlists: bool
    save_liberate_decks: bool
    save_lib: bool
    save_metrics_csv: bool
    generate_verilog: bool
    generate_lef: bool


@dataclass
class RxConfig:
    w_preamp_n_um: float       # RX pre-amp NMOS width (um)
    w_preamp_p_um: float       # RX pre-amp PMOS width (um)
    w_buf_n_um: float          # RX output buffer NMOS width (um)
    w_buf_p_um: float          # RX output buffer PMOS width (um)
    input_slews_ns_override: Optional[list] = None  # Override auto-detected RX input slews
    rx_slew_source: str = "tx_pad"  # "tx_pad" = TX output at pad (tx_only), "channel" = after channel RC (tx)


@dataclass
class SweepConfig:
    enabled: bool
    pkg_type: list
    reach_mm: list
    bump_pitch_um: list
    data_rate_Gbps: list


@dataclass
class RxSizingConfig:
    """RX buffer sizing optimization parameters."""
    enabled: bool
    max_rx_delay_ui_fraction: float   # e.g. 0.1 → RX delay must be < 10% of UI


@dataclass
class TxSizingConfig:
    """TX inverter chain sizing parameters (adaptive search)."""
    enabled: bool
    rise_fall_pct_ui: float            # UCIe TX input rise/fall time as fraction of UI (measured 20%-80%)
    max_iterations: int = 20           # SPICE simulation budget for the adaptive search


@dataclass
class LayoutConfig:
    """Optional physical layout knobs (bump map)."""
    bump_map_enabled: bool = False
    bump_map_file:    Optional[str] = None  # path to .txt (absolute or relative to config dir)
    tx_signal_pairs:  Optional[list] = None  # List[area.SignalPair] — computed from bump map
    rx_signal_pairs:  Optional[list] = None  # List[area.SignalPair] — computed from bump map


@dataclass
class AreaHiddenConfig:
    """Density / margin constants for the area model. Override via the
    area_hidden section in the JSON config; all fields have safe node-class
    defaults in scripts/area.py when the section is omitted."""
    cap_mim_fF_per_um2:     Optional[float] = None
    res_poly_ohm_per_sq:    Optional[float] = None
    res_min_width_um:       Optional[float] = None
    res_bias_ohm_per_sq:    Optional[float] = None
    esd_diode_fF_per_um2:   Optional[float] = None
    pad_cap_fF_per_um2:     Optional[float] = None
    metal_spacing_um:       Optional[float] = None
    metal_track_um:         Optional[float] = None
    nwell_pwell_gap_um:     Optional[float] = None
    active_margin_frac:     Optional[float] = None
    pad_access_overhead_um: Optional[float] = None
    lane_width_frac:        Optional[float] = None  # None=auto (pad-driven); 0<frac≤1 sets lane_width=frac×P


@dataclass
class CoOptConfig:
    """TX/RX co-optimisation with Pareto frontier search (lookup-table)."""
    enabled: bool
    rise_fall_pct_ui:    float = 0.35       # TX input slew as fraction of UI (sets Liberate index_1)
    max_latency_ui:      float = 0.0        # TX+RX latency upper bound in UI (0 = auto from UCIe spec)
    n_tx_configs:        int   = 8          # number of TX configurations to characterise
    n_rx_configs:        int   = 8          # number of RX configurations to characterise
    n_tx_load_points:    int   = 6          # load-sweep points per TX Liberate run
    n_rx_slew_points:    int   = 6          # slew-sweep points per RX Liberate run
    max_parallel:        int   = 4          # max concurrent Liberate runs
    pareto_selection:    str   = "balanced" # which Pareto point to pick: 'balanced', 'best_power', 'best_delay'


@dataclass
class Config:
    process: ProcessConfig
    transistor: TransistorConfig
    link: LinkConfig
    channel: ChannelConfig
    channel_hidden: ChannelHiddenConfig
    equalization: EqualizationConfig
    equalization_hidden: EqualizationHiddenConfig
    termination: TerminationConfig
    termination_hidden: TerminationHiddenConfig
    rx: RxConfig
    output: OutputConfig
    sweep: SweepConfig
    backend: str                               # "liberate" or "charlib"; no default - must be explicit in config
    rx_sizing:   Optional[RxSizingConfig]   = None
    tx_sizing:   Optional[TxSizingConfig]   = None
    co_opt:      Optional[CoOptConfig]      = None
    layout:      Optional[LayoutConfig]     = None
    area_hidden: Optional[AreaHiddenConfig] = None
    clocking:        Optional[object] = None   # clocking.ClockingConfig
    clocking_hidden: Optional[object] = None   # clocking.ClockingHiddenConfig
    pdk_path:        Optional[str]    = None   # resolved PDK config the entry pointed to
    liberate:    Optional[LiberateConfig]  = None  # required when backend == "liberate"
    charlib:     Optional[CharLibConfig]   = None  # required when backend == "charlib"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _strip_comments(d):
    """Recursively remove keys starting with '_' (used as JSON comments)."""
    if isinstance(d, dict):
        return {k: _strip_comments(v) for k, v in d.items() if not k.startswith("_")}
    return d


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* onto *base* (override wins). Returns a new dict.

    A ``None`` value in *override* means "inherit": it does NOT clobber a non-None
    value already present in *base*. This lets the top-level config.json leave a
    key (e.g. process.lib_path) null to defer to a PDK config that ships its own.
    """
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        elif v is None and out.get(k) is not None:
            continue   # null in the higher layer = inherit the lower layer's value
        else:
            out[k] = v
    return out


# Path-valued keys are resolved relative to the file that DEFINES them (see
# _load_raw), so that e.g. a PDK config's "../templates/..." stays correct even
# when the entry point is the general root config.json.
_PATH_KEYS = (
    ("process",  "lib_path"),
    ("liberate", "template_dir"),
    ("output",   "base_dir"),
    ("charlib",  "charlib_executable"),
    ("charlib",  "ngspice_executable"),
    ("charlib",  "ngspice_library_path"),
)


def _load_raw(path: str, pdk_override: str = None, is_entry: bool = True) -> dict:
    """Load one config file and merge in its base layers (lowest precedence first).

    A config may reference base layers that it overrides:
      * ``"defaults"`` — the level-2 *_hidden defaults file.
      * ``"pdk"``      — a per-PDK config (process/transistor/co_opt/templates).
    Both are resolved relative to *this* file's directory (or absolute). The
    entry file's ``"pdk"`` may be overridden by *pdk_override* (a CLI path,
    resolved relative to the CWD). Precedence: defaults < pdk < this file.

    Path-valued keys (_PATH_KEYS) are made absolute relative to the directory of
    the file that defined them, so relative paths survive the merge regardless of
    which file is the entry point.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    cfg_dir = os.path.dirname(path)
    with open(path, "r") as f:
        raw = _strip_comments(json.load(f))

    # Resolve this file's own path-valued keys relative to this file's dir.
    for section, key in _PATH_KEYS:
        if section in raw and isinstance(raw[section], dict):
            p = raw[section].get(key)
            if p and not os.path.isabs(p):
                raw[section][key] = os.path.normpath(os.path.join(cfg_dir, p))

    # Collect base layers (lowest precedence first): defaults, then pdk.
    bases = []
    defaults_ref = raw.pop("defaults", None)
    if defaults_ref:
        dp = defaults_ref if os.path.isabs(defaults_ref) else os.path.join(cfg_dir, defaults_ref)
        bases.append(_load_raw(dp, is_entry=False))

    pdk_ref = raw.pop("pdk", None)
    if is_entry and pdk_override is not None:
        pp = os.path.abspath(pdk_override)          # CLI override: relative to CWD
    elif pdk_ref:
        pp = pdk_ref if os.path.isabs(pdk_ref) else os.path.join(cfg_dir, pdk_ref)
    else:
        pp = None
    if pp:
        bases.append(_load_raw(pp, is_entry=False))

    merged = {}
    for b in bases:
        merged = _deep_merge(merged, b)
    return _deep_merge(merged, raw)


def load_config(config_path: str, pdk_config: str = None) -> Config:
    # The entry config is the top-level config.json, which selects a PDK via its
    # "pdk" pointer. *pdk_config*, when given, overrides that pointer (used for the
    # cross-repo restricted overlay).
    config_path = os.path.abspath(config_path)
    config_dir  = os.path.dirname(config_path)

    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = _load_raw(config_path, pdk_override=pdk_config, is_entry=True)

    # A PDK config passed directly (instead of config.json) lacks the run sections.
    missing_run = [s for s in ("link", "channel", "equalization", "termination",
                               "output", "sweep") if s not in raw]
    if missing_run:
        raise ValueError(
            f"{config_path} is missing run sections {missing_run}. Pass the top-level "
            f"config.json (which holds the run config and selects a PDK via 'pdk'), "
            f"not a PDK config directly. To choose a PDK, edit config.json's 'pdk' "
            f"pointer or pass a PDK config as the 2nd argument.")

    if "process" not in raw or "transistor" not in raw:
        raise ValueError(
            f"No PDK selected for {config_path}: the config (or the file it points "
            f"to via 'pdk') must provide 'process' and 'transistor'. Set a 'pdk' "
            f"pointer in config.json, or pass a PDK config path as the 2nd argument.")

    def _resolve(path: str) -> str:
        """Resolve a path that may be relative (to the entry config dir) or absolute."""
        if path and not os.path.isabs(path):
            return os.path.normpath(os.path.join(config_dir, path))
        return path

    # Resolve the PDK config path so it can be archived alongside config.json in
    # the run dir. Mirrors the pdk pointer resolution in _load_raw: a CLI override
    # wins, otherwise the entry config's own "pdk" pointer (relative to its dir).
    if pdk_config is not None:
        pdk_path = os.path.abspath(pdk_config)
    else:
        with open(config_path, "r") as f:
            _pdk_ref = json.load(f).get("pdk")
        pdk_path = _resolve(_pdk_ref) if _pdk_ref else None

    hid_raw = raw["channel_hidden"]
    channel_hidden = ChannelHiddenConfig(
        eps_sio2                   = hid_raw["eps_sio2"],
        eps_fr4                    = hid_raw["eps_fr4"],
        eps_polyimide              = hid_raw["eps_polyimide"],
        tox_chiplet_um             = hid_raw["tox_chiplet_um"],
        tox_silicon_interposer_um  = hid_raw["tox_silicon_interposer_um"],
        tox_organic_substrate_um   = hid_raw["tox_organic_substrate_um"],
        r_pad_ref_ohm              = hid_raw["r_pad_ref_ohm"],
        r_pad_ref_width_um         = hid_raw["r_pad_ref_width_um"],
        bump_resistivity_ohm_um    = hid_raw["bump_resistivity_ohm_um"],
        bump_relative_permeability = hid_raw["bump_relative_permeability"],
        trace_silicon              = TraceHiddenConfig(**hid_raw["trace_silicon"]),
        trace_organic              = TraceHiddenConfig(**hid_raw["trace_organic"]),
        esd_silicon_fine_pitch_fF  = hid_raw["esd_silicon_fine_pitch_fF"],
        esd_fine_pitch_threshold_um= hid_raw["esd_fine_pitch_threshold_um"],
        esd_silicon_interposer_fF  = hid_raw["esd_silicon_interposer_fF"],
        esd_organic_fF             = hid_raw["esd_organic_fF"],
        esd_levels                 = [list(row) for row in hid_raw["esd_levels"]] if "esd_levels" in hid_raw else None,
        ucie_pad_cap_table         = [list(row) for row in hid_raw["ucie_pad_cap_table"]] if "ucie_pad_cap_table" in hid_raw else None,
    )

    th_raw = raw["termination_hidden"]
    # Pull fields that may have been in the old user-facing termination section
    _term_raw = raw.get("termination", {})
    term_hidden = TerminationHiddenConfig(
        termination_improvement_factor = th_raw["termination_improvement_factor"],
        unterminated_limits            = th_raw["unterminated_limits"],
        ac_coupled    = th_raw.get("ac_coupled",    _term_raw.get("ac_coupled",    False)),
        r_tx_ohm      = th_raw.get("r_tx_ohm",      _term_raw.get("r_tx_ohm",      50.0)),
        r_rx_ohm      = th_raw.get("r_rx_ohm",      _term_raw.get("r_rx_ohm",      50.0)),
        r_bias_hi_ohm = th_raw.get("r_bias_hi_ohm", _term_raw.get("r_bias_hi_ohm", 1e6)),
        r_bias_lo_ohm = th_raw.get("r_bias_lo_ohm", _term_raw.get("r_bias_lo_ohm", 1e6)),
        term_levels   = [tuple(lv) for lv in th_raw["term_levels"]] if "term_levels" in th_raw else None,
    )

    eq_h_raw = raw["equalization_hidden"]
    eq_hidden = EqualizationHiddenConfig(
        eq_cap_fraction = eq_h_raw["eq_cap_fraction"],
        eq_levels       = [tuple(lv) for lv in eq_h_raw["eq_levels"]] if "eq_levels" in eq_h_raw else None,
    )

    # RX sizing config — defaults for backward compatibility with configs
    # that may not have the "rx" section yet.
    rx_raw = raw.get("rx_hidden", raw.get("rx", {}))
    rx_cfg = RxConfig(
        w_preamp_n_um          = rx_raw.get("w_preamp_n_um", 1.0),
        w_preamp_p_um          = rx_raw.get("w_preamp_p_um", 3.0),
        w_buf_n_um             = rx_raw.get("w_buf_n_um", 3.0),
        w_buf_p_um             = rx_raw.get("w_buf_p_um", 9.0),
        input_slews_ns_override = rx_raw.get("input_slews_ns_override", None),
        rx_slew_source         = rx_raw.get("rx_slew_source", "tx_pad"),
    )

    # RX sizing optimization config (reads from rx_hidden)
    _rx_h = raw.get("rx_hidden", raw.get("rx", {}))
    _rx_s = raw.get("rx_sizing", {})
    rx_sizing_cfg = None
    if _rx_h.get("rx_sizing_enabled", _rx_s.get("enabled", False)):
        rx_sizing_cfg = RxSizingConfig(
            enabled                   = True,
            max_rx_delay_ui_fraction  = _rx_h.get("max_rx_delay_ui_fraction",
                                         _rx_s.get("max_rx_delay_ui_fraction", 0.1)),
        )

    # TX sizing optimization config (reads from tx_hidden)
    _tx_h = raw.get("tx_hidden", {})
    _tx_s = raw.get("tx_sizing", {})
    tx_sizing_cfg = None
    if _tx_h.get("tx_sizing_enabled", _tx_s.get("enabled", False)):
        tx_sizing_cfg = TxSizingConfig(
            enabled          = True,
            rise_fall_pct_ui = _tx_h.get("rise_fall_pct_ui", _tx_s.get("rise_fall_pct_ui", 0.35)),
            max_iterations   = _tx_h.get("max_iterations",   _tx_s.get("max_iterations",   20)),
        )

    # Co-optimisation config — optional
    co_opt_raw = raw.get("co_opt", None)
    co_opt_cfg = None
    if co_opt_raw and co_opt_raw.get("enabled", False):
        _raw_selection = co_opt_raw.get("pareto_selection", "balanced")
        if _raw_selection not in ("balanced", "best_power", "best_delay", "all"):
            raise ValueError(
                f"co_opt.pareto_selection must be 'balanced', 'best_power', "
                f"'best_delay', or 'all'; got {_raw_selection!r}"
            )
        co_opt_cfg = CoOptConfig(
            enabled            = True,
            rise_fall_pct_ui   = co_opt_raw.get("rise_fall_pct_ui", 0.35),
            max_latency_ui     = 0.0 if co_opt_raw.get("max_latency_ui", "auto") == "auto"
                                 else float(co_opt_raw["max_latency_ui"]),
            n_tx_configs       = co_opt_raw.get("n_tx_configs",
                                 co_opt_raw.get("budget_per_rx", 8)),
            n_rx_configs       = co_opt_raw.get("n_rx_configs",
                                 co_opt_raw.get("n_rx_seeds", 8)),
            n_tx_load_points   = co_opt_raw.get("n_tx_load_points", 6),
            n_rx_slew_points   = co_opt_raw.get("n_rx_slew_points", 6),
            max_parallel       = co_opt_raw.get("max_parallel", 4),
            pareto_selection   = _raw_selection,
        )

    # Extract optional nested coupling_cap block from channel.* so the flat
    # ChannelConfig(**raw["channel"]) call still works.
    # Override fields that were moved to channel_hidden are merged in here
    # with null defaults so ChannelConfig receives all required arguments.
    _ch_hid = raw.get("channel_hidden", {})
    channel_raw = {
        "bump_diameter_um": _ch_hid.get("bump_diameter_um", None),
        "bump_height_um":   _ch_hid.get("bump_height_um",   None),
        "trace_width_um":   _ch_hid.get("trace_width_um",   None),
        "esd_type":         _ch_hid.get("esd_type",         None),
        **raw["channel"],
    }
    cc_raw = channel_raw.pop("coupling_cap", None)
    coupling_cap_cfg = None
    if cc_raw is not None:
        coupling_cap_cfg = CouplingCapConfig(
            enabled        = bool(cc_raw.get("enabled", True)),
            cc_ratio_trace = float(cc_raw.get("cc_ratio_trace", 0.4)),
            cc_ratio_pad   = float(cc_raw.get("cc_ratio_pad",   0.2)),
            cc_rx_pad_fF   = float(cc_raw.get("cc_rx_pad_fF",   1.0)),
        )
    channel_cfg = ChannelConfig(**channel_raw)
    channel_cfg.coupling_cap = coupling_cap_cfg

    # Layout (bump map) — optional
    layout_raw = raw.get("layout", None)
    layout_cfg = None
    if layout_raw is not None:
        bump_map_file = layout_raw.get("bump_map_file", None)
        if bump_map_file:
            resolved = _resolve(bump_map_file)
            # Bump maps are UCIe-standard framework data, shipped once with the
            # public repo. Resolve relative to the config dir first (so a PDK
            # package may ship a custom map next to its config); otherwise fall
            # back to the framework's shared maps under <REPO_ROOT>/configs/.
            if not os.path.isfile(resolved) and not os.path.isabs(bump_map_file):
                fallback = os.path.normpath(os.path.join(REPO_ROOT, "configs", bump_map_file))
                if os.path.isfile(fallback):
                    resolved = fallback
            bump_map_file = resolved
        bm_enabled = bool(layout_raw.get("bump_map_enabled", False))
        tx_pairs = None
        rx_pairs = None
        if bm_enabled and bump_map_file:
            import area as area_mod
            lane_count = int(raw["link"]["lane_count"])
            bm = area_mod.load_bump_map(bump_map_file, lane_count)
            tx_pairs = area_mod.get_signal_pairs(bm, "tx")
            rx_pairs = area_mod.get_signal_pairs(bm, "rx")
        layout_cfg = LayoutConfig(
            bump_map_enabled = bm_enabled,
            bump_map_file    = bump_map_file,
            tx_signal_pairs  = tx_pairs,
            rx_signal_pairs  = rx_pairs,
        )

    # Area hidden density constants — optional (defaults live in area.py)
    ah_raw = raw.get("area_hidden", None)
    area_hidden_cfg = None
    if ah_raw is not None:
        area_hidden_cfg = AreaHiddenConfig(
            cap_mim_fF_per_um2     = ah_raw.get("cap_mim_fF_per_um2"),
            res_poly_ohm_per_sq    = ah_raw.get("res_poly_ohm_per_sq"),
            res_min_width_um       = ah_raw.get("res_min_width_um"),
            res_bias_ohm_per_sq    = ah_raw.get("res_bias_ohm_per_sq"),
            esd_diode_fF_per_um2   = ah_raw.get("esd_diode_fF_per_um2"),
            pad_cap_fF_per_um2     = ah_raw.get("pad_cap_fF_per_um2"),
            metal_spacing_um       = ah_raw.get("metal_spacing_um"),
            metal_track_um         = ah_raw.get("metal_track_um"),
            nwell_pwell_gap_um     = ah_raw.get("nwell_pwell_gap_um"),
            active_margin_frac     = ah_raw.get("active_margin_frac"),
            pad_access_overhead_um = ah_raw.get("pad_access_overhead_um"),
            lane_width_frac        = ah_raw.get("lane_width_frac"),
        )

    # Backend selection - required; no default
    backend = raw.get("backend")
    if backend is None:
        raise ValueError(
            f"{config_path} must specify \"backend\": set "
            "\"backend\": \"liberate\" or \"backend\": \"charlib\" in config.json.")

    # Liberate config - required when backend == "liberate"; may be absent/partial otherwise
    _liberate_required = {"template_dir", "slew_lower_rise", "slew_upper_rise",
                          "slew_lower_fall", "slew_upper_fall", "input_slews_ns", "output_loads_pF"}
    _liberate_raw = raw.get("liberate", {})
    if _liberate_required.issubset(_liberate_raw.keys()):
        liberate_cfg = LiberateConfig(**_liberate_raw)
    else:
        liberate_cfg = None

    # CharLib config - required when backend == "charlib"; absent otherwise
    _charlib_raw = raw.get("charlib")
    charlib_cfg = CharLibConfig(**_charlib_raw) if _charlib_raw is not None else None

    # Clocking — optional (defaults live in scripts/clocking.py)
    clocking_cfg        = clocking.load_clocking_config(raw.get("clocking"))
    clocking_hidden_cfg = clocking.load_clocking_hidden(raw.get("clocking_hidden"))

    return Config(
        process             = ProcessConfig(**raw["process"]),
        transistor          = TransistorConfig(
                                **{**raw["transistor"],
                                   **{k: v for k, v in raw.get("tx_hidden", {}).items()
                                      if k in ("w_n_um", "w_p_um")}}
                            ),
        link                = LinkConfig(**raw["link"]),
        channel             = channel_cfg,
        channel_hidden      = channel_hidden,
        equalization        = EqualizationConfig(
                                enabled           = raw["equalization"].get("enabled",
                                                    raw["equalization"].get("passive_eq_enabled", True)),
                                loss_threshold_dB = raw.get("equalization_hidden", {}).get("loss_threshold_dB",
                                                    raw["equalization"].get("loss_threshold_dB", 3.0)),
                            ),
        equalization_hidden = eq_hidden,
        termination         = TerminationConfig(
                                enabled = raw.get("termination", {}).get("enabled", "auto"),
                            ),
        termination_hidden  = term_hidden,
        rx                  = rx_cfg,
        output              = OutputConfig(**raw["output"]),
        sweep               = SweepConfig(**raw["sweep"]),
        backend             = backend,
        rx_sizing           = rx_sizing_cfg,
        tx_sizing           = tx_sizing_cfg,
        co_opt              = co_opt_cfg,
        layout              = layout_cfg,
        area_hidden         = area_hidden_cfg,
        clocking            = clocking_cfg,
        clocking_hidden     = clocking_hidden_cfg,
        pdk_path            = pdk_path,
        liberate            = liberate_cfg,
        charlib             = charlib_cfg,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_config(cfg: Config) -> None:
    errors = []

    if cfg.process.model_include_format == "hspice_ptm":
        # hspice_ptm: lib_path is a directory; the HSPICE BSIM .inc models live
        # in a models_<corner>/ subdir (e.g. FreePDK45 nom/ff/ss).
        _corner = cfg.process.lib_corner or "nom"
        _models_dir = os.path.join(cfg.process.lib_path, f"models_{_corner}")
        if not os.path.isdir(_models_dir):
            errors.append(f"[process] model models dir not found: {_models_dir}")
    elif not os.path.isfile(cfg.process.lib_path):
        errors.append(f"[process] lib_path not found: {cfg.process.lib_path}")
    if cfg.process.vdd <= 0:
        errors.append(f"[process] vdd must be > 0, got {cfg.process.vdd}")

    if cfg.transistor.w_n_um <= 0 or cfg.transistor.w_p_um <= 0:
        errors.append("[transistor] w_n_um and w_p_um must be > 0")
    if cfg.transistor.l_um <= 0:
        errors.append("[transistor] l_um must be > 0")
    if cfg.transistor.w_min_um >= cfg.transistor.w_max_um:
        errors.append("[transistor] w_min_um must be < w_max_um")
    if cfg.transistor.w_n_um < cfg.transistor.w_min_um:
        errors.append(f"[transistor] w_n_um ({cfg.transistor.w_n_um}) is below PDK minimum w_min_um ({cfg.transistor.w_min_um})")
    if cfg.transistor.w_p_um < cfg.transistor.w_min_um:
        errors.append(f"[transistor] w_p_um ({cfg.transistor.w_p_um}) is below PDK minimum w_min_um ({cfg.transistor.w_min_um})")
    if cfg.transistor.w_n_um > cfg.transistor.w_max_um:
        errors.append(f"[transistor] w_n_um ({cfg.transistor.w_n_um}) exceeds w_max_um ({cfg.transistor.w_max_um}); use parallel instances or reduce width")
    if cfg.transistor.w_p_um > cfg.transistor.w_max_um:
        errors.append(f"[transistor] w_p_um ({cfg.transistor.w_p_um}) exceeds w_max_um ({cfg.transistor.w_max_um}); use parallel instances or reduce width")

    if cfg.link.pkg_type not in ("silicon", "organic"):
        errors.append(f"[link] pkg_type must be 'silicon' or 'organic', got '{cfg.link.pkg_type}'")
    if not (1 <= cfg.link.reach_mm <= 80):
        errors.append(f"[link] reach_mm {cfg.link.reach_mm} outside supported range [1, 80] mm")
    if not (1 <= cfg.link.bump_pitch_um <= 300):
        errors.append(f"[link] bump_pitch_um {cfg.link.bump_pitch_um} outside supported range [1, 300] um")
    if not (1 <= cfg.link.data_rate_Gbps <= 112):
        errors.append(f"[link] data_rate_Gbps {cfg.link.data_rate_Gbps} outside supported range [1, 112] Gb/s")
    if not (1 <= cfg.link.lane_count <= 256):
        errors.append(f"[link] lane_count {cfg.link.lane_count} outside supported range [1, 256]")

    if cfg.channel.esd_type is not None and cfg.channel.esd_type not in ("silicon", "organic"):
        errors.append(f"[channel] esd_type must be 'silicon', 'organic', or null, got '{cfg.channel.esd_type}'")
    if not (0.0 < cfg.channel.bump_diameter_scale <= 1.0):
        errors.append(f"[channel] bump_diameter_scale must be in (0, 1], got {cfg.channel.bump_diameter_scale}")

    if cfg.sweep.enabled:
        for p in cfg.sweep.pkg_type:
            if p not in ("silicon", "organic"):
                errors.append(f"[sweep] unknown pkg_type '{p}'")
        if not cfg.sweep.reach_mm:
            errors.append("[sweep] reach_mm list is empty")
        if not cfg.sweep.bump_pitch_um:
            errors.append("[sweep] bump_pitch_um list is empty")
        if not cfg.sweep.data_rate_Gbps:
            errors.append("[sweep] data_rate_Gbps list is empty")

    if cfg.backend == "liberate":
        if cfg.liberate is None:
            errors.append("[liberate] backend selected but 'liberate' section is missing or incomplete in config")
        elif not os.path.isdir(cfg.liberate.template_dir):
            errors.append(f"[liberate] template_dir not found: {cfg.liberate.template_dir}")
    elif cfg.backend == "charlib":
        if cfg.charlib is None:
            errors.append("[charlib] backend selected but 'charlib' section is missing or incomplete in config")
        else:
            if not os.path.isfile(cfg.charlib.charlib_executable):
                errors.append(f"[charlib] charlib_executable not found: {cfg.charlib.charlib_executable}")
            if not os.path.isfile(cfg.charlib.ngspice_library_path):
                errors.append(f"[charlib] ngspice_library_path not found: {cfg.charlib.ngspice_library_path}")
    else:
        errors.append(f"[backend] must be 'liberate' or 'charlib', got {cfg.backend!r}")

    if errors:
        raise ValueError("Configuration validation failed:\n"
                         + "\n".join(f"  - {e}" for e in errors))



# ---------------------------------------------------------------------------
# Run directory setup
# ---------------------------------------------------------------------------

def create_run_dir(cfg: Config, config_path: str) -> str:
    """
    Create the output directory for this run and copy the config file into it.

    Directory structure:
        Single link : <base_dir>/<timestamp>_<pkg>_<reach>mm_<pitch>um_<rate>Gbps/
        Sweep       : <base_dir>/<timestamp>_sweep/

    The config file used for this run is copied in as 'config.json', and the PDK
    config it referenced is copied alongside under its own name, so every result
    directory is fully self-contained and reproducible.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    pdk = cfg.process.node  # e.g. "freepdk45"

    if cfg.sweep.enabled:
        run_name = f"{timestamp}_{pdk}_sweep"
    else:
        lk = cfg.link
        run_name = f"{timestamp}_{pdk}_{lk.pkg_type}_{lk.reach_mm}mm_{lk.bump_pitch_um}um_{lk.data_rate_Gbps}Gbps"

    run_dir = os.path.join(cfg.output.base_dir, run_name)
    os.makedirs(run_dir, exist_ok=True)

    # Copy config into the run directory for reproducibility
    shutil.copy2(config_path, os.path.join(run_dir, "config.json"))

    # Also archive the PDK config it referenced (e.g. tsmc16.json); config.json's
    # "pdk" pointer is relative and won't resolve from inside the run dir.
    if cfg.pdk_path and os.path.isfile(cfg.pdk_path):
        shutil.copy2(cfg.pdk_path, os.path.join(run_dir, os.path.basename(cfg.pdk_path)))

    return run_dir


# ---------------------------------------------------------------------------
# Summary printing
# ---------------------------------------------------------------------------

def print_config_summary(cfg: Config, run_dir: str) -> None:
    sep = "-" * 60
    print(sep)
    print(f"  Chiplet Link Generator — {cfg.process.node.upper()}")
    print(sep)
    if cfg.sweep.enabled:
        n = (len(cfg.sweep.pkg_type) * len(cfg.sweep.reach_mm)
             * len(cfg.sweep.bump_pitch_um) * len(cfg.sweep.data_rate_Gbps))
        print(f"  Mode        : SWEEP ({n} combinations)")
    else:
        lk = cfg.link
        print(f"  Mode        : SINGLE LINK")
        print(f"  Link        : {lk.pkg_type}  {lk.reach_mm} mm  "
              f"{lk.bump_pitch_um} um pitch  {lk.data_rate_Gbps} Gb/s  "
              f"{lk.lane_count} lanes")
    print(f"  Output dir  : {run_dir}")
    print(f"  Process     : {cfg.process.node}  VDD={cfg.process.vdd}V  "
          f"corner={cfg.process.lib_corner}  T={cfg.process.temp}°C")
    print(f"  Termination : {'AC-coupled' if cfg.termination_hidden.ac_coupled else 'DC'}  "
          f"swing={cfg.process.vdd}V  "
          f"R_TX={cfg.termination_hidden.r_tx_ohm}Ω  R_RX={cfg.termination_hidden.r_rx_ohm}Ω  "
          f"(graduated levels)")
    print(f"  Equalization: {'enabled' if cfg.equalization.enabled else 'disabled'}  "
          f"threshold={cfg.equalization.loss_threshold_dB} dB  "
          f"(graduated levels)")
    print(sep)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_N_STAGES = 6
_BANNER_WIDTH = 64


def _stage(n: int, title: str) -> None:
    """Print a consistent stage header."""
    tag   = f"[{n}/{_N_STAGES}]"
    fill  = "─" * (_BANNER_WIDTH - len(tag) - len(title) - 3)
    print(f"\n{tag}  {title}  {fill}")


def _generate_point_collateral(cfg, ch_result, eq_result, term_result,
                                point_dir, tx_sizing_override=None,
                                cap_in_pF_override=None):
    """Run the full TX → RX → Metrics pipeline for one design point.

    Steps:
      1. Initial RX Liberate  (tx_result=None) → PAD input cap
      2. TX Liberate           (full lane count, with optional sizing override)
      3. RX re-characterisation (with real TX output slew)
      4. get_metrics.extract()

    Returns (tx_result, rx_result, metrics).
    """
    import tx as tx_mod
    import rx as rx_mod
    import get_metrics

    # 1. Initial RX → PAD cap
    print("  Running initial RX characterisation → PAD input cap...")
    rx_init = rx_mod.gen_netlist(cfg, ch_result, term_result, point_dir,
                                tx_result=None)
    rx_cap = rx_init.cap_in_pF
    print(f"  RX PAD input cap = {rx_cap:.5f} pF")

    # 2. TX Liberate (full lane count).  Termination lives on the RX side
    #    (rxip.scs), so the TX run is NOT terminated.
    print("  Running TX characterisation...")
    tx_result = tx_mod.gen_netlist(cfg, ch_result, eq_result, point_dir,
                                  rx_cap_in_pF=rx_cap,
                                  tx_sizing_result=tx_sizing_override,
                                  cap_in_pF_override=cap_in_pF_override)
    print(tx_result.report())

    # 2b. TX-only Liberate run (tier 1: TX+EQ, no channel) for UCIe latency
    print("  Running TX-only (no channel) characterisation...")
    tx_result = tx_mod.gen_tx_only_run(cfg, ch_result, eq_result, tx_result,
                                       point_dir, rx_cap_in_pF=rx_cap)

    # 2c. TX+channel Liberate run (tier 2: channel_rc=1, rx_pad_bump_rc=0)
    #     Enables the 3-tier energy breakdown: E_tx, E_channel, E_rxpad+esd.
    print("  Running TX+channel (no RX bump/pad) characterisation...")
    tx_result = tx_mod.gen_tx_channel_run(cfg, ch_result, eq_result, tx_result,
                                          point_dir, rx_cap_in_pF=rx_cap)

    # 3. RX re-characterisation with real TX output slew
    print("  Running RX re-characterisation with TX slew...")
    rx_result = rx_mod.gen_netlist(cfg, ch_result, term_result, point_dir,
                                  tx_result=tx_result)
    print(rx_result.report())

    # 4. Metrics extraction
    metrics = get_metrics.extract(cfg, ch_result, eq_result, term_result,
                                  tx_result, point_dir)
    print(metrics.report())

    return tx_result, rx_result, metrics


def run_single(cfg: Config, run_dir: str) -> None:
    """Run the full characterisation pipeline for a single link configuration.

    Pipeline (6 stages):
      1-3  Channel RC / termination / equalization  (unchanged)
      4    TX/RX sizing exploration (Pareto) — single-lane, parallel
      5    Full collateral generation for each Pareto point (single-lane Liberate, full-lane Verilog/LEF)
      6    Summary
    """

    # ------------------------------------------------------------------
    # Stage 1: Channel RC
    # ------------------------------------------------------------------
    _stage(1, "Channel RC model")
    ch_result = channel.get_channel(cfg)
    print(ch_result.report())

    # ------------------------------------------------------------------
    # Stage 2: Termination decision
    # ------------------------------------------------------------------
    _stage(2, "Termination")
    term_result = termination.get_termination(cfg, ch_result)
    print(term_result.report())

    # ------------------------------------------------------------------
    # Stage 3: Equalization decision
    # ------------------------------------------------------------------
    _stage(3, "Equalization")
    eq_result = equalization.get_equalization(cfg, ch_result, term_result)
    print(eq_result.report())

    # ------------------------------------------------------------------
    # Clocking-circuit characterization (once per run): generate + run the
    # serializer / deserializer / DCC / DLL / PI Spectre decks for the
    # configured PDK and use the MEASURED energies as clocking_hidden,
    # overriding the config estimates.  Per-circuit fallback to the config
    # value if a sim fails.  Design-point-independent, so done once here.
    # ------------------------------------------------------------------
    if getattr(cfg, "clocking", None) is not None and cfg.clocking.enabled:
        import clocking
        _clk_sim = "ngspice" if cfg.backend == "charlib" else "Spectre"
        print(f"\n[clocking] Characterising clock circuits via {_clk_sim} "
              "(serializer, deserializer, DCC, DLL, PI)...")
        char_dir = os.path.join(run_dir, "clocking_char")
        hid_meas, char_lines = clocking.characterize(cfg, char_dir)
        cfg.clocking_hidden = hid_meas
        print("\n".join(char_lines))

    # ------------------------------------------------------------------
    # Stage 4: TX/RX Sizing Exploration (Pareto)
    #
    # Single-lane Liberate runs in parallel to explore the TX/RX sizing
    # space.  TX input slew = rise_fall_pct_ui × UI.  Produces the
    # Pareto frontier of (energy/bit, delay).
    # ------------------------------------------------------------------
    _stage(4, "TX/RX sizing exploration (Pareto)")
    co_result = None
    if cfg.co_opt is not None and cfg.co_opt.enabled:
        import co_opt_pareto
        co_result = co_opt_pareto.run_co_opt(
            cfg                = cfg,
            ch_result          = ch_result,
            eq_result          = eq_result,
            term_result        = term_result,
            run_dir            = run_dir,
            rise_fall_pct_ui   = cfg.co_opt.rise_fall_pct_ui,
            max_latency_ui     = cfg.co_opt.max_latency_ui,
            n_tx_configs       = cfg.co_opt.n_tx_configs,
            n_rx_configs       = cfg.co_opt.n_rx_configs,
            n_tx_load_points   = cfg.co_opt.n_tx_load_points,
            n_rx_slew_points   = cfg.co_opt.n_rx_slew_points,
            max_parallel       = cfg.co_opt.max_parallel,
            pareto_selection   = cfg.co_opt.pareto_selection,
        )
        if co_result is not None:
            print(co_result.report())
        else:
            print("  WARNING: Sizing exploration failed — "
                  "falling back to default config sizing.")
    else:
        print("  TX/RX co-optimisation: DISABLED — "
              "using default config sizing")

    # ------------------------------------------------------------------
    # Stage 5: Full Collateral Generation
    #
    # For each Pareto-optimal design point (or the single default), run
    # the complete N-lane pipeline: initial RX → TX Liberate → RX re-char
    # → metrics → Verilog/LEF → link_library.
    # ------------------------------------------------------------------
    _stage(5, "Full collateral generation")

    if co_result is not None and co_result.pareto_front:
        from tx_sizing import TxSizingCandidate, TxSizingResult
        design_points = sorted(co_result.pareto_front,
                               key=lambda p: max(p.total_delay_rr_ps, p.total_delay_ff_ps))
        print(f"  Generating full {cfg.link.lane_count}-lane collateral for "
              f"{len(design_points)} Pareto-optimal design point(s)...\n")

        pareto_metrics = []   # collects LinkMetrics for each point (full collateral)
        n_points = len(design_points)
        max_parallel = cfg.co_opt.max_parallel if cfg.co_opt else 4

        def _run_one_point(idx_point):
            """Worker: full collateral for one Pareto point. Returns (idx, metrics)."""
            idx, point = idx_point
            point_dir = os.path.join(run_dir, f"pareto_{idx:03d}")
            os.makedirs(point_dir, exist_ok=True)

            sep = "\u2500" * 56
            print(f"\n  {sep}")
            print(f"  Pareto point {idx+1}/{n_points}")
            print(f"    TX: N={point.tx_num_stages} "
                  f"\u03b2={point.tx_beta_ratio:.2f} "
                  f"f={point.tx_stage_ratio:.2f}")
            print(f"    RX: preamp="
                  f"{point.rx_w_preamp_n_um:.3f}/"
                  f"{point.rx_w_preamp_p_um:.3f}u  "
                  f"buf={point.rx_w_buf_n_um:.3f}/"
                  f"{point.rx_w_buf_p_um:.3f}u")
            print(f"  {sep}")

            cfg_point = copy.deepcopy(cfg)
            cfg_point.rx.w_preamp_n_um = point.rx_w_preamp_n_um
            cfg_point.rx.w_preamp_p_um = point.rx_w_preamp_p_um
            cfg_point.rx.w_buf_n_um    = point.rx_w_buf_n_um
            cfg_point.rx.w_buf_p_um    = point.rx_w_buf_p_um

            # Apply same TX input slew override used during exploration
            data_rate_Hz_pt = cfg.link.data_rate_Gbps * 1e9
            ui_ns_pt = 1.0 / data_rate_Hz_pt * 1e9
            tx_input_slew_ns = cfg.co_opt.rise_fall_pct_ui * ui_ns_pt
            cfg_point.liberate.input_slews_ns = [tx_input_slew_ns]

            cfg_liberate = copy.deepcopy(cfg_point)

            tx_sizing_override = TxSizingResult(
                chosen=TxSizingCandidate(
                    num_stages     = point.tx_num_stages,
                    beta_ratio     = point.tx_beta_ratio,
                    stage_ratio    = point.tx_stage_ratio,
                    inverter_sizes = point.tx_inverter_sizes,
                    sim_success    = True,
                ),
                all_candidates = [],
                target_rise_ns = 0,
                target_fall_ns = 0,
                target_met     = True,
                message        = "",
            )

            tx_result, rx_result, metrics = _generate_point_collateral(
                cfg_liberate, ch_result, eq_result, term_result, point_dir,
                tx_sizing_override=tx_sizing_override,
            )

            if cfg.output.save_metrics_csv:
                _write_metrics_csv(metrics, point_dir)

            # Area model — runs before gen_lef so LEF dimensions match.
            print("  \u2192 Computing area model...")
            import area as area_mod
            area_result = area_mod.compute(
                cfg_point, tx_result, rx_result, ch_result,
                eq_result, term_result, point_dir,
            )
            print(area_result.report())

            if cfg.output.generate_verilog:
                print("  \u2192 Generating behavioral Verilog models...")
                import gen_verilog
                gen_verilog.generate(cfg_point, metrics, tx_result,
                                    rx_result, point_dir)
            if cfg.output.generate_lef:
                print("  \u2192 Generating LEF macro...")
                import gen_lef
                gen_lef.generate(cfg_point, metrics, tx_result,
                                rx_result, point_dir, area_result=area_result)

            _collect_link_library(point_dir, tx_result.tx_dir,
                                  rx_result.rx_dir)
            return idx, metrics

        point_results = {}  # idx → metrics
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = {
                pool.submit(_run_one_point, (idx, point)): idx
                for idx, point in enumerate(design_points)
            }
            for fut in as_completed(futures):
                idx, metrics = fut.result()   # re-raises any exception from the worker
                point_results[idx] = metrics

        # Restore original order (sorted by delay) expected by _refilter_pareto
        pareto_metrics = [point_results[i] for i in range(n_points)]

        # Re-filter Pareto front using actual (full-collateral) metrics.
        # The exploration step uses interpolated LUT metrics which may deviate
        # from the real Liberate numbers, so some originally-selected points
        # may be dominated after the real characterisation.
        filtered_points, filtered_metrics = _refilter_pareto(
            design_points, pareto_metrics
        )
        n_dropped = len(design_points) - len(filtered_points)
        if n_dropped:
            print(f"\n  [Pareto re-filter] Dropped {n_dropped} point(s) "
                  f"dominated in real metrics "
                  f"({len(filtered_points)} remain on true Pareto front).")

        # Save top-level Pareto summary figure and table
        _pareto_selection = cfg.co_opt.pareto_selection if cfg.co_opt is not None else "balanced"
        _save_pareto_summary(filtered_points, filtered_metrics, run_dir,
                             selection=_pareto_selection)
    else:
        # No co-opt or exploration failed — single point, default sizing
        print("  Generating full collateral with default config sizing...")
        cfg_liberate = copy.deepcopy(cfg)
        tx_result, rx_result, metrics = _generate_point_collateral(
            cfg_liberate, ch_result, eq_result, term_result, run_dir,
        )

        if cfg.output.save_metrics_csv:
            _write_metrics_csv(metrics, run_dir)

        # Area model — runs before gen_lef so LEF dimensions match.
        print("  \u2192 Computing area model...")
        import area as area_mod
        area_result = area_mod.compute(
            cfg, tx_result, rx_result, ch_result,
            eq_result, term_result, run_dir,
        )
        print(area_result.report())

        if cfg.output.generate_verilog:
            print("  \u2192 Generating behavioral Verilog models...")
            import gen_verilog
            gen_verilog.generate(cfg, metrics, tx_result, rx_result, run_dir)
        if cfg.output.generate_lef:
            print("  \u2192 Generating LEF macro...")
            import gen_lef
            gen_lef.generate(cfg, metrics, tx_result, rx_result, run_dir,
                             area_result=area_result)

        _collect_link_library(run_dir, tx_result.tx_dir, rx_result.rx_dir)

        # Write recommended_point.txt for single-point runs (area section only,
        # since there is no Pareto exploration TX/RX parameter table to report).
        _sp_txt_path = os.path.join(run_dir, "recommended_point.txt")
        d_wc = max(metrics.total_delay_rr_ps, metrics.total_delay_ff_ps)
        _sp_lines = [
            "Design Point (Single-Point Run — No Co-Optimisation)",
            "=" * 64,
            "",
            "Metrics:",
            f"  E_tx_pJ_per_bit: {metrics.E_tx_pJ_per_bit:.6f}  (TX+EQ device only)",
            f"  E_channel_pJ_per_bit: {metrics.E_channel_pJ_per_bit:.6f}",
            f"  E_rxpad_bump_pJ_per_bit: {metrics.E_rxpad_bump_pJ_per_bit:.6f}",
            f"  E_rx_device_pJ_per_bit: {metrics.E_rx_pJ_per_bit - metrics.E_rxpad_bump_pJ_per_bit:.6f}",
            f"  E_rx_pJ_per_bit: {metrics.E_rx_pJ_per_bit:.6f}  (device + pad+bump+ESD)",
            f"  E_term_pJ_per_bit: {metrics.E_term_pJ_per_bit:.6f}",
            f"  E_clock_pJ_per_bit: {metrics.E_clock_pJ_per_bit:.6f}  (per data lane)",
            f"  E_data_pJ_per_bit: {metrics.E_data_pJ_per_bit:.6f}  (data lane only)",
            f"  E_total_pJ_per_bit: {metrics.E_total_pJ_per_bit:.6f}  (data + clock)",
            f"  total_delay_wc_ps: {d_wc:.2f}  (= max(RR, FF))",
            f"  total_delay_rr_ps: {metrics.total_delay_rr_ps:.2f}",
            f"  total_delay_ff_ps: {metrics.total_delay_ff_ps:.2f}",
            *_clocking_report_lines(metrics),
            "",
            "Physical Area Model",
            "-" * 40,
            area_result.report(),
        ]
        with open(_sp_txt_path, "w") as _fh:
            _fh.write("\n".join(_sp_lines) + "\n")
        print(f"  → Design point report  : {_sp_txt_path}")

    # ------------------------------------------------------------------
    # Stage 6: Summary
    # ------------------------------------------------------------------
    _stage(6, "Summary")
    if co_result is not None and co_result.pareto_front:
        n_pts = len(co_result.pareto_front)
        print(f"  Generated collateral for {n_pts} Pareto-optimal "
              f"design point(s).")
        if co_result.csv_path:
            print(f"  Exploration results : {co_result.csv_path}")
        if co_result.plot_path:
            print(f"  Pareto plot         : {co_result.plot_path}")
        summary_csv  = os.path.join(run_dir, "pareto_summary.csv")
        summary_plot = os.path.join(run_dir, "pareto_summary.png")
        if os.path.isfile(summary_csv):
            print(f"  Summary table       : {summary_csv}")
        if os.path.isfile(summary_plot):
            print(f"  Summary figure      : {summary_plot}")
        for idx in range(n_pts):
            pdir = os.path.join(run_dir, f"pareto_{idx:03d}")
            print(f"  Pareto {idx:>2} collateral: {pdir}")
    else:
        print(f"  Link collateral generated in: {run_dir}")


def _write_metrics_csv(metrics, run_dir: str) -> None:
    """Write all scalar LinkMetrics fields to metrics.csv inside run_dir."""
    csv_path = os.path.join(run_dir, "metrics.csv")
    fields = {
        f.name: getattr(metrics, f.name)
        for f in dataclasses.fields(metrics)
        if f.name != "warnings"
    }
    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fields.keys()))
        writer.writeheader()
        writer.writerow(fields)
    print(f"  → Metrics saved to {csv_path}")


def _refilter_pareto(design_points, metrics_list):
    """
    Re-compute the Pareto front using actual (full-collateral) LinkMetrics.

    Returns (filtered_points, filtered_metrics) — parallel lists containing
    only the non-dominated subset, sorted by ascending worst-case delay.

    Dominance is judged on (E_total, worst-case delay) where
    worst-case delay = max(total_delay_rr_ps, total_delay_ff_ps).
    A point (E_i, d_i) is dominated if there exists (E_j, d_j) with
    E_j <= E_i AND d_j <= d_i with at least one strict inequality.
    """
    combined = list(zip(design_points, metrics_list))

    def _is_dominated(idx):
        ei = metrics_list[idx].E_total_pJ_per_bit
        di = max(metrics_list[idx].total_delay_rr_ps,
                 metrics_list[idx].total_delay_ff_ps)
        for j, (_, mj) in enumerate(combined):
            if j == idx:
                continue
            ej = mj.E_total_pJ_per_bit
            dj = max(mj.total_delay_rr_ps, mj.total_delay_ff_ps)
            if ej <= ei and dj <= di and (ej < ei or dj < di):
                return True
        return False

    non_dominated = [(p, m) for idx, (p, m) in enumerate(combined)
                     if not _is_dominated(idx)]
    non_dominated.sort(key=lambda pm: max(pm[1].total_delay_rr_ps,
                                          pm[1].total_delay_ff_ps))
    if non_dominated:
        pts, mets = zip(*non_dominated)
        return list(pts), list(mets)
    return [], []


def _select_recommended_pareto_idx(metrics_list, selection: str = "balanced"):
    """Return index of recommended point from final Pareto metrics.

    selection:
      'balanced'   — knee-style: min Euclidean distance to ideal (0, 0) in
                     normalised (energy, delay) space.
      'best_power' — point with lowest total energy per bit.
      'best_delay' — point with lowest worst-case delay.
      'all'        — equivalent to 'balanced' for the primary recommended point;
                     _save_pareto_summary will additionally write best_power and
                     best_delay selections as separate txt files.
    Deterministic tie-break in all modes: lower energy, then lower delay, then index.
    """
    if not metrics_list:
        return None
    if len(metrics_list) == 1:
        return 0

    if selection == "all":
        selection = "balanced"   # primary recommendation uses balanced

    if selection == "best_power":
        best_idx, best_key = 0, None
        for i, m in enumerate(metrics_list):
            d_wc = max(m.total_delay_rr_ps, m.total_delay_ff_ps)
            key = (m.E_total_pJ_per_bit, d_wc, i)
            if best_key is None or key < best_key:
                best_key = key
                best_idx = i
        return best_idx

    if selection == "best_delay":
        best_idx, best_key = 0, None
        for i, m in enumerate(metrics_list):
            d_wc = max(m.total_delay_rr_ps, m.total_delay_ff_ps)
            key = (d_wc, m.E_total_pJ_per_bit, i)
            if best_key is None or key < best_key:
                best_key = key
                best_idx = i
        return best_idx

    # default: 'balanced' — knee-style Euclidean distance
    energies = [m.E_total_pJ_per_bit for m in metrics_list]
    delays = [max(m.total_delay_rr_ps, m.total_delay_ff_ps) for m in metrics_list]

    e_min, e_max = min(energies), max(energies)
    d_min, d_max = min(delays), max(delays)
    e_span = max(e_max - e_min, 1e-12)
    d_span = max(d_max - d_min, 1e-12)

    best_idx = 0
    best_key = None
    for i, m in enumerate(metrics_list):
        d_wc = max(m.total_delay_rr_ps, m.total_delay_ff_ps)
        e_norm = (m.E_total_pJ_per_bit - e_min) / e_span
        d_norm = (d_wc - d_min) / d_span
        dist = (e_norm * e_norm + d_norm * d_norm) ** 0.5
        key = (dist, m.E_total_pJ_per_bit, d_wc, i)
        if best_key is None or key < best_key:
            best_key = key
            best_idx = i
    return best_idx


def _clocking_report_lines(metrics) -> list:
    """Clocking section for the recommended/single-point txt reports ([] if off)."""
    if not getattr(metrics, "clocking_enabled", False):
        return []
    return [
        "",
        "Clocking (UCIe forwarded clock):",
        f"  mode: {metrics.clock_mode}  (M={metrics.clock_M}, "
        f"fCK={metrics.clock_fCK_GHz:g} GHz, "
        f"deskew={'required' if metrics.clock_deskew_required else 'optional'})",
        f"  E_clock_pJ_per_bit (per data lane): {metrics.E_clock_pJ_per_bit:.6f}",
        f"    clock_lane:   {metrics.E_clock_lane_pJ_per_bit:.6f}",
        f"    dll:          {metrics.E_clock_dll_pJ_per_bit:.6f}",
        f"    phase_int_pi: {metrics.E_clock_pi_pJ_per_bit:.6f}",
        f"    serializer:   {metrics.E_clock_ser_pJ_per_bit:.6f}",
        f"    deserializer: {metrics.E_clock_deser_pJ_per_bit:.6f}",
        f"    dcc:          {metrics.E_clock_dcc_pJ_per_bit:.6f}",
        f"  clock_power_total_mW (whole IP): {metrics.clock_power_total_mW:.4f}",
        f"  clock_bumps (one direction): {metrics.clock_bumps}",
    ]


def _write_recommended_point_text(run_dir: str, point_idx: int, point, metrics,
                                   area_report_text: str = "",
                                   label: str = "Recommended Pareto Point (Final Full-Collateral Frontier)",
                                   filename: str = "recommended_point.txt") -> str:
    """Write a human-readable recommended point report for final Pareto front."""
    txt_path = os.path.join(run_dir, filename)
    d_wc = max(metrics.total_delay_rr_ps, metrics.total_delay_ff_ps)
    lines = [
        label,
        "=" * 64,
        f"point_idx: {point_idx}",
        "",
        "TX parameters:",
        f"  tx_num_stages: {point.tx_num_stages}",
        f"  tx_stage_ratio: {point.tx_stage_ratio:.4f}",
        f"  tx_beta_ratio: {point.tx_beta_ratio:.4f}",
        "",
        "RX parameters:",
        f"  rx_w_preamp_n_um: {point.rx_w_preamp_n_um:.4f}",
        f"  rx_w_preamp_p_um: {point.rx_w_preamp_p_um:.4f}",
        f"  rx_w_buf_n_um: {point.rx_w_buf_n_um:.4f}",
        f"  rx_w_buf_p_um: {point.rx_w_buf_p_um:.4f}",
        "",
        "Metrics:",
        f"  E_tx_pJ_per_bit: {metrics.E_tx_pJ_per_bit:.6f}  (TX+EQ device only)",
        f"  E_channel_pJ_per_bit: {metrics.E_channel_pJ_per_bit:.6f}",
        f"  E_rxpad_bump_pJ_per_bit: {metrics.E_rxpad_bump_pJ_per_bit:.6f}",
        f"  E_rx_device_pJ_per_bit: {metrics.E_rx_pJ_per_bit - metrics.E_rxpad_bump_pJ_per_bit:.6f}",
        f"  E_rx_pJ_per_bit: {metrics.E_rx_pJ_per_bit:.6f}  (device + pad+bump+ESD)",
        f"  E_term_pJ_per_bit: {metrics.E_term_pJ_per_bit:.6f}",
        f"  E_clock_pJ_per_bit: {metrics.E_clock_pJ_per_bit:.6f}  (per data lane)",
        f"  E_data_pJ_per_bit: {metrics.E_data_pJ_per_bit:.6f}  (data lane only)",
        f"  E_total_pJ_per_bit: {metrics.E_total_pJ_per_bit:.6f}  (data + clock)",
        f"  total_delay_wc_ps: {d_wc:.2f}  (= max(RR, FF))",
        f"  total_delay_rr_ps: {metrics.total_delay_rr_ps:.2f}",
        f"  total_delay_ff_ps: {metrics.total_delay_ff_ps:.2f}",
    ]
    lines += _clocking_report_lines(metrics)
    if area_report_text:
        lines += [
            "",
            "Physical Area Model",
            "-" * 40,
            area_report_text.rstrip(),
        ]
    with open(txt_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return txt_path


def _save_pareto_summary(design_points, metrics_list, run_dir: str,
                         selection: str = "balanced"):
    """Save pareto_summary.csv and pareto_summary.png to the top-level run_dir.

    Parameters
    ----------
    design_points : list of CoOptCandidate sorted by delay
    metrics_list  : parallel list of LinkMetrics from full-collateral runs
    run_dir       : top-level output directory for this run
    """
    # ------------------------------------------------------------------
    # Determine selection indices
    # When selection == "all", compute all three and write three txt files.
    # The primary recommended index (used for is_recommended in CSV) is "balanced".
    # ------------------------------------------------------------------
    save_all = (selection == "all")
    recommended_idx   = _select_recommended_pareto_idx(metrics_list, "balanced")
    best_power_idx    = _select_recommended_pareto_idx(metrics_list, "best_power") if save_all else None
    best_delay_idx    = _select_recommended_pareto_idx(metrics_list, "best_delay") if save_all else None
    if not save_all:
        # honour explicit single-mode selection for is_recommended column
        recommended_idx = _select_recommended_pareto_idx(metrics_list, selection)

    # ------------------------------------------------------------------
    # CSV table
    # ------------------------------------------------------------------
    csv_path = os.path.join(run_dir, "pareto_summary.csv")
    fieldnames = [
        "point_idx",
        "is_recommended",
    ]
    if save_all:
        fieldnames += ["is_best_power", "is_best_delay"]
    fieldnames += [
        "tx_num_stages", "tx_stage_ratio", "tx_beta_ratio",
        "rx_w_preamp_n_um", "rx_w_preamp_p_um",
        "rx_w_buf_n_um", "rx_w_buf_p_um",
        "E_tx_pJ_per_bit", "E_channel_pJ_per_bit", "E_rxpad_bump_pJ_per_bit",
        "E_rx_pJ_per_bit", "E_term_pJ_per_bit",
        "E_clock_pJ_per_bit", "clock_power_total_mW",
        "E_total_pJ_per_bit", "total_delay_wc_ps", "total_delay_rr_ps", "total_delay_ff_ps",
    ]
    rows = []
    for idx, (point, metrics) in enumerate(zip(design_points, metrics_list)):
        row = {
            "point_idx":          idx,
            "is_recommended":     int(recommended_idx is not None and idx == recommended_idx),
            "tx_num_stages":      point.tx_num_stages,
            "tx_stage_ratio":     round(point.tx_stage_ratio, 4),
            "tx_beta_ratio":      round(point.tx_beta_ratio, 4),
            "rx_w_preamp_n_um":   round(point.rx_w_preamp_n_um, 4),
            "rx_w_preamp_p_um":   round(point.rx_w_preamp_p_um, 4),
            "rx_w_buf_n_um":      round(point.rx_w_buf_n_um, 4),
            "rx_w_buf_p_um":      round(point.rx_w_buf_p_um, 4),
            "E_tx_pJ_per_bit":         round(metrics.E_tx_pJ_per_bit, 6),
            "E_channel_pJ_per_bit":    round(metrics.E_channel_pJ_per_bit, 6),
            "E_rxpad_bump_pJ_per_bit": round(metrics.E_rxpad_bump_pJ_per_bit, 6),
            "E_rx_pJ_per_bit":         round(metrics.E_rx_pJ_per_bit, 6),
            "E_term_pJ_per_bit":       round(metrics.E_term_pJ_per_bit, 6),
            "E_clock_pJ_per_bit":      round(metrics.E_clock_pJ_per_bit, 6),
            "clock_power_total_mW":    round(metrics.clock_power_total_mW, 4),
            "E_total_pJ_per_bit":      round(metrics.E_total_pJ_per_bit, 6),
            "total_delay_wc_ps":  round(max(metrics.total_delay_rr_ps,
                                            metrics.total_delay_ff_ps), 2),
            "total_delay_rr_ps":  round(metrics.total_delay_rr_ps, 2),
            "total_delay_ff_ps":  round(metrics.total_delay_ff_ps, 2),
        }
        if save_all:
            row["is_best_power"] = int(best_power_idx is not None and idx == best_power_idx)
            row["is_best_delay"] = int(best_delay_idx is not None and idx == best_delay_idx)
        rows.append(row)
    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  → Pareto summary table : {csv_path}")

    # ------------------------------------------------------------------
    # Write txt report(s)
    # ------------------------------------------------------------------
    def _area_txt_for(idx):
        txt = ""
        rpt = os.path.join(run_dir, f"pareto_{idx:03d}", "area_report.txt")
        if os.path.isfile(rpt):
            with open(rpt) as fh:
                txt = fh.read()
        return txt

    if recommended_idx is not None:
        txt_path = _write_recommended_point_text(
            run_dir, recommended_idx,
            design_points[recommended_idx], metrics_list[recommended_idx],
            area_report_text=_area_txt_for(recommended_idx),
            label="Recommended Pareto Point — Balanced (min Euclidean distance to ideal)",
            filename="recommended_point.txt",
        )
        print(f"  → Recommended (balanced)   : {txt_path}")

    if save_all and best_power_idx is not None:
        txt_path = _write_recommended_point_text(
            run_dir, best_power_idx,
            design_points[best_power_idx], metrics_list[best_power_idx],
            area_report_text=_area_txt_for(best_power_idx),
            label="Best-Power Pareto Point — Minimum energy per bit",
            filename="best_power_point.txt",
        )
        print(f"  → Best power               : {txt_path}")

    if save_all and best_delay_idx is not None:
        txt_path = _write_recommended_point_text(
            run_dir, best_delay_idx,
            design_points[best_delay_idx], metrics_list[best_delay_idx],
            area_report_text=_area_txt_for(best_delay_idx),
            label="Best-Delay Pareto Point — Minimum worst-case link delay",
            filename="best_delay_point.txt",
        )
        print(f"  → Best delay               : {txt_path}")

    # ------------------------------------------------------------------
    # Pareto frontier figure
    # ------------------------------------------------------------------
    plot_path = os.path.join(run_dir, "pareto_summary.png")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    energies = [m.E_total_pJ_per_bit for m in metrics_list]
    delays   = [max(m.total_delay_rr_ps, m.total_delay_ff_ps) for m in metrics_list]

    # Sort by worst-case delay so the step line is drawn left-to-right
    sorted_pts = sorted(zip(delays, energies, range(len(delays))))
    sd  = [d for d, _, _ in sorted_pts]
    se  = [e for _, e, _ in sorted_pts]
    ids = [i for _, _, i in sorted_pts]

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(sd, se, "o-", color="tomato", linewidth=1.8, markersize=7,
            label="Pareto frontier (full collateral)")
    for orig_idx, (d, e) in zip(ids, zip(sd, se)):
        ax.annotate(str(orig_idx), (d, e),
                    textcoords="offset points", xytext=(5, 4),
                    fontsize=8)

    # Annotation helper
    def _annotate_selection(ax, m_idx, color, marker, zlabel, offset=(10, 10)):
        if m_idx is None:
            return
        m = metrics_list[m_idx]
        d = max(m.total_delay_rr_ps, m.total_delay_ff_ps)
        e = m.E_total_pJ_per_bit
        ax.scatter([d], [e], s=160, marker=marker, color=color,
                   edgecolors="black", linewidths=0.8, zorder=6, label=zlabel)
        ax.annotate(f"{zlabel} ({m_idx})", (d, e),
                    textcoords="offset points", xytext=offset, fontsize=8,
                    bbox=dict(boxstyle="round,pad=0.2", fc="white",
                              ec="black", alpha=0.85))

    _annotate_selection(ax, recommended_idx, "gold",    "*", "Balanced",   (10,  10))
    if save_all:
        _annotate_selection(ax, best_power_idx, "#1f77b4", "^", "Best power", (10, -18))
        _annotate_selection(ax, best_delay_idx, "#2ca02c", "s", "Best delay", (-60, 10))

    ax.set_xlabel("Total link delay — worst-case max(RR, FF) (ps)")
    ax.set_ylabel("Total energy per bit (pJ/bit)")
    ax.set_title("TX/RX Co-Optimisation: Pareto Frontier (Full Collateral)")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(plot_path, dpi=150)
    plt.close(fig)
    print(f"  → Pareto summary figure: {plot_path}")


def _collect_link_library(run_dir: str, tx_dir: str, rx_dir: str) -> None:
    """
    Gather all generated library collateral (.v, .scs, .lib, .lef) into a
    single link_library/ folder under run_dir.

    Files collected:
        txip.scs           — TX SPICE netlist (Liberate input)
        rxip.scs           — RX SPICE netlist (Liberate input)
        txip_nldm.lib      — TX timing/power library (Liberate output)
        rxip_nldm.lib      — RX timing/power library (Liberate output)
        txip.v             — TX Verilog model from Liberate (write_verilog)
        rxip.v             — RX Verilog model from Liberate (write_verilog)
        tx.v               — TX behavioral Verilog (gen_verilog, if generated)
        rx.v               — RX behavioral Verilog (gen_verilog, if generated)
        link_ip.lef        — Physical LEF macro (gen_lef, if generated)
    """
    lib_dir = os.path.join(run_dir, "link_library")
    os.makedirs(lib_dir, exist_ok=True)

    # Mapping: (source_path, dest_filename)
    candidates = [
        (os.path.join(tx_dir, "txip.scs"),                    "txip.scs"),
        (os.path.join(rx_dir, "rxip.scs"),                    "rxip.scs"),
        (os.path.join(tx_dir, "LIBRARY", "txip_nldm.lib"),    "txip_nldm.lib"),
        (os.path.join(rx_dir, "LIBRARY", "rxip_nldm.lib"),    "rxip_nldm.lib"),
        (os.path.join(tx_dir, "LIBRARY", "txip.v"),           "txip.v"),
        (os.path.join(rx_dir, "LIBRARY", "rxip.v"),           "rxip.v"),
        (os.path.join(run_dir, "link_library", "tx.v"),       None),   # already in place
        (os.path.join(run_dir, "link_library", "rx.v"),       None),   # already in place
        (os.path.join(run_dir, "link_library", "link_ip.lef"), None),  # already in place
    ]

    print("\n  Collecting link collateral...")
    collected = []
    for src, dest_name in candidates:
        if not os.path.isfile(src):
            continue
        if dest_name is None:
            # Already written directly into link_library by its generator.
            collected.append(os.path.basename(src))
            continue
        dest = os.path.join(lib_dir, dest_name)
        shutil.copy2(src, dest)
        collected.append(dest_name)

    if collected:
        for name in collected:
            print(f"    {name}")
    print(f"  Output: {lib_dir}")


def run_sweep(cfg: Config, run_dir: str) -> None:
    """Run the pipeline over all sweep combinations."""
    from itertools import product

    combos = list(product(
        cfg.sweep.pkg_type,
        cfg.sweep.reach_mm,
        cfg.sweep.bump_pitch_um,
        cfg.sweep.data_rate_Gbps,
    ))
    print(f"\nSweep: {len(combos)} combinations\n")

    for i, (pkg, reach, pitch, rate) in enumerate(combos, 1):
        cfg.link.pkg_type       = pkg
        cfg.link.reach_mm       = reach
        cfg.link.bump_pitch_um  = pitch
        cfg.link.data_rate_Gbps = rate

        combo_name = f"{pkg}_{reach}mm_{pitch}um_{rate}Gbps"
        combo_dir  = os.path.join(run_dir, combo_name)
        os.makedirs(combo_dir, exist_ok=True)

        print(f"[{i}/{len(combos)}] {combo_name}")
        run_single(cfg, combo_dir)


# ---------------------------------------------------------------------------
# Logging helper — tees stdout/stderr to a file
# ---------------------------------------------------------------------------

class _Tee:
    """Write to both the original stream and a log file simultaneously."""
    def __init__(self, stream, logfile):
        self._stream  = stream
        self._logfile = logfile

    def write(self, data):
        self._stream.write(data)
        self._logfile.write(data)

    def flush(self):
        self._stream.flush()
        self._logfile.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Usage: main.py [config] [pdk]
    #   config : entry config (default: the general <REPO_ROOT>/config.json).
    #   pdk    : optional PDK config path that overrides the entry's "pdk" pointer
    #            (e.g. a config from the restricted overlay repo).
    if len(sys.argv) > 1:
        config_path = os.path.abspath(sys.argv[1])
    else:
        config_path = DEFAULT_CONFIG_PATH
    pdk_override = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else None

    print(f"Loading config: {config_path}"
          + (f"  (pdk override: {pdk_override})" if pdk_override else ""))
    cfg = load_config(config_path, pdk_config=pdk_override)

    try:
        validate_config(cfg)
    except ValueError as e:
        print(f"\nERROR: {e}")
        sys.exit(1)

    run_dir = create_run_dir(cfg, config_path)

    # ------------------------------------------------------------------
    # Tee all output (stdout + stderr) to run.log inside the run directory.
    # ------------------------------------------------------------------
    log_path = os.path.join(run_dir, "run.log")
    _log_fh  = open(log_path, "w", buffering=1)
    _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(sys.stdout, _log_fh)
    sys.stderr = _Tee(sys.stderr, _log_fh)

    try:
        print_config_summary(cfg, run_dir)
        print(f"  Log         : {log_path}")

        if cfg.sweep.enabled:
            run_sweep(cfg, run_dir)
        else:
            run_single(cfg, run_dir)
    finally:
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr
        _log_fh.close()

    print(f"\n  Log saved to: {log_path}")


if __name__ == "__main__":
    main()