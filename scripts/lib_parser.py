"""
lib_parser.py — extract timing tables from NLDM Liberty (.lib) files.

Public API:
    parse_lib_timing(lib_path, cell_name=None) -> dict
"""

import re
from typing import Dict, List, Optional


def _extract_balanced_blocks(content: str, keyword_pattern: str) -> List[str]:
    """Find all `keyword { ... }` blocks with properly balanced braces.

    Uses a brace-depth counter rather than a regex, so nested `{...}` inside the
    block are handled correctly.  Returns a list of the inner contents (the text
    between the outermost `{` and its matching `}`).
    """
    results = []
    for m in re.finditer(keyword_pattern + r'\s*\{', content):
        start = m.end()   # position just after the opening '{'
        depth = 1
        i = start
        while i < len(content) and depth > 0:
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
            i += 1
        if depth == 0:
            results.append(content[start:i - 1])  # content between { and }
    return results


def _parse_values_block(text: str) -> List[List[float]]:
    """Parse a Liberty 'values' block into a 2D list of floats.

    Example input:
        values ( \\
            "1.03366", \\
            "1.03365" \\
        );
    Each quoted string is one row (comma-separated values within a row).
    """
    m = re.search(r'values\s*\((.*?)\)\s*;', text, re.DOTALL)
    if not m:
        return []
    body = m.group(1)
    rows = []
    for quoted in re.findall(r'"([^"]*)"', body):
        row = [float(v.strip()) for v in quoted.split(",") if v.strip()]
        rows.append(row)
    return rows


def _parse_index(text: str, idx_name: str) -> List[float]:
    """Parse index_1 or index_2 from a timing group."""
    m = re.search(rf'{idx_name}\s*\(\s*"([^"]*)"\s*\)', text)
    if not m:
        return []
    return [float(v.strip()) for v in m.group(1).split(",") if v.strip()]


def parse_lib_timing(lib_path: str, cell_name: Optional[str] = None) -> Dict:
    """
    Parse timing data from a Liberty .lib file.

    Parameters
    ----------
    lib_path : str
        Path to the .lib file.
    cell_name : str, optional
        If given, only parse timing arcs for this cell. If None, parse the
        first cell found.

    Returns
    -------
    dict with keys:
        "cell_rise", "cell_fall", "rise_transition", "fall_transition"
            Each is a list of dicts, one per timing arc, with keys:
                "related_pin": str
                "pin": str
                "index_1": list of float
                "index_2": list of float
                "values": 2D list of float
        "avg_rise_transition_ns": float  (average across all arcs/slews/loads)
        "avg_fall_transition_ns": float
        "avg_cell_rise_ns": float
        "avg_cell_fall_ns": float
    """
    with open(lib_path, "r") as f:
        content = f.read()

    result = {
        "cell_rise": [],
        "cell_fall": [],
        "rise_transition": [],
        "fall_transition": [],
        "avg_rise_transition_ns": 0.0,
        "avg_fall_transition_ns": 0.0,
        "avg_cell_rise_ns": 0.0,
        "avg_cell_fall_ns": 0.0,
    }

    # Find all timing groups by splitting on 'timing ()' blocks using
    # balanced-brace matching.  A simple non-greedy regex stops at the first
    # inner nested '}' (e.g. the close of 'cell_rise'), truncating the block
    # before 'rise_transition' / 'fall_transition' are reached.
    timing_groups = _extract_balanced_blocks(content, r'timing\s*\([^)]*\)')

    for group_name in ("cell_rise", "cell_fall", "rise_transition", "fall_transition"):
        all_values = []
        for tg in timing_groups:
            # Find the specific timing table within this timing group
            pattern = rf'{group_name}\s*\([^)]*\)\s*\{{(.*?)\}}'
            m = re.search(pattern, tg, re.DOTALL)
            if not m:
                continue
            block = m.group(1)
            index_1 = _parse_index(block, "index_1")
            index_2 = _parse_index(block, "index_2")
            values = _parse_values_block(block)

            # Extract related_pin from timing group
            rp_match = re.search(r'related_pin\s*:\s*"?([^"\s;]+)"?', tg)
            related_pin = rp_match.group(1) if rp_match else ""

            result[group_name].append({
                "related_pin": related_pin,
                "index_1": index_1,
                "index_2": index_2,
                "values": values,
            })

            # Collect all values for averaging
            for row in values:
                all_values.extend(row)

        if all_values:
            result[f"avg_{group_name}_ns"] = sum(all_values) / len(all_values)

    return result


def extract_transition_times(lib_path: str) -> Dict[str, float]:
    """
    Convenience function: extract average rise/fall transition times from a .lib file.

    Returns
    -------
    dict with keys:
        "avg_rise_transition_ns": float
        "avg_fall_transition_ns": float
        "avg_cell_rise_ns": float
        "avg_cell_fall_ns": float
        "mid_rise_transition_ns": float  (value at middle slew/load index)
        "mid_fall_transition_ns": float
    """
    data = parse_lib_timing(lib_path)

    result = {
        "avg_rise_transition_ns": data["avg_rise_transition_ns"],
        "avg_fall_transition_ns": data["avg_fall_transition_ns"],
        "avg_cell_rise_ns": data["avg_cell_rise_ns"],
        "avg_cell_fall_ns": data["avg_cell_fall_ns"],
        "mid_rise_transition_ns": 0.0,
        "mid_fall_transition_ns": 0.0,
    }

    # Extract mid-index value (middle slew, first or middle load)
    for group_name, key in [("rise_transition", "mid_rise_transition_ns"),
                            ("fall_transition", "mid_fall_transition_ns")]:
        arcs = data[group_name]
        if arcs:
            # Use first arc (all lanes are identical)
            arc = arcs[0]
            values = arc["values"]
            if values:
                mid_row = len(values) // 2
                mid_col = len(values[0]) // 2 if values[0] else 0
                result[key] = values[mid_row][mid_col]

    return result


