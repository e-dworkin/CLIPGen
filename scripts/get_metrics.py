"""
get_metrics.py — extract power and timing metrics from Liberate datasheets and
Liberty (.lib) files.
"""

import os
import re
import sys
import math
from dataclasses import dataclass, field
from typing import Optional

# lib_parser lives in scripts/ alongside this file. main.py already puts
# SCRIPTS_DIR on sys.path before importing us, so a plain import works.
import lib_parser
import clocking


@dataclass
class LinkMetrics:
    tx_leak_avg_nW:        float = 0.0
    tx_delay_rr_avg_ns:    float = 0.0
    tx_delay_ff_avg_ns:    float = 0.0
    tx_slew_rr_avg_ns:     float = 0.0
    tx_slew_ff_avg_ns:     float = 0.0
    tx_sw_rise_avg_pJ:     float = 0.0
    tx_sw_fall_avg_pJ:     float = 0.0

    rx_leak_avg_nW:        float = 0.0
    rx_delay_rr_avg_ns:    float = 0.0
    rx_delay_ff_avg_ns:    float = 0.0
    rx_slew_rr_avg_ns:     float = 0.0
    rx_slew_ff_avg_ns:     float = 0.0
    rx_sw_rise_avg_pJ:     float = 0.0
    rx_sw_fall_avg_pJ:     float = 0.0

    alpha:                    float = 0.5
    E_tx_pJ_per_bit:          float = 0.0   # TX+EQ device energy only (from tx_only run)
    E_channel_pJ_per_bit:     float = 0.0   # channel RC dissipation (tx_channel - tx_only)
    E_rxpad_bump_pJ_per_bit:  float = 0.0   # RX bump+pad+ESD (tx_full - tx_channel), attributed to RX
    E_rx_pJ_per_bit:          float = 0.0   # RX device switching + E_rxpad_bump
    E_rx_cap_pJ_per_bit:      float = 0.0   # analytical re-attribution (0 when 3-tier active)
    E_term_pJ_per_bit:        float = 0.0
    E_ch_pJ_per_bit:          float = 0.0   # 0 when channel RC is inside TX netlist
    E_total_pJ_per_bit:       float = 0.0   # data lane + clocking (when enabled)
    E_data_pJ_per_bit:        float = 0.0   # data lane only (tx+channel+rx+term)

    # --- Clocking (forwarded-clock overhead; 0 when disabled) ---
    clocking_enabled:         bool  = False
    clock_mode:               str   = ""    # half-rate / quarter-rate
    clock_M:                  int   = 0     # data:clock ratio
    clock_fCK_GHz:            float = 0.0
    clock_deskew_required:    bool  = False
    clock_bumps:              int   = 0     # extra clock bumps (one direction)
    # energy allocated to each data lane (pJ per bit)
    E_clock_pJ_per_bit:       float = 0.0   # total clocking, per data bit per data lane
    E_clock_lane_pJ_per_bit:  float = 0.0
    E_clock_dll_pJ_per_bit:   float = 0.0
    E_clock_pi_pJ_per_bit:    float = 0.0
    E_clock_ser_pJ_per_bit:   float = 0.0
    E_clock_deser_pJ_per_bit: float = 0.0
    E_clock_dcc_pJ_per_bit:   float = 0.0
    # IP total
    clock_power_total_mW:     float = 0.0   # total clocking power for the whole IP

    # True when the channel RC ladder is embedded inside txip.scs.
    # In that mode: E_tx already contains the channel switching energy (no
    # double-count), and tx_delay already covers the full TX+channel path.
    channel_rc_integrated:    bool  = False

    total_delay_rr_ps:        float = 0.0
    total_delay_ff_ps:        float = 0.0

    # TX+RX delay only (excluding channel).  When channel_rc_integrated=True,
    # channel Elmore delay is subtracted from total_delay.  Used for UCIe
    # latency compliance checking.
    tx_rx_delay_rr_ps:        float = 0.0
    tx_rx_delay_ff_ps:        float = 0.0

    warnings:                 list  = field(default_factory=list)

    def report(self) -> str:
        lines = [
            "=== Link Metrics ===",
            "  -- TX --",
            f"  Leakage          : {self.tx_leak_avg_nW:.3f} nW",
            f"  Delay RR/FF      : {self.tx_delay_rr_avg_ns*1000:.2f} / {self.tx_delay_ff_avg_ns*1000:.2f} ps",
            f"  Slew  RR/FF      : {self.tx_slew_rr_avg_ns*1000:.2f} / {self.tx_slew_ff_avg_ns*1000:.2f} ps",
            f"  Sw energy R/F    : {self.tx_sw_rise_avg_pJ:.4f} / {self.tx_sw_fall_avg_pJ:.4f} pJ",
            "  -- RX --",
            f"  Leakage          : {self.rx_leak_avg_nW:.3f} nW",
            f"  Delay RR/FF      : {self.rx_delay_rr_avg_ns*1000:.2f} / {self.rx_delay_ff_avg_ns*1000:.2f} ps",
            f"  Slew  RR/FF      : {self.rx_slew_rr_avg_ns*1000:.2f} / {self.rx_slew_ff_avg_ns*1000:.2f} ps",
            f"  Sw energy R/F    : {self.rx_sw_rise_avg_pJ:.4f} / {self.rx_sw_fall_avg_pJ:.4f} pJ",
            "  -- Energy per bit (alpha=0.5) --",
            f"  TX device        : {self.E_tx_pJ_per_bit:.4f} pJ/bit",
            f"  Channel          : {self.E_channel_pJ_per_bit:.4f} pJ/bit",
            f"  RX bump+pad+ESD  : {self.E_rxpad_bump_pJ_per_bit:.4f} pJ/bit",
            f"  RX device        : {self.E_rx_pJ_per_bit - self.E_rxpad_bump_pJ_per_bit:.4f} pJ/bit",
            f"  Termination      : {self.E_term_pJ_per_bit:.4f} pJ/bit",
            f"  Clocking (/lane) : {self.E_clock_pJ_per_bit:.4f} pJ/bit"
                + ("" if self.clocking_enabled else "  (disabled)"),
            f"  TOTAL            : {self.E_total_pJ_per_bit:.4f} pJ/bit  (data + clock)",
            "  -- Total link delay --",
            f"  RR               : {self.total_delay_rr_ps:.2f} ps" +
                ("  (TX delay covers full TX+channel path)" if self.channel_rc_integrated else ""),
            f"  FF               : {self.total_delay_ff_ps:.2f} ps" +
                ("  (TX delay covers full TX+channel path)" if self.channel_rc_integrated else ""),
            "  -- TX+RX delay (excl. channel) --",
            f"  RR               : {self.tx_rx_delay_rr_ps:.2f} ps",
            f"  FF               : {self.tx_rx_delay_ff_ps:.2f} ps",
        ]
        if self.clocking_enabled:
            lines += [
                "  -- Clocking (UCIe forwarded clock) --",
                f"  Mode             : {self.clock_mode}  M={self.clock_M}  "
                f"fCK={self.clock_fCK_GHz:g} GHz  "
                f"deskew={'req' if self.clock_deskew_required else 'opt'}",
                f"  Per data lane    : {self.E_clock_pJ_per_bit:.4f} pJ/bit  "
                f"(lane {self.E_clock_lane_pJ_per_bit:.4f}, dll {self.E_clock_dll_pJ_per_bit:.4f}, "
                f"pi {self.E_clock_pi_pJ_per_bit:.4f}, ser {self.E_clock_ser_pJ_per_bit:.4f}, "
                f"deser {self.E_clock_deser_pJ_per_bit:.4f}, dcc {self.E_clock_dcc_pJ_per_bit:.4f})",
                f"  IP total clock   : {self.clock_power_total_mW:.3f} mW  "
                f"(+{self.clock_bumps} clock bumps, one direction)",
            ]
        if self.warnings:
            lines.append("  -- Warnings --")
            for w in self.warnings:
                lines.append(f"  ! {w}")
        return "\n".join(lines)


