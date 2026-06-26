"""
device.py — PDK-agnostic device-instantiation helpers.

Carries no foundry device names, model paths, or process constants; all
PDK-specific behaviour comes from the config and the per-PDK template directory.
The instantiation style is selected by cfg.transistor.style ("subckt_wl" or
"finfet_nfin").
"""

import os
from dataclasses import dataclass

SUBCKT_WL   = "subckt_wl"
FINFET_NFIN = "finfet_nfin"


@dataclass
class DeviceSpec:
    """Style parameters that tell the netlist generators how to write devices.

    Built from ``cfg.transistor``; all fields have defaults so that older
    configs (without the new keys) still load and behave as ``subckt_wl``.
    """
    style:          str   = SUBCKT_WL
    l_um:           float = 0.05
    nf:             int   = 1
    include_nf:     bool  = True     # subckt_wl: emit "nf=<nf>" on each instance
    w_max_um:       float = 900.0    # subckt_wl: split instances wider than this
    nfin_w_min_um:  float = 0.01     # finfet_nfin: width of a single-fin device
    nfin_w_step_um: float = 0.048    # finfet_nfin: incremental width per added fin
    nfin_max:       int   = 20       # finfet_nfin: max fins per instance (split above)

    @classmethod
    def from_cfg(cls, cfg) -> "DeviceSpec":
        t = cfg.transistor
        return cls(
            style          = getattr(t, "style", SUBCKT_WL),
            l_um           = t.l_um,
            nf             = t.nf,
            include_nf     = getattr(t, "include_nf", True),
            w_max_um       = t.w_max_um,
            nfin_w_min_um  = getattr(t, "nfin_w_min_um", 0.01),
            nfin_w_step_um = getattr(t, "nfin_w_step_um", 0.048),
            nfin_max       = getattr(t, "nfin_max", 20),
        )

    @property
    def is_finfet(self) -> bool:
        return self.style == FINFET_NFIN

    def l_nm(self) -> int:
        return int(round(self.l_um * 1000))

    def w_to_nfin(self, w_um: float) -> int:
        """Quantise a physical width (um) to an integer fin count."""
        return max(1, int(round((w_um - self.nfin_w_min_um) / self.nfin_w_step_um)) + 1)


# ---------------------------------------------------------------------------
# Per-PDK SPICE/Tcl data files (read verbatim from the template directory)
# ---------------------------------------------------------------------------

def read_pdk_file(template_dir: str, ip: str, filename: str) -> str:
    """Return the verbatim contents of ``<template_dir>/<ip>/<filename>``.

    ``ip`` is ``"txip"`` or ``"rxip"``.  Used for ``model.sp`` and
    ``define_leafcell.tcl`` so that all PDK-specific SPICE/Tcl text lives in
    the per-PDK template package rather than in Python.
    """
    path = os.path.join(template_dir, ip, filename)
    with open(path) as fh:
        return fh.read()


def model_include_text(template_dir: str, ip: str = "txip") -> str:
    """Return the PDK model-include text (contents of ``model.sp``)."""
    return read_pdk_file(template_dir, ip, "model.sp")


def define_leafcell_text(template_dir: str, ip: str = "txip") -> str:
    """Return the PDK Liberate leaf-cell definitions (``define_leafcell.tcl``)."""
    return read_pdk_file(template_dir, ip, "define_leafcell.tcl")
