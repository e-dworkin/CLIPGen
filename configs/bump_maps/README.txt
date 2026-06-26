Bump map files for chiplet link IP
==================================

Each .txt file describes the bump grid for one chiplet link IP block.  The
file is consumed by scripts/area.py (via cfg.layout.bump_map_file) when
cfg.layout.bump_map_enabled is true.

File format
-----------
  - One row of the physical grid per non-blank text line.
  - Tokens separated by whitespace and/or commas.
  - '#' starts a line comment.
  - Rows may have different lengths; shorter rows are right-padded with '-'.
  - Token set (case-insensitive):
        tx     signal bump — TX lane pad          (must equal lane_count)
        rx     signal bump — RX lane pad          (must equal lane_count)
        vdd    power bump
        vss    ground bump
        other  auxiliary / reserved bump
        -      empty slot (placeholder)
        .      empty slot (alias for '-')

Validation (performed by scripts/area.py)
-----------------------------------------
  - All tokens must be legal (see list above).
  - The count of 'tx' bumps must equal cfg.link.lane_count.
  - The count of 'rx' bumps must equal cfg.link.lane_count.
  - vdd / vss / other / empty counts are unconstrained.

Naming convention
-----------------
Suggested (not enforced):
    ucie_<package>_<lane_count>lane[_<variant>].txt

  ucie_silicon_8lane.txt     — UCIe-1.0 advanced package, 8 tx + 8 rx
  ucie_organic_16lane.txt    — UCIe-1.0 standard package, 16 tx + 16 rx
  ...

Example (4 tx + 4 rx, silicon-interposer style)
-----------------------------------------------
    # Row 1: signal row — TX bumps on the left, RX bumps on the right
    tx  -  tx  -  rx  -  rx  -
    -  vdd  -  vdd  -  vdd  -  vdd
    tx  -  tx  -  rx  -  rx  -
    -  vss  -  vss  -  vss  -  vss

That file has 4 tx bumps, 4 rx bumps, 4 vdd bumps, 4 vss bumps, 16 empty
slots, in a 4 x 8 grid.  Set cfg.link.lane_count = 4 to use it.