def _avg(values):
    return sum(values) / len(values) if values else 0.0


def _tx_only_delay_rr(tx_result, fallback_ns: float) -> float:
    """Return TX-only rise-rise delay in ns from the direct Liberate run.

    When a TX-only run was performed (tx_only_dir is set), use its measured
    delay.  Otherwise fall back to the regular TX delay (which already
    excludes the channel when channel_rc_integrated=False).
    """
    if getattr(tx_result, 'tx_only_dir', None) and tx_result.tx_only_delay_rr_ns > 0:
        return tx_result.tx_only_delay_rr_ns
    return fallback_ns


def _tx_only_delay_ff(tx_result, fallback_ns: float) -> float:
    """Return TX-only fall-fall delay in ns from the direct Liberate run."""
    if getattr(tx_result, 'tx_only_dir', None) and tx_result.tx_only_delay_ff_ns > 0:
        return tx_result.tx_only_delay_ff_ns
    return fallback_ns


def _parse_tx_datasheet(path: str, warnings: list) -> dict:
    result = {}
    if not os.path.exists(path):
        warnings.append(f"TX datasheet not found: {path}")
        return result

    with open(path) as f:
        content = f.read()

    # --- Leakage ---
    # FIX 1: Liberate writes the section as "Leakage" with a table header
    # "Leakage(nW)", not "Leakage power(nW)".  Match the table header row.
    m = re.search(
        r'Leakage\(nW\).*?\|\s*txip\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
        content, re.DOTALL
    )
    if m:
        result["tx_leak_min_nW"] = float(m.group(1))
        result["tx_leak_avg_nW"] = float(m.group(2))
        result["tx_leak_max_nW"] = float(m.group(3))
    else:
        warnings.append("TX: leakage power not found")

    # --- Delay: IN_i->PAD_i (RR and FF) ---
    for edge, key in (("RR", "tx_delay_rr"), ("FF", "tx_delay_ff")):
        rows = re.findall(
            rf'\|\s*txip\s*\|\s*IN_\d+->PAD_\d+\({edge}\)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
            content
        )
        if rows:
            firsts = [float(r[0]) for r in rows]
            mids   = [float(r[1]) for r in rows]
            lasts  = [float(r[2]) for r in rows]
            result[f"{key}_first_ns"] = _avg(firsts)
            result[f"{key}_mid_ns"]   = _avg(mids)
            result[f"{key}_last_ns"]  = _avg(lasts)
            result[f"{key}_avg_ns"]   = _avg(firsts + mids + lasts)
        else:
            warnings.append(f"TX: delay {edge} not found")

    # --- Output transition: PAD_i (RR and FF) ---
    # Liberate does not emit output transition tables in the text datasheet
    # for this cell type. We leave these as zero here; transition times will
    # be filled in from the .lib file later (see _parse_lib_transitions).
    for edge, key in (("RR", "tx_slew_rr"), ("FF", "tx_slew_ff")):
        rows = re.findall(
            rf'\|\s*txip\s*\|\s*PAD_\d+\({edge}\)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
            content
        )
        if rows:
            all_vals = [float(v) for r in rows for v in r]
            result[f"{key}_avg_ns"] = _avg(all_vals)

    # --- Switching power: to PAD_i rising / falling ---
    # Each arc section has two rows per pin: the first row is VDD
    # internal_power (real energy from supply), the second is VSS
    # (ground charge × VDD, for power-integrity analysis).
    # We need only the VDD row (row 0).
    for direction, key in (("rising", "tx_sw_rise"), ("falling", "tx_sw_fall")):
        sections = re.split(
            rf'Internal switching power\(pJ\)\s+to PAD_\d+ {direction}:',
            content
        )
        all_firsts, all_mids, all_lasts = [], [], []
        for sec in sections[1:]:
            sec_body = sec.split("Internal switching power")[0]
            rows = re.findall(
                r'\|\s*txip\s*\|\s*IN_\d+\s*\|\s*([\d.-]+)\s*\|\s*([\d.-]+)\s*\|\s*([\d.-]+)\s*\|',
                sec_body
            )
            if rows:
                # First row = VDD internal_power.
                vdd_row = rows[0]
                all_firsts.append(float(vdd_row[0]))
                all_mids.append(float(vdd_row[1]))
                all_lasts.append(float(vdd_row[2]))

        if all_firsts:
            result[f"{key}_first_pJ"] = _avg(all_firsts)
            result[f"{key}_mid_pJ"]   = _avg(all_mids)
            result[f"{key}_last_pJ"]  = _avg(all_lasts)
            result[f"{key}_avg_pJ"]   = _avg(all_firsts + all_mids + all_lasts)
        else:
            warnings.append(f"TX: switching power {direction} not found")

    return result


