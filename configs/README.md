# Config Reference

Parameter documentation for the chiplet link config files. The public release
ships `freepdk45.json` (open-source FreePDK45). All PDK-specific knowledge lives
in the config (plus the per-PDK template directory it points to), so adding a PDK
is purely a matter of supplying a config + a template dir — no Python change.

## Config layout (two files)

A run is assembled from **two** files, **deep-merged** at load time (the top-level
config overrides the PDK config):

```
config.json              # TOP-LEVEL run config (repo root) — the ENTRY POINT
configs/<node>.json      # PDK config — everything technology-specific (incl. the *_hidden blocks)
```

- **Top-level** `config.json` (repo root) is what you pass to `main.py`. It holds
  the run settings — `link`, `sweep`, `output`, `co_opt`, the `liberate`
  characterization settings (slews, `input_slews_ns`, `output_loads_pF`,
  `tx_include_channel_rc`), `clocking`, `channel`/`equalization`/`termination`
  knobs, `layout` — and a `"pdk"` pointer selecting the active PDK config. Override
  the pointer with a 2nd CLI arg: `main.py config.json configs/<node>.json`.
  (`liberate` and `spectre` must already be on your `$PATH`.)
- **PDK** `configs/<node>.json` holds everything technology-specific:
  `process`, `transistor`, `liberate.template_dir` (the PDK's Liberate template
  dir — it stays here so it resolves correctly per PDK), **and** the rarely-touched
  constant blocks (`channel_hidden`, `equalization_hidden`, `termination_hidden`,
  `clocking_hidden`, `tx_hidden`, `rx_hidden`, `area_hidden`).

Precedence is **pdk < top-level**, so a value set in `config.json` overrides the
same value in the PDK config. Path-valued keys (`lib_path`, `template_dir`,
`base_dir`) are resolved relative to the file that **defines** them.

The sections below document every field regardless of which file it lives in.

---

## `process`

| Field | Description |
|---|---|
| `node` | Process node identifier string (e.g. `"freepdk45"`). Used only as a label / results-directory name. |
| `lib_path` | Path to the PDK model file. For `model_include_format = "hspice_ptm"` (FreePDK45) this is a *directory* (`.../hspice/tran_models`) whose `models_<corner>/` subdir holds the HSPICE BSIM4 PTM `.inc` models; for `spice_lib` / `spectre_include` it is the model file itself. Set this to your own install path. |
| `lib_corner` | Simulation corner. For `hspice_ptm` it selects the `models_<corner>/` subdir (`"nom"`/`"ff"`/`"ss"`); for `spice_lib` it is the `.lib` section name; for `spectre_include` it may be empty. |
| `lib_corner2` | Optional second corner include (e.g. for diode/passive models). `null` for core-only designs. |
| `model_include_format` | How `model.sp` is built (see `scripts/device.py`): `"hspice_ptm"` (`.inc` PTM models + wrapper subckts; FreePDK45), `"spice_lib"` (`.lib "<lib_path>" <corner>`), or `"spectre_include"` (Spectre `include` of `lib_path` + `allModels.scs`). |
| `vdd` | Supply voltage in volts. Also used as the TX swing for the termination boundary table lookup. FreePDK45: 1.1 V. |
| `temp` | Simulation temperature in °C. |

---

## `link`

Primary link specification. These are the most commonly changed parameters.

| Field | Description |
|---|---|
| `pkg_type` | Package / interposer type: `"silicon"` (silicon interposer / fan-out) or `"organic"` (organic substrate / flip-chip). Drives default trace RC, ESD, and pad capacitance models. |
| `reach_mm` | Die-to-die reach in mm. Drives channel RC, ESD level selection, and termination decisions. |
| `bump_pitch_um` | C4 / micro-bump pitch in µm. Drives bump diameter, pad capacitance, and area model. |
| `data_rate_Gbps` | Target data rate in Gb/s per lane. Used for UCIe spec lookups and timing constraints. |
| `lane_count` | Number of data lanes. Must equal the number of `tx` bumps AND `rx` bumps in the bump map file. |

---

## `clocking`