# ---------------------------------------------------------------------------
# Internal-power table parsing (rise_power / fall_power from .lib)
# ---------------------------------------------------------------------------

def parse_lib_power(lib_path: str, pg_pin: str = "VDD") -> Dict:
    """
    Parse internal_power tables (rise_power / fall_power) from a Liberty .lib.

    Only power groups with ``related_pg_pin : <pg_pin>`` are collected
    (default "VDD" — the supply-side switching energy used by get_metrics).

    Returns
    -------
    dict with keys:
        "rise_power" : list of arc-dicts  [{related_pin, index_1, index_2, values}, ...]
        "fall_power" : list of arc-dicts
    where ``values`` is a 2-D list [slew_row][load_col] of floats (pJ).
    """
    with open(lib_path, "r") as f:
        content = f.read()

    result: Dict[str, list] = {"rise_power": [], "fall_power": []}

    # Find all internal_power () { ... } blocks
    ip_blocks = _extract_balanced_blocks(content, r'internal_power\s*\([^)]*\)')

    for ip in ip_blocks:
        # Filter by related_pg_pin
        pg_match = re.search(r'related_pg_pin\s*:\s*(\w+)', ip)
        if pg_match and pg_match.group(1) != pg_pin:
            continue

        rp_match = re.search(r'related_pin\s*:\s*"?([^"\s;]+)"?', ip)
        related_pin = rp_match.group(1) if rp_match else ""

        for group_name in ("rise_power", "fall_power"):
            pattern = rf'{group_name}\s*\([^)]*\)\s*\{{(.*?)\}}'
            m = re.search(pattern, ip, re.DOTALL)
            if not m:
                continue
            block = m.group(1)
            index_1 = _parse_index(block, "index_1")
            index_2 = _parse_index(block, "index_2")
            values  = _parse_values_block(block)

            result[group_name].append({
                "related_pin": related_pin,
                "index_1": index_1,
                "index_2": index_2,
                "values": values,
            })

    return result


# ---------------------------------------------------------------------------
# Pin capacitance extraction
# ---------------------------------------------------------------------------

def parse_pin_capacitance(lib_path: str, pin_pattern: Optional[str] = None) -> Dict[str, float]:
    """
    Extract input-pin capacitance values from a Liberty .lib file.

    Parameters
    ----------
    lib_path : str
        Path to the .lib file.
    pin_pattern : str, optional
        Regex pattern to match pin names (e.g. ``r"PAD_\\d+"`` or ``"in"``).
        If None, all input pins with a ``capacitance`` attribute are returned.

    Returns
    -------
    dict mapping pin_name → capacitance (in pF, library unit).
    """
    with open(lib_path, "r") as f:
        content = f.read()

    caps: Dict[str, float] = {}

    # Find all pin ( <name> ) { ... } blocks
    pin_blocks = re.finditer(r'pin\s*\(\s*(\w+)\s*\)\s*\{', content)
    for pm in pin_blocks:
        pin_name = pm.group(1)
        if pin_pattern and not re.search(pin_pattern, pin_name):
            continue

        # Extract the balanced block content
        start = pm.end()
        depth = 1
        i = start
        while i < len(content) and depth > 0:
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
            i += 1
        block = content[start:i - 1] if depth == 0 else ""

        # Only input pins
        dir_match = re.search(r'direction\s*:\s*(\w+)', block)
        if dir_match and dir_match.group(1) != "input":
            continue

        cap_match = re.search(r'\bcapacitance\s*:\s*([\d.eE+-]+)', block)
        if cap_match:
            caps[pin_name] = float(cap_match.group(1))

    return caps


# ---------------------------------------------------------------------------
# Leakage power extraction
# ---------------------------------------------------------------------------

def parse_cell_leakage_power(lib_path: str, cell_name: Optional[str] = None) -> float:
    """
    Parse leakage_power blocks from a Liberty .lib file. Not needed for Liberate since
    leakage results are gleaned from the DATASHEET.txt. This function is needed for
    CharLib since it reports leakage power only through the .lib output.

    Parameters
    ----------
    lib_path : str
        Path to the .lib file.
    cell_name : str, optional
        If given, only parse leakage_power blocks inside the named cell block.
        If None, parse all leakage_power blocks in the file.

    Returns
    -------
    float
        Average of all ``value : <float>`` entries found (in nW, library unit).
        Returns 0.0 if no leakage_power blocks are found.
    """
    with open(lib_path, "r") as f:
        content = f.read()

    if cell_name is not None:
        # Find the matching cell block
        cell_blocks = _extract_balanced_blocks(content, rf'cell\s*\(\s*{re.escape(cell_name)}\s*\)')
        if not cell_blocks:
            return 0.0
        search_text = cell_blocks[0]
    else:
        search_text = content

    lp_blocks = _extract_balanced_blocks(search_text, r'leakage_power\s*\([^)]*\)')
    values = []
    for block in lp_blocks:
        m = re.search(r'\bvalue\s*:\s*([\d.eE+\-]+)', block)
        if m:
            try:
                values.append(float(m.group(1)))
            except ValueError:
                pass

    return sum(values) / len(values) if values else 0.0