def _parse_rx_datasheet(path: str, warnings: list) -> dict:
    result = {}
    if not os.path.exists(path):
        warnings.append(f"RX datasheet not found: {path}")
        return result

    with open(path) as f:
        content = f.read()

    # --- Leakage ---
    # FIX 1 (RX): same header correction as TX.
    m = re.search(
        r'Leakage\(nW\).*?\|\s*rxip\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
        content, re.DOTALL
    )
    if m:
        result["rx_leak_min_nW"] = float(m.group(1))
        result["rx_leak_avg_nW"] = float(m.group(2))
        result["rx_leak_max_nW"] = float(m.group(3))
    else:
        warnings.append("RX: leakage power not found")

    # --- Delay: PAD_i->OUT_i (RR and FF) ---
    for edge, key in (("RR", "rx_delay_rr"), ("FF", "rx_delay_ff")):
        rows = re.findall(
            rf'\|\s*rxip\s*\|\s*PAD_\d+->OUT_\d+\({edge}\)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
            content
        )
        if rows:
            all_vals = [float(v) for r in rows for v in r]
            result[f"{key}_first_ns"] = _avg([float(r[0]) for r in rows])
            result[f"{key}_mid_ns"]   = _avg([float(r[1]) for r in rows])
            result[f"{key}_last_ns"]  = _avg([float(r[2]) for r in rows])
            result[f"{key}_avg_ns"]   = _avg(all_vals)
        else:
            warnings.append(f"RX: delay {edge} not found")

    # --- Output transition: OUT_i (RR and FF) ---
    # May be absent from the text datasheet; transition times will be filled
    # in from the .lib file later (see _parse_lib_transitions).
    for edge, key in (("RR", "rx_slew_rr"), ("FF", "rx_slew_ff")):
        rows = re.findall(
            rf'\|\s*rxip\s*\|\s*OUT_\d+\({edge}\)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|',
            content
        )
        if rows:
            all_vals = [float(v) for r in rows for v in r]
            result[f"{key}_avg_ns"] = _avg(all_vals)

    # --- Switching power: to OUT_i rising / falling ---
    # Each arc section has two rows per pin: the first row is VDD
    # internal_power, the second is VSS (power-integrity metric, not
    # additional energy).  We need only the VDD row (row 0).
    for direction, key in (("rising", "rx_sw_rise"), ("falling", "rx_sw_fall")):
        sections = re.split(
            rf'Internal switching power\(pJ\)\s+to OUT_\d+ {direction}:',
            content
        )
        all_firsts, all_mids, all_lasts = [], [], []
        for sec in sections[1:]:
            sec_body = sec.split("Internal switching power")[0]
            rows = re.findall(
                r'\|\s*rxip\s*\|\s*PAD_\d+\s*\|\s*([\d.-]+)\s*\|\s*([\d.-]+)\s*\|\s*([\d.-]+)\s*\|',
                sec_body
            )
            if rows:
                # First row = VDD internal_power (real energy from supply).
                # Second row = VSS (ground charge × VDD, for PI analysis).
                vdd_row = rows[0]
                all_firsts.append(float(vdd_row[0]))
                all_mids.append(float(vdd_row[1]))
                all_lasts.append(float(vdd_row[2]))

        if all_firsts:
            result[f"{key}_first_pJ"] = _avg(all_firsts)
            result[f"{key}_mid_pJ"]   = _avg(all_mids)
            result[f"{key}_last_pJ"]  = _avg(all_lasts)
            result[f"{key}_avg_pJ"]   = _avg(all_firsts + all_mids + all_lasts)
        else:
            warnings.append(f"RX: switching power {direction} not found")

    return result