Forwarded-clock power-overhead model (`scripts/clocking.py`). Reuses the single-lane data characterization for the clock *lane* and adds the on-die phase-generation circuits, normalized to **per data bit, per data lane**, so the total folds onto `E_total`. The forwarded clock is **differential** (2 wires), fixed by the UCIe architecture and not currently configurable; the data-rate-dependent half-/quarter-rate choice is taken from the spec.

| Field | Description |
|---|---|
| `enabled` | Compute, report, and **add** the clocking overhead to `E_total`. `false` = zeroed (nothing added) — this is the single on/off knob for clocking in the totals. |
| `n_clocks` | Number of forwarded clocks in the IP. UCIe forwards one clock shared by the whole module → `1`. `x = lane_count / n_clocks` data lanes share each clock (sets the amortization). |
| `ratio` | Data:clock ratio: `"sdr"` (M=1), `"ddr"` (M=2), `"qdr"` (M=4), or `"ucie"` to pick from the data rate per the spec (half-rate `ddr` if fCK ≤ `fck_cap_GHz`, else quarter-rate `qdr`). |
| `deskew` | Per-lane phase-interpolator deskew. `"ucie"` = spec rule (Required ≥ `deskew_required_min_GTs`, off below). Or force `true` / `false`. |

**Quarter-rate note:** the clock lane is characterized at the equivalent rate `2R/M`. Half-rate's equivalent rate equals the data rate, so the existing data-lane run is reused exactly; quarter-rate's is `R/2`, which is kept as the rate-independent proxy (no extra Liberate run — slightly over-counts).

---

## `transistor`

PDK transistor model names, default sizing, and the device-instantiation style.
Widths are in µm; for FinFET style the code converts width to fin count internally.

| Field | Description |
|---|---|
| `nmos_name` | NMOS device/subcircuit model name as defined in this PDK's `model.sp` / library. FreePDK45: `nmos_vtg`. |
| `pmos_name` | PMOS device/subcircuit model name. FreePDK45: `pmos_vtg`. |
| `w_n_um` | NMOS channel width in µm. |
| `w_p_um` | PMOS channel width in µm. |
| `l_um` | Gate length in µm (minimum core gate length for the node). |
| `nf` | Number of fingers per device instance (emitted only when `include_nf` is true). |
| `w_min_um` | Minimum allowed channel width (sizing bound; also the single-fin width for `finfet_nfin`). |
| `w_max_um` | Maximum allowed channel width (sizing bound; `subckt_wl` splits wider devices into parallel instances). |
| `style` | Device-instantiation style (`scripts/device.py`): `"subckt_wl"` → `x.. <model> w=.. l=.. [nf=..]`; `"finfet_nfin"` → `x.. <model> L=..n nfin=..` (width quantized to fins). |
| `include_nf` | `subckt_wl` only: emit the `nf=<nf>` token on each instance. Set `false` for devices whose subckt takes no `nf` parameter. |
| `nfin_w_min_um` | `finfet_nfin` only: physical width of a single-fin device. |
| `nfin_w_step_um` | `finfet_nfin` only: width added per extra fin. `nfin = max(1, round((w − nfin_w_min)/nfin_w_step) + 1)`. |
| `nfin_max` | `finfet_nfin` only: max fins per instance; wider devices are split into parallel instances. |

**FreePDK45 note:** `nmos_vtg` / `pmos_vtg` are parameterized 4-terminal wrapper subckts (D G S B) defined in the template `model.sp`, wrapping the open-source BSIM4 PTM models (`NMOS_VTG` / `PMOS_VTG`). Other flavors are available by name: `nmos_vtl`/`vth`/`thkox` (+ `pmos_*`). Drawn `l_um` = 0.05 µm; uses `style = "subckt_wl"` with `include_nf = true`.

---

## `channel`

User-facing physical channel parameters. Tune these to match your physical link geometry.

