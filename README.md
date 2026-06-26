# CLIPGen — Chiplet Link IP Generation

End-to-end characterization framework for 2.5D chiplet die-to-die (D2D) links.
Given a process technology and a physical link specification, the framework:

1. Models the channel RC (pad, bump, trace, ESD).
2. Evaluates termination and passive equalization decisions.
3. Generates SPICE netlists for TX and RX.
4. Runs Cadence Liberate to produce `.lib` timing/power datasheets.
5. Extracts per-bit energy, delay, and area metrics.
6. Writes behavioral Verilog (`.v`) and physical LEF (`.lef`) models.

---

## Repository Layout

```
CLIPGen/
├── config.json                 # TOP-LEVEL run config (entry point); selects a PDK via "pdk"
├── configs/                    # per-PDK configuration
│   └── freepdk45.json          # PDK config: process/transistor/co_opt + hidden constant blocks
├── scripts/                    # Python source — all pipeline stages (PDK-agnostic)
│   ├── main.py                 # Entry point / pipeline orchestrator
│   ├── device.py               # Config-driven device-instantiation helpers
│   ├── channel.py              # Stage 1: channel RC model
│   ├── termination.py          # Stage 2: termination decision
│   ├── equalization.py         # Stage 3: passive equalization
│   ├── tx.py                   # Stage 4: TX netlist + Liberate
│   ├── rx.py                   # Stage 5: RX netlist + Liberate
│   ├── get_metrics.py          # Stage 6: metrics extraction
│   ├── gen_verilog.py          # Stage 7: behavioral Verilog generation
│   ├── gen_lef.py              # Stage 7: LEF macro generation
│   ├── area.py                 # Transistor area helpers
│   ├── clocking.py             # Clocking overhead model
│   └── ...
├── templates/                  # Liberate characterization templates (per PDK)
│   └── freepdk45_liberate_template/
│       ├── txip/               # txip.scs, model.sp, char.tcl, run.sh, …
│       └── rxip/
└── results/                    # Auto-created run output directories
    └── <timestamp>_<link_tag>/
        ├── config.json         # Copy of the config used for this run
        ├── tx/                 # TX Liberate working directory
        ├── rx/                 # RX Liberate working directory
        └── link_library/       # Assembled library collateral
            ├── txip.scs
            ├── rxip.scs
            ├── txip_nldm.lib
            ├── rxip_nldm.lib
            ├── tx.v / rx.v
            └── link_ip.lef
```

---

## Prerequisites

- **Python 3.7+** (standard library only — no third-party packages).
- **Cadence Liberate** and **Spectre** must already be **on your `$PATH`**. 
- **A PDK.** This release ships **FreePDK45** (open-source). Download/install it,
  then set `process.lib_path` in **`config.json`** to your
  `.../ncsu_basekit/models/hspice/tran_models` directory.

## Configuration model

You normally edit **one file: `config.json`**. A run is assembled from
two files, merged at load time (the top-level config overrides the PDK config):

1. **`config.json`** — the **entry point** and the only file you usually touch.
   The two things to set live here: `pdk` (which PDK to run, default
   `configs/freepdk45.json`) and `process.lib_path` (your install of that PDK's
   models) — plus all the run settings (link spec, `sweep`, `output`, `co_opt`,
   and the `liberate` characterization settings: slews, `output_loads_pF`, …).
2. **`configs/<pdk>.json`** — the **PDK config** selected by `pdk`: everything
   technology-specific (`process` node/corner/format/vdd, `transistor`,
   `liberate.template_dir`) plus the rarely-touched `*_hidden` constant blocks.