def _parse_lib_transitions(lib_path: str, prefix: str, warnings: list) -> dict:
    """
    Extract rise/fall transition times from a Liberty .lib file.

    Parameters
    ----------
    lib_path : str
        Path to the .lib file (e.g. txip_nldm.lib or rxip_nldm.lib).
    prefix : str
        "tx" or "rx" — used as key prefix in the returned dict.
    warnings : list
        Warnings list to append to if parsing fails.

    Returns
    -------
    dict with keys like "{prefix}_slew_rr_avg_ns", "{prefix}_slew_ff_avg_ns".
    """
    result = {}
    if not os.path.exists(lib_path):
        warnings.append(f"{prefix.upper()}: .lib file not found: {lib_path}")
        return result

    try:
        data = lib_parser.extract_transition_times(lib_path)
        if data["avg_rise_transition_ns"] > 0:
            result[f"{prefix}_slew_rr_avg_ns"] = data["avg_rise_transition_ns"]
        if data["avg_fall_transition_ns"] > 0:
            result[f"{prefix}_slew_ff_avg_ns"] = data["avg_fall_transition_ns"]
    except Exception as e:
        warnings.append(f"{prefix.upper()}: failed to parse .lib transitions: {e}")

    return result


def _extract_from_lib_charlib(lib_path: str, prefix: str, warnings: list) -> dict:
    """
    Extract all metrics from a CharLib Liberty .lib file.

    prefix : "tx" or "rx" — used as the key prefix.
    Returns a dict with the same keys as _parse_tx_datasheet / _parse_rx_datasheet.
    All metrics come from the .lib; there is no DATASHEET for CharLib runs.
    """
    result = {}
    if not os.path.exists(lib_path):
        warnings.append(f"{prefix.upper()}: .lib file not found: {lib_path}")
        return result

    try:
        timing = lib_parser.parse_lib_timing(lib_path)
        result[f"{prefix}_delay_rr_avg_ns"] = timing.get("avg_cell_rise_ns",        0.0)
        result[f"{prefix}_delay_ff_avg_ns"] = timing.get("avg_cell_fall_ns",        0.0)
        result[f"{prefix}_slew_rr_avg_ns"]  = timing.get("avg_rise_transition_ns",  0.0)
        result[f"{prefix}_slew_ff_avg_ns"]  = timing.get("avg_fall_transition_ns",  0.0)
    except Exception as e:
        warnings.append(f"{prefix.upper()}: failed to parse .lib timing: {e}")

    try:
        power = lib_parser.parse_lib_power(lib_path)
        rise_vals = [v for arc in power.get("rise_power", [])
                     for row in arc.get("values", []) for v in row]
        fall_vals = [v for arc in power.get("fall_power", [])
                     for row in arc.get("values", []) for v in row]
        if rise_vals:
            result[f"{prefix}_sw_rise_avg_pJ"] = _avg(rise_vals)
        else:
            warnings.append(f"{prefix.upper()}: rise_power not found in .lib")
        if fall_vals:
            result[f"{prefix}_sw_fall_avg_pJ"] = _avg(fall_vals)
        else:
            warnings.append(f"{prefix.upper()}: fall_power not found in .lib")
    except Exception as e:
        warnings.append(f"{prefix.upper()}: failed to parse .lib power: {e}")

    try:
        result[f"{prefix}_leak_avg_nW"] = lib_parser.parse_cell_leakage_power(lib_path)
    except Exception as e:
        warnings.append(f"{prefix.upper()}: failed to parse .lib leakage: {e}")

    return result