| Field | Description |
|---|---|
| `bump_diameter_scale` | `bump_diameter = bump_diameter_scale × bump_pitch`. Default 0.64 (64% fill). |
| `bump_diameter_um` | Override: fixed bump diameter in µm. `null` = use `bump_diameter_scale`. |
| `bump_height_um` | Override: bump height in µm. `null` = auto: 35 µm for diameter ≥ 20 µm, else height = diameter. |
| `trace_width_um` | Override: trace width in µm. `null` = auto: silicon → 3 µm, organic → 30 µm (from `channel_hidden` base values). |
| `esd_type` | Override ESD structure type: `"silicon"` or `"organic"`. `null` = derived from `pkg_type`. |
| `esd_mode` | ESD level selection. `"auto"`: select level based on `reach_mm` using `esd_levels` table. Or set explicitly to `"minimal"`, `"moderate"`, or `"standard"`. |
| `pad_cap_mode` | Pad capacitance model. `"physical"`: compute from geometry (parallel-plate model). `"ucie"`: use UCIe spec max values by data rate (from `ucie_pad_cap_table`); ESD cap is set to 0 because it is subsumed into the UCIe pad cap spec. |

### `channel.coupling_cap`

Inter-lane coupling-capacitance model. When enabled, adjacent lanes are stitched with `Cc = ratio × C_ground` at every Pi-ladder node (4 trace + 2 interposer pad) inside `txip.scs`, and an on-die PAD-to-PAD fringe cap inside `rxip.scs`.

| Field | Description |
|---|---|
| `enabled` | Enable/disable inter-lane coupling model. |
| `cc_ratio_trace` | `Cc / C_trace_shunt` ratio at each trace Pi-ladder node. Physically 0.3–0.5 for edge-coupled microstrip with pitch/width ~8:1; reduce toward 0.1 for wider pitch / thicker dielectric. |
| `cc_ratio_pad` | `Cc / C_pad_interposer` ratio at each TX/RX interposer pad node. Typically smaller than `cc_ratio_trace` because landing pads are farther apart than routed traces. |
| `cc_rx_pad_fF` | Absolute chip-PAD to chip-PAD coupling capacitance on the RX die (fF). Represents local on-die pad-ring fringe cap between adjacent lanes; independent of interposer coupling modelled in `txip.scs`. |

---

## `equalization`

| Field | Description |
|---|---|
| `passive_eq_enabled` | Enable passive RC equalization at TX output. |
| `loss_threshold_dB` | Enable passive equalization when channel RC loss at Nyquist exceeds this value (dB). |

---

## `termination`

Thevenin split topology with VTERM mid-rail.

| Field | Description |
|---|---|
| `ac_coupled` | Use AC-coupled termination (capacitor in series with termination resistor). `false` = DC-coupled. |
| `r_tx_ohm` | TX driver output impedance in Ω. Typical 50 Ω; UCIe Standard Package allows 30 Ω at 12/16 GT/s (Table 5-18 footnote b). |
| `r_rx_ohm` | RX termination resistance in Ω. |
| `r_bias_hi_ohm` | High-side Thevenin bias resistor in Ω. Equal values (hi = lo) give symmetric VTERM = VDD/2. Kept large (1 MΩ) to minimize static current; actual termination power is modeled analytically. |
| `r_bias_lo_ohm` | Low-side Thevenin bias resistor in Ω. See `r_bias_hi_ohm`. |

---

## `rx`

RX pre-amplifier and output buffer initial sizing. Widths in µm; for `style = "finfet_nfin"` they are converted to fin count internally (`nfin = round((w − nfin_w_min)/nfin_w_step) + 1`).

| Field | Description |
|---|---|
| `w_preamp_n_um` | NMOS width of pre-amplifier stage (µm). |
| `w_preamp_p_um` | PMOS width of pre-amplifier stage (µm). |
| `w_buf_n_um` | NMOS width of output buffer (µm). |
| `w_buf_p_um` | PMOS width of output buffer (µm). |
| `input_slews_ns_override` | Override RX input slew list (ns). `null` = auto-detected from TX output transition. Set to a list (e.g. `[0.25, 0.5, 1.0]`) to fix the slew. |
| `rx_slew_source` | Source for auto-detected RX slews. `"tx_pad"` = TX output at pad before channel (tx_only run). `"channel"` = signal at far end of channel (main TX run). |

---

## `co_opt`