You pass `config.json` to `main.py`; it pulls in the selected PDK config
automatically. (A key left `null` in `config.json`, e.g. `process.lib_path`,
inherits the PDK config's value — handy when a PDK ships its own path.)

## Quick Start

### 1. Run with the defaults (general `config.json` → FreePDK45)

```bash
cd CLIPGen
# source your own Cadence environment first if not already on PATH, e.g.:
#   source ~/setup_cadence.csh
python3 scripts/main.py                      # uses ./config.json (pdk = freepdk45)
```

### 2. Select a different PDK

Either edit the `"pdk"` pointer in `config.json`, or pass a PDK config as the
**second** argument (it overrides the pointer — handy for the restricted overlay):

```bash
python3 scripts/main.py config.json configs/freepdk45.json
python3 scripts/main.py config.json /path/to/overlay/configs/<pdk>.json
```

`main.py` can be invoked from any working directory — every path inside a config
is resolved relative to the file that defines it.

### 3. Run a parameter sweep

Set `"sweep": {"enabled": true, ...}` in the config JSON, then run as above.
Every combination of `pkg_type × reach_mm × bump_pitch_um × data_rate_Gbps`
gets its own subdirectory under `results/<timestamp>_sweep/`.

---

## Configuration reference

Every parameter — its meaning, valid values, and how to set it — is documented in
**[`configs/README.md`](configs/README.md)**. The **Configuration model** section
above explains how `config.json` and the PDK config combine.

### Shipped PDK

| Config | Process | VDD | Notes |
|---|---|---|---|
| `freepdk45.json` | FreePDK45 (open 45 nm bulk) | 1.1 V | Open-source PTM BSIM4 models; `model_include_format = "hspice_ptm"`, `style = "subckt_wl"` |

The framework itself is PDK-agnostic — all PDK-specific knowledge lives in the
config (`process.model_include_format`, `transistor.style`, device names, …) plus
the per-PDK template directory it points to. See **Adding a PDK** below.

---

## Pipeline Stages

`scripts/main.py` orchestrates seven stages in sequence:

| Stage | Module | Output |
|---|---|---|
| 1 | `channel.py` | Per-component C/R breakdown; total channel RC |
| 2 | `termination.py` | Termination recommendation and power budget |
| 3 | `equalization.py` | Passive EQ decision; R_eq / C_eq values |
| 4 | `tx.py` | `txip.scs`, Liberate scripts → `txip_nldm.lib` |
| 5 | `rx.py` | `rxip.scs`, Liberate scripts → `rxip_nldm.lib` |
| 6 | `get_metrics.py` | Energy/delay metrics CSV; summary report |
| 7 | `gen_verilog.py`, `gen_lef.py` | `tx.v`, `rx.v`, `link_ip.lef` |

All stage outputs are written to a timestamped run directory:

```
results/<YYYYMMDD_HHMMSS>_<pkg>_<reach>mm_<pitch>um_<rate>Gbps/
```

---

## Output: `link_library/`

After a successful run, `link_library/` contains everything needed to
integrate the characterised IP into a digital flow:

| File | Description |
|---|---|
| `txip.scs` | TX SPICE subcircuit (Spectre format) |
| `rxip.scs` | RX SPICE subcircuit (Spectre format) |
| `txip_nldm.lib` | TX NLDM Liberty timing/power library |
| `rxip_nldm.lib` | RX NLDM Liberty timing/power library |
| `txip.v` / `rxip.v` | Verilog models from Liberate `write_verilog` |
| `tx.v` / `rx.v` | Behavioral Verilog (gen_verilog, N-lane wrappers) |
| `link_ip.lef` | Physical LEF MACRO for txip and rxip |

---

## Liberate Templates (`templates/`)

Each PDK has its own template subdirectory referenced by
`liberate.template_dir` in the config. The template provides:

- `txip/txip.scs` — Unit TX cell subcircuit (used verbatim; `.param`
  `equalization` / `Req` / `Ceq` are patched by `tx.py` at runtime).
- `rxip/rxip.scs` — Unit RX cell subcircuit.
- `txip/char.tcl`, `txip/run.sh` — Liberate characterization scripts.
- `txip/template.tcl` — Timing arc templates.
- `txip/define_leafcell.tcl` — Liberate leaf-cell definitions (read **verbatim**;
  this is where the PDK's device/cell names live).
- `txip/model.sp` — Model include (for `spice_lib` / `spectre_include` this file is
  used directly; for `hspice_ptm` `model.sp` is generated from `process.lib_path`).

---

## Adding a PDK

The Python is PDK-agnostic, so adding a PDK is **pure data** — no code change:

1. Copy `templates/freepdk45_liberate_template/` to `templates/<pdk>_liberate_template/`
   and adapt `txip/rxip` `*.scs`, `define_leafcell.tcl`, and `model.sp` to your PDK.
2. Copy `configs/freepdk45.json` to `configs/<pdk>.json` and set:
   - `process.lib_path` / `lib_corner` / `model_include_format`
     (`spice_lib` for a single `.lib`, `spectre_include` for a Spectre model file,
     `hspice_ptm` for HSPICE `.inc` PTM models),
   - `transistor.nmos_name` / `pmos_name`,
   - `transistor.style` (`subckt_wl` or `finfet_nfin`) plus `include_nf` /
     `nfin_w_min_um` / `nfin_w_step_um` / `nfin_max` as appropriate,
   - `liberate.template_dir`, `liberate.output_loads_pF`, `co_opt`, and the
     `*_hidden` constant blocks (esp. `area_hidden` densities) for your node.
3. Run it via the general config, pointing at the new PDK:

   ```bash
   python3 scripts/main.py config.json configs/<pdk>.json
   ```

   (or set `"pdk": "configs/<pdk>.json"` in `config.json` and just run
   `python3 scripts/main.py`).