def _parse_rx_lib_delay_min(lib_path: str, warnings: list) -> dict:
    """
    Extract RX delay at minimum input slew and minimum output load (index [0][0])
    from the Liberty .lib file.

    This selects the cell_rise[0][0] and cell_fall[0][0] values, corresponding
    to the smallest input slew (index_1[0]) and smallest load cap (index_2[0]).
    For RX, this represents the typical operating point: the RX sees a known
    input slew from the TX/channel and drives a small internal logic load.

    The result is averaged across all timing arcs (PAD_0->OUT_0, PAD_1->OUT_1, ...).

    Returns
    -------
    dict with keys "rx_delay_rr_avg_ns" and "rx_delay_ff_avg_ns".
    """
    result = {}
    if not os.path.exists(lib_path):
        warnings.append(f"RX: .lib file not found for delay extraction: {lib_path}")
        return result

    try:
        data = lib_parser.parse_lib_timing(lib_path)

        # cell_rise [0][0] across all arcs → RX rise-rise delay
        rise_vals = []
        for arc in data.get("cell_rise", []):
            vals = arc.get("values", [])
            if vals and vals[0]:
                rise_vals.append(vals[0][0])
        if rise_vals:
            result["rx_delay_rr_avg_ns"] = sum(rise_vals) / len(rise_vals)

        # cell_fall [0][0] across all arcs → RX fall-fall delay
        fall_vals = []
        for arc in data.get("cell_fall", []):
            vals = arc.get("values", [])
            if vals and vals[0]:
                fall_vals.append(vals[0][0])
        if fall_vals:
            result["rx_delay_ff_avg_ns"] = sum(fall_vals) / len(fall_vals)

    except Exception as e:
        warnings.append(f"RX: failed to parse .lib delay at [0][0]: {e}")

    return result