TX/RX co-optimisation: flat grid of `n_tx_configs × n_rx_configs` pairs. Each pair runs the full Liberate pipeline. TX↔RX coupling is resolved per pair (TX uses measured RX input cap as load; RX uses actual TX output slew).

| Field | Description |
|---|---|
| `enabled` | Enable co-optimisation. Preferred over `tx_sizing` / `rx_sizing` individually. |
| `rise_fall_pct_ui` | TX input slew as fraction of UI. Sets the Liberate `index_1` input transition time. UCIe spec: 0.35 × UI (20%–80%). E.g. at 16 Gb/s: 0.35 × 62.5 ps = 21.875 ps. |
| `max_latency_ui` | TX+RX latency upper bound in UI (excluding channel). `"auto"` = follow UCIe spec: 12 UI for ≤ 16 Gb/s, 16 UI for > 16 Gb/s. Set to a number to override. |
| `n_tx_configs` | Number of TX configurations in the co-opt grid. |
| `n_rx_configs` | Number of RX configurations in the co-opt grid. Total pairs = `n_tx_configs × n_rx_configs`. |
| `n_tx_load_points` | Number of TX load points for the characterisation sweep. |
| `n_rx_slew_points` | Number of RX slew points for the characterisation sweep. |
| `max_parallel` | Maximum concurrent Liberate runs during exploration. Set to number of available CPU cores / licenses. |
| `pareto_selection` | Which Pareto point is recommended. `"all"`: return full Pareto front. `"balanced"`: knee-style min Euclidean distance to ideal (lowest energy + lowest delay). `"best_power"`: lowest energy per bit. `"best_delay"`: lowest worst-case delay. |

---

## `layout`

Physical layout options using a bump map text file.

Bump map format: tokens per cell are `tx`, `rx`, `vdd`, `vss`, `other`, `-` (empty). Whitespace or comma separated; `#` starts a comment line. The number of `tx` tokens AND `rx` tokens must each equal `link.lane_count`. `vdd`/`vss`/`other`/empty counts are free.

| Field | Description |
|---|---|
| `bump_map_enabled` | Use a bump map for area analysis. When `false`, `scripts/area.py` reports per-lane dimensions only. |
| `bump_map_file` | Path to the `.txt` bump map. Relative paths are resolved against the config file's directory. When enabled and the file is missing or its `tx`/`rx` counts don't match `lane_count`, the run stops with an error. |

---

## `output`

| Field | Description |
|---|---|
| `base_dir` | Base directory for run outputs. Run dirs are created as `<base_dir>/<timestamp>_<link_name>/`. Config is copied in automatically. |
| `save_netlists` | Save generated SPICE/Spectre netlists. |
| `save_liberate_decks` | Save Liberate TCL decks. |
| `save_lib` | Save the characterised Liberty `.lib` file. |
| `save_metrics_csv` | Save metrics summary as CSV. |
| `generate_verilog` | Generate a behavioural Verilog model. |
| `generate_lef` | Generate an abstract LEF file. |

---

## `liberate`

Liberate characterisation settings.

| Field | Description |
|---|---|
| `template_dir` | Path to the Liberate template directory for this PDK (relative to config file). |
| `slew_lower_rise` | Lower threshold for rise slew measurement (fraction of VDD). |
| `slew_upper_rise` | Upper threshold for rise slew measurement (fraction of VDD). |
| `slew_lower_fall` | Lower threshold for fall slew measurement (fraction of VDD). |
| `slew_upper_fall` | Upper threshold for fall slew measurement (fraction of VDD). |
| `input_slews_ns` | List of input slew times (ns) to sweep in Liberate characterisation. |
| `output_loads_pF` | List of output load capacitances (pF) to sweep. Scale to the node: FinFET nodes use small loads (e.g. `[0.0005, ..., 0.064]`); 45–65nm-class nodes use moderate loads (e.g. `[0.005, ..., 1.0]`, the FreePDK45 default). |
| `tx_include_channel_rc` | Embed channel RC ladder inside `txip.scs` so Liberate captures TX+channel delay and energy in one shot. When `true`: channel energy is NOT added separately in `get_metrics` (already in TX switching power); TX delay covers full TX+channel propagation. Only enable when the template `txip.scs` supports the `channel_rc` `.param`. |

---

## `sweep`

Parameter sweep mode. When `enabled`, overrides `link` parameters and runs all combinations.

| Field | Description |
|---|---|
| `enabled` | Enable sweep mode. |
| `pkg_type` | List of package types to sweep. |
| `reach_mm` | List of reach values (mm) to sweep. |
| `bump_pitch_um` | List of bump pitches (µm) to sweep. |
| `data_rate_Gbps` | List of data rates (Gb/s) to sweep. |

---

## `tx_sizing` *(hidden — advanced)*

Standalone TX sizing: automatically find the inverter chain configuration that meets the rise/fall time target with minimum power. **Disabled by default** — use `co_opt` for joint TX+RX sizing, which gives better results because TX and RX are sized together.

| Field | Description |
|---|---|
| `enabled` | Enable standalone TX sizing. |
| `rise_fall_pct_ui` | Target TX rise/fall time as fraction of UI (20%–80%). UCIe spec: 0.35. E.g. at 32 Gb/s: 0.35 × 31.25 ps = 10.94 ps. |
| `max_iterations` | Maximum SPICE simulations for the adaptive search. The algorithm uses analytical initialisation + golden-section refinement; 15–20 is usually sufficient. |

---

## `rx_sizing` *(hidden — advanced)*

Standalone RX buffer sizing optimisation. **Disabled by default** — use `co_opt`. Separate RX sizing ignores the TX output slew dependency and produces suboptimal results.

| Field | Description |
|---|---|
| `enabled` | Enable standalone RX sizing. |
| `max_rx_delay_ui_fraction` | Fraction of UI allocated to RX buffer delay. 0.1 = 10% of UI. |

---

## `channel_hidden` *(hidden — physical model constants)*

Physical model constants sourced from literature. Only modify when updating the underlying RC model.

| Field | Description |
|---|---|
| `eps_sio2` | Relative permittivity of SiO₂ (3.9). |
| `eps_fr4` | Relative permittivity of FR4 organic substrate (4.7). |
| `eps_polyimide` | Relative permittivity of polyimide dielectric (3.5). |
| `tox_chiplet_um` | ILD thickness on chiplet pad (µm). Advanced BEOL ≈ 0.5 µm; 45–65nm-class BEOL stacks ≈ 0.8 µm. Exact thickness is PDK-specific — set from your own stack. |
| `tox_silicon_interposer_um` | ILD thickness on silicon interposer pad (µm). Default 1.0 µm. Ref: Y.-H. Chen et al., "Silicon vs. Organic Interposer," DATE 2021. |
| `tox_organic_substrate_um` | ILD thickness on organic substrate pad (µm). Default 20.0 µm. Ref: J. H. Lau, *Fundamentals of Microsystems Packaging*, McGraw-Hill, 2001. |
| `r_pad_ref_ohm` | Reference pad resistance at reference width (Ω). Default 0.017 Ω at 25 µm. Ref: S. L. Wright et al., "Characterization of Micro-bump C4 Interconnects," ECTC 2006, doi:10.1109/ECTC.2006.1645716. |
| `r_pad_ref_width_um` | Reference width for `r_pad_ref_ohm` scaling (µm). |
| `bump_resistivity_ohm_um` | Bump material resistivity (Ω·µm). Default 0.0168 Ω·µm = 1.68×10⁻⁸ Ω·m (bulk Cu). Ref: T. Bandaru et al., IEEE Trans. Compon. Packag. Manuf. Technol., 2013. |
| `bump_relative_permeability` | Bump material relative permeability. 1.0 for copper. |
| `trace_silicon.c_per_mm_fF` | Silicon interposer trace capacitance (fF/mm). Default 185 fF/mm. |
| `trace_silicon.r_per_mm_ohm` | Silicon interposer trace resistance (Ω/mm). Default 1.04 Ω/mm. |
| `trace_silicon.default_width_um` | Default silicon interposer trace width (µm). Default 3 µm. |
| `trace_silicon.default_eps_r` | Default silicon trace dielectric constant. |
| `trace_organic.c_per_mm_fF` | Organic substrate trace capacitance (fF/mm). Default 138 fF/mm. |
| `trace_organic.r_per_mm_ohm` | Organic substrate trace resistance (Ω/mm). Default 0.036 Ω/mm. |
| `trace_organic.default_width_um` | Default organic substrate trace width (µm). Default 30 µm. |
| `trace_organic.default_eps_r` | Default organic trace dielectric constant. |
| `esd_silicon_fine_pitch_fF` | ESD capacitance for fine-pitch silicon pads (fF). Default 1.6 fF. Ref: Industry Council on ESD Target Levels, White Paper 2, rev. 1.0. Applies when bump pitch < `esd_fine_pitch_threshold_um`. |
| `esd_fine_pitch_threshold_um` | Pitch threshold below which fine-pitch ESD sizing applies (µm). Default 10 µm (hybrid-bonding regime). |
| `esd_silicon_interposer_fF` | ESD capacitance per pad for standard silicon interposer (fF). Default 27 fF. Ref: B. Van Thourhout et al., IEEE Trans. Device Mater. Rel., 2016. |
| `esd_organic_fF` | ESD capacitance per pad for organic substrate (fF). Default 225 fF. Ref: R. Venkatesan et al. (NVIDIA), "Simba," IEEE JSSC, 2020. |
| `esd_levels` | Graduated ESD protection table: `[max_reach_mm, multiplier, label]`. Multiplier scales the base ESD capacitance. Entry with `max_reach_mm=0` is catch-all. Higher reach allows stronger ESD. |
| `ucie_pad_cap_table` | UCIe Standard Package spec max pad capacitance (fF) by data rate (GT/s): `[max_data_rate_GTs, cap_fF]`. Entry with `max_data_rate_GTs=0` is catch-all. Values: ≤8 GT/s → 300 fF, ≤16 GT/s → 200 fF, ≤32 GT/s → 125 fF. Used for both TX and RX chiplet pads when `pad_cap_mode = "ucie"`. |