def _compute_energy(tx, rx, ch_result, term_result, cfg,
                    channel_rc_integrated: bool = False, alpha=0.5,
                    rx_cap_in_pF: float = 0.0,
                    tx_only=None, tx_channel=None):
    vdd          = cfg.process.vdd
    C_ch_fF      = ch_result.total_shunt_C_fF

    tx_rise = tx.get("tx_sw_rise_avg_pJ", 0.0)
    tx_fall = tx.get("tx_sw_fall_avg_pJ", 0.0)
    E_tx_full = alpha * (tx_rise + tx_fall) / 2.0

    rx_rise = rx.get("rx_sw_rise_avg_pJ", 0.0)
    rx_fall = rx.get("rx_sw_fall_avg_pJ", 0.0)
    E_rx_device = alpha * (rx_rise + rx_fall) / 2.0

    E_channel_pJ   = 0.0
    E_rxpad_bump   = 0.0
    E_rx_cap       = 0.0
    E_tx_device    = E_tx_full  # default: no separation available

    if channel_rc_integrated and tx_only is not None and tx_channel is not None:
        # --- 3-tier measured breakdown ---
        # E_tx_device  = tx_only run    (TX+EQ, no channel, no RX parasitics)
        # E_tx_channel = tx_channel run (TX+EQ+channel, no RX bump/pad/ESD)
        # E_tx_full    = tx run         (TX+EQ+channel+RX bump/pad/ESD)
        only_rise = tx_only.get("tx_sw_rise_avg_pJ", 0.0)
        only_fall = tx_only.get("tx_sw_fall_avg_pJ", 0.0)
        ch_rise   = tx_channel.get("tx_sw_rise_avg_pJ", 0.0)
        ch_fall   = tx_channel.get("tx_sw_fall_avg_pJ", 0.0)
        E_tx_device   = alpha * (only_rise + only_fall) / 2.0
        E_tx_plus_ch  = alpha * (ch_rise   + ch_fall)   / 2.0
        E_channel_pJ  = max(0.0, E_tx_plus_ch - E_tx_device)
        E_rxpad_bump  = max(0.0, E_tx_full    - E_tx_plus_ch)
    elif channel_rc_integrated and rx_cap_in_pF > 0.0:
        # --- Fallback: analytical re-attribution (no tx_channel run) ---
        E_rx_cap   = alpha * rx_cap_in_pF * (vdd ** 2) / 2.0
        E_tx_device = max(0.0, E_tx_full - E_rx_cap)
        E_rxpad_bump = E_rx_cap
    elif not channel_rc_integrated:
        # Channel RC is external; add analytically
        C_ch_F  = C_ch_fF * 1e-15
        E_channel_pJ = alpha * C_ch_F * (vdd ** 2) / 2 * 1e12

    E_rx_total = E_rx_device + E_rxpad_bump

    if term_result is not None and not getattr(term_result, 'use_termination', True):
        E_term_pJ = term_result.E_term_pJ
    else:
        E_term_pJ = 0.0

    return {
        "E_tx_pJ_per_bit":       E_tx_device,
        "E_channel_pJ_per_bit":  E_channel_pJ,
        "E_rxpad_bump_pJ_per_bit": E_rxpad_bump,
        "E_rx_pJ_per_bit":       E_rx_total,
        "E_rx_cap_pJ_per_bit":   E_rx_cap,
        "E_term_pJ_per_bit":     E_term_pJ,
        "E_ch_pJ_per_bit":       E_channel_pJ,
        "E_total_pJ_per_bit":    E_tx_device + E_channel_pJ + E_rx_total + E_term_pJ,
    }