---

## `equalization_hidden` *(hidden — EQ topology constants)*

Equalizer sizing constants. Only change when updating the EQ topology.

| Field | Description |
|---|---|
| `eq_cap_fraction` | `C_eq = eq_cap_fraction × C_channel`. Controls equalizer strength vs. area. Typical range: 0.05–0.2. |
| `eq_levels` | Graduated EQ table: `[max_loss_multiplier, eq_cap_fraction, label]`. Multiplier is relative to `loss_threshold_dB`. Entry with `max_loss_multiplier=0` is catch-all. Higher loss triggers stronger pre-emphasis. |

---

## `termination_hidden` *(hidden — PHY boundary tables)*

Datasheet boundary table and model constants. Only modify when updating to a new PHY spec.

| Field | Description |
|---|---|
| `termination_improvement_factor` | Reach multiplier when termination is used vs. unterminated. Default 1.75×. |
| `unterminated_limits` | `unterminated_limits[tx_swing] = [[data_rate_GTs, max_reach_mm], ...]` boundary pairs, reach monotonically increasing. The nearest `tx_swing` key to `process.vdd` is selected automatically. |
| `term_levels` | Graduated termination table: `[max_reach_ratio, r_scale, c_ac_scale, label]`. `r_scale` applies to `r_rx_ohm`; `c_ac_scale` applies to 30 pF base. Entry with `max_reach_ratio=0` is catch-all. Higher reach ratio triggers stronger termination. |

---

## `clocking_hidden` *(hidden — clock-circuit energy constants)*

Energy constants for the clocking model. **Serializer and DCC are measured** (transient Spectre, FreePDK45 1.1 V 8 GHz, `temp/ddr_overhead/`); the **deserializer, DLL, and per-lane PI are estimated by analogy to the measured DCC** (a duty/phase circuit of similar class) and each emit a warning in the report — recharacterize per PDK when data is available. In the non-FreePDK45 configs all values are FreePDK45-sourced estimates.