def extract(cfg, ch_result, eq_result, term_result, tx_result, run_dir: str) -> LinkMetrics:
    """
    Extract power and timing metrics from Liberate datasheets.

    tx_result.channel_rc_integrated controls whether the channel RC energy is
    already captured inside the TX Liberate switching power (True) or needs to
    be added analytically (False, legacy behaviour).
    """
    warnings = []

    channel_rc_integrated = getattr(tx_result, 'channel_rc_integrated', False)
    use_charlib = getattr(tx_result, 'backend', 'liberate') == "charlib"

    tx_ds_path = os.path.join(run_dir, "tx", "DATASHEET", "txip.txt")
    rx_ds_path = os.path.join(run_dir, "rx", "DATASHEET", "rxip.txt")

    tx_lib_path = os.path.join(run_dir, "tx", "LIBRARY", "txip_nldm.lib")
    rx_lib_path = os.path.join(run_dir, "rx", "LIBRARY", "rxip_nldm.lib")

    rx = _extract_from_lib_charlib(rx_lib_path, "rx", warnings) if use_charlib \
        else _parse_rx_datasheet(rx_ds_path, warnings)

    # `tx` always comes from the main run's "tx/" directory — the most-complete
    # result available (full topology, channel+RX-bump/pad, when
    # channel_rc_integrated; no-channel otherwise), mirroring the Liberate
    # path's tx_ds_path exactly. The tx_only/tx_channel 3-tier breakdown below
    # is a separate, additional decomposition read from their own directories.
    tx = _extract_from_lib_charlib(tx_lib_path, "tx", warnings) if use_charlib \
        else _parse_tx_datasheet(tx_ds_path, warnings)

    # --- Fill in transition times from .lib files (Liberate only; CharLib path already
    # populated via _extract_from_lib_charlib above) ---
    if tx.get("tx_slew_rr_avg_ns", 0.0) == 0.0:
        tx_lib_trans = _parse_lib_transitions(tx_lib_path, "tx", warnings)
        tx.update(tx_lib_trans)

    if rx.get("rx_slew_rr_avg_ns", 0.0) == 0.0:
        rx_lib_trans = _parse_lib_transitions(rx_lib_path, "rx", warnings)
        rx.update(rx_lib_trans)

    # --- Override RX delay with .lib [0][0] (min input slew, min load cap) ---
    # The datasheet averages across all slew/load combos.  For RX, the
    # relevant operating point is minimum input slew (driven by TX/channel)
    # and minimum output load (small internal logic fanout).
    rx_lib_delay = _parse_rx_lib_delay_min(rx_lib_path, warnings)
    if rx_lib_delay:
        rx.update(rx_lib_delay)

    # When channel RC is embedded in the TX netlist, tx_result.load_pF is the
    # RX device input capacitance used as the external CharLib/Liberate load.
    # The energy to charge that cap is embedded in E_tx and is re-attributed to
    # E_rx inside _compute_energy so that E_rx reflects all RX-side dissipation.
    rx_cap_in_pF = 0.0
    if channel_rc_integrated:
        rx_cap_in_pF = getattr(tx_result, 'load_pF', 0.0)

    # 3-tier breakdown: tx_only (no channel) and tx_channel (channel, no RX
    # bump/pad) comparison runs, gated on channel_rc_integrated exactly like
    # the run functions in tx.py that produce them. CharLib reads its own
    # tx_only/tx_channel .lib files directly; Liberate parses DATASHEET files
    # from the directories tx_result.tx_only_dir/tx_channel_dir point to.
    tx_only_data    = None
    tx_channel_data = None
    if channel_rc_integrated:
        if use_charlib:
            tx_only_lib = os.path.join(run_dir, "tx_only",   "LIBRARY", "txip_only_nldm.lib")
            tx_ch_lib   = os.path.join(run_dir, "tx_channel", "LIBRARY", "txip_ch_nldm.lib")
            tx_only_data    = _extract_from_lib_charlib(tx_only_lib, "tx", warnings)
            tx_channel_data = _extract_from_lib_charlib(tx_ch_lib,   "tx", warnings)
        else:
            tx_only_dir    = getattr(tx_result, 'tx_only_dir',    None)
            tx_channel_dir = getattr(tx_result, 'tx_channel_dir', None)
            if tx_only_dir:
                tx_only_ds = os.path.join(tx_only_dir, "DATASHEET", "txip.txt")
                tx_only_data = _parse_tx_datasheet(tx_only_ds, warnings)
            if tx_channel_dir:
                tx_ch_ds = os.path.join(tx_channel_dir, "DATASHEET", "txip.txt")
                tx_channel_data = _parse_tx_datasheet(tx_ch_ds, warnings)

    energy = _compute_energy(tx, rx, ch_result, term_result, cfg,
                             channel_rc_integrated=channel_rc_integrated,
                             rx_cap_in_pF=rx_cap_in_pF,
                             tx_only=tx_only_data,
                             tx_channel=tx_channel_data)

    # --- Clocking overhead: reuse the data-lane energy/bit as E_lane(R) ---
    # E_data is the data lane only (tx+channel+rx+term); clocking is added on
    # top (0 when clocking.enabled is False).  IP-total clock power = per-lane
    # clock energy/bit × data rate × number of data lanes.
    E_data  = energy["E_total_pJ_per_bit"]
    clk     = clocking.get_clocking(cfg, E_data, link_energy_pJ=E_data)
    E_clock = clk.E_clock_total_pJ
    clock_power_total_mW = cfg.link.lane_count * E_clock * cfg.link.data_rate_Gbps
    E_total_with_clock   = E_data + E_clock

    alpha = 0.5

    tx_delay_rr = tx.get("tx_delay_rr_avg_ns", 0.0)
    tx_delay_ff = tx.get("tx_delay_ff_avg_ns", 0.0)
    rx_delay_rr = rx.get("rx_delay_rr_avg_ns", 0.0)
    rx_delay_ff = rx.get("rx_delay_ff_avg_ns", 0.0)

    return LinkMetrics(
        tx_leak_avg_nW      = tx.get("tx_leak_avg_nW",    0.0),
        tx_delay_rr_avg_ns  = tx_delay_rr,
        tx_delay_ff_avg_ns  = tx_delay_ff,
        tx_slew_rr_avg_ns   = tx.get("tx_slew_rr_avg_ns", 0.0),
        tx_slew_ff_avg_ns   = tx.get("tx_slew_ff_avg_ns", 0.0),
        tx_sw_rise_avg_pJ   = tx.get("tx_sw_rise_avg_pJ", 0.0),
        tx_sw_fall_avg_pJ   = tx.get("tx_sw_fall_avg_pJ", 0.0),

        rx_leak_avg_nW      = rx.get("rx_leak_avg_nW",    0.0),
        rx_delay_rr_avg_ns  = rx_delay_rr,
        rx_delay_ff_avg_ns  = rx_delay_ff,
        rx_slew_rr_avg_ns   = rx.get("rx_slew_rr_avg_ns", 0.0),
        rx_slew_ff_avg_ns   = rx.get("rx_slew_ff_avg_ns", 0.0),
        rx_sw_rise_avg_pJ   = rx.get("rx_sw_rise_avg_pJ", 0.0),
        rx_sw_fall_avg_pJ   = rx.get("rx_sw_fall_avg_pJ", 0.0),

        alpha                  = alpha,
        E_tx_pJ_per_bit        = energy["E_tx_pJ_per_bit"],
        E_channel_pJ_per_bit   = energy["E_channel_pJ_per_bit"],
        E_rxpad_bump_pJ_per_bit = energy["E_rxpad_bump_pJ_per_bit"],
        E_rx_pJ_per_bit        = energy["E_rx_pJ_per_bit"],
        E_rx_cap_pJ_per_bit    = energy["E_rx_cap_pJ_per_bit"],
        E_term_pJ_per_bit      = energy["E_term_pJ_per_bit"],
        E_ch_pJ_per_bit        = energy["E_ch_pJ_per_bit"],
        E_total_pJ_per_bit     = E_total_with_clock,
        E_data_pJ_per_bit      = E_data,

        clocking_enabled         = clk.enabled,
        clock_mode               = clk.mode,
        clock_M                  = clk.M,
        clock_fCK_GHz            = clk.f_clk_GHz,
        clock_deskew_required    = clk.deskew_required,
        clock_bumps              = clk.clock_bumps_total,
        E_clock_pJ_per_bit       = E_clock,
        E_clock_lane_pJ_per_bit  = clk.E_clk_lane_pJ,
        E_clock_dll_pJ_per_bit   = clk.E_dll_pJ,
        E_clock_pi_pJ_per_bit    = clk.E_phase_int_pJ,
        E_clock_ser_pJ_per_bit   = clk.E_serializer_pJ,
        E_clock_deser_pJ_per_bit = clk.E_deserializer_pJ,
        E_clock_dcc_pJ_per_bit   = clk.E_dcc_pJ,
        clock_power_total_mW     = clock_power_total_mW,

        channel_rc_integrated  = channel_rc_integrated,

        # When channel_rc_integrated=True, tx_delay already covers the full
        # TX+channel propagation path (IN_i -> far-end PAD_i through the
        # embedded RC ladder).  total_delay = tx_delay + rx_delay is the
        # complete link latency in both modes.
        #
        # TX+RX-only delay (for UCIe latency constraint): uses the direct
        # TX-only Liberate measurement (TX+EQ, no channel) when available.
        # Falls back to the regular tx_delay when channel is not embedded.
        total_delay_rr_ps      = (tx_delay_rr + rx_delay_rr) * 1000.0,
        total_delay_ff_ps      = (tx_delay_ff + rx_delay_ff) * 1000.0,

        tx_rx_delay_rr_ps      = (_tx_only_delay_rr(tx_result, tx_delay_rr) + rx_delay_rr) * 1000.0,
        tx_rx_delay_ff_ps      = (_tx_only_delay_ff(tx_result, tx_delay_ff) + rx_delay_ff) * 1000.0,

        warnings               = warnings,
    )