| Field | Description |
|---|---|
| `serializer_fj_per_stage` | Energy of one 2:1 serializer stage (fJ/bit, α=0.5, real txip load). An M:1 serializer is `log2(M)` stages (per lane). **Measured.** |
| `deserializer_fj_per_stage` | Energy of one 2:1 deserializer stage (fJ/bit), RX side. Assumed symmetric to the serializer (not yet measured). |
| `dcc_power_uW` | Duty-cycle corrector power per clock domain (µW), amortized over the `x` lanes sharing the clock. Applied when M ≥ 2 (both clock edges used). **Measured.** |
| `dll_power_uW` | RX DLL / phase-generator power per clock domain (µW), amortized over `x`; scaled `×(M/2)` (quarter-rate quadrature ≈ 2× a half-rate phase shift). Estimated by analogy to the DCC. |
| `pi_power_uW_per_lane` | Per-lane phase-interpolator (deskew) power (µW). **Per data lane — does not amortize.** Applied only when deskew is required. Estimated by analogy to the DCC. |
| `fck_cap_GHz` | Forwarded-clock frequency cap (GHz). Above it the half-rate clock is too fast, so `ratio: "auto"` selects quarter-rate. UCIe: 8 GHz. |
| `deskew_required_min_GTs` | Data rate (GT/s) at/above which per-lane deskew is Required under `deskew: "auto"`. UCIe: 12 GT/s. |

---

## `area_hidden` *(hidden — layout density constants)*

Density and margin constants for the area model (`scripts/area.py`). Node-aware base values are loaded from `_AREA_DEFAULTS_BY_NODE` in `area.py`; values set here take precedence. For any PDK other than FreePDK45, supply your own densities/geometry in this `area_hidden` section (the base 65nm-class defaults are used otherwise).

The **FreePDK45** values below are sourced — and documented inline — in `area.py` `_AREA_DEFAULTS_BY_NODE["freepdk45"]`: geometry values (`metal_*`, `nwell_pwell_gap_um`) are cited directly from the open kit (`gscl45nm.lef`, `calibreDRC.rul`); the rest are 45 nm-class estimates because FreePDK45 is a digital-only PDK (no MIM cap, precision resistor, or ESD diode). `configs/freepdk45.json` leaves these keys `null` so the documented `area.py` defaults apply.

| Field | FreePDK45 | Description |
|---|---|---|
| `cap_mim_fF_per_um2` | 1.2 | Capacitor density (fF/µm²). FreePDK45 has no MIM → MOM (metal-finger) estimate. |
| `res_poly_ohm_per_sq` | 250 | Poly resistor sheet resistance (Ω/sq). Used for R_eq (TX) and R_term (RX). FreePDK45: estimate (no precision-R layer). |
| `res_min_width_um` | 0.10 | Minimum resistor width (µm). |
| `res_bias_ohm_per_sq` | 1200 | High-R layer sheet resistance for MΩ-class Thevenin bias resistors (Ω/sq). |
| `esd_diode_fF_per_um2` | 0.3 | ESD diode capacitance density (fF/µm²). FreePDK45: estimate (no ESD device). |
| `pad_cap_fF_per_um2` | 1.2 | On-die landing pad stack capacitance density (fF/µm²). |
| `metal_spacing_um` | 0.07 | Local metal spacing (µm). FreePDK45: M2/M3 `SPACING` from `gscl45nm.lef`. |
| `metal_track_um` | 0.07 | Local metal track width (µm). FreePDK45: M2/M3 `WIDTH` from `gscl45nm.lef`. |
| `nwell_pwell_gap_um` | 0.225 | N-well/P-well isolation gap (µm). FreePDK45: `calibreDRC.rul` (`EXTERNAL nwell pwell < 0.225`). |
| `active_margin_frac` | 0.25 | `active_area *= (1 + frac)`. Covers diffusion contacts, poly extensions, well taps. |
| `pad_access_overhead_um` | 1.5 | Routing overhead per pad for pad access (µm). |
| `lane_width_frac` | null | IO cell width as fraction of bump pitch P. `null` = auto (`lane_width = pad_diam + 2×metal_spacing`). Set to e.g. 0.8 to force `lane_width = 0.8×P`. Must be ≤ 1.0. |
