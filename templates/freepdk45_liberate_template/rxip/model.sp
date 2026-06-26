*** Model include file — FreePDK45 (open-source) ***
* BSIM4 (level=54) Predictive Technology Models, nom corner.
simulator lang = spice

* ---- Raw PTM device models (drawn L = 50 nm) ----
.include "/path/to/FreePDK45/ncsu_basekit/models/hspice/tran_models/models_nom/NMOS_VTG.inc"
.include "/path/to/FreePDK45/ncsu_basekit/models/hspice/tran_models/models_nom/PMOS_VTG.inc"
.include "/path/to/FreePDK45/ncsu_basekit/models/hspice/tran_models/models_nom/NMOS_VTL.inc"
.include "/path/to/FreePDK45/ncsu_basekit/models/hspice/tran_models/models_nom/PMOS_VTL.inc"
.include "/path/to/FreePDK45/ncsu_basekit/models/hspice/tran_models/models_nom/NMOS_VTH.inc"
.include "/path/to/FreePDK45/ncsu_basekit/models/hspice/tran_models/models_nom/PMOS_VTH.inc"
.include "/path/to/FreePDK45/ncsu_basekit/models/hspice/tran_models/models_nom/NMOS_THKOX.inc"
.include "/path/to/FreePDK45/ncsu_basekit/models/hspice/tran_models/models_nom/PMOS_THKOX.inc"

* ---- Parameterized 4-terminal wrappers (drain gate source bulk) ----
.subckt nmos_vtg d g s b w=0.1u l=0.05u nf=1
M0 d g s b NMOS_VTG w=w l=l nf=nf
.ends nmos_vtg
.subckt pmos_vtg d g s b w=0.1u l=0.05u nf=1
M0 d g s b PMOS_VTG w=w l=l nf=nf
.ends pmos_vtg
.subckt nmos_vtl d g s b w=0.1u l=0.05u nf=1
M0 d g s b NMOS_VTL w=w l=l nf=nf
.ends nmos_vtl
.subckt pmos_vtl d g s b w=0.1u l=0.05u nf=1
M0 d g s b PMOS_VTL w=w l=l nf=nf
.ends pmos_vtl
.subckt nmos_vth d g s b w=0.1u l=0.05u nf=1
M0 d g s b NMOS_VTH w=w l=l nf=nf
.ends nmos_vth
.subckt pmos_vth d g s b w=0.1u l=0.05u nf=1
M0 d g s b PMOS_VTH w=w l=l nf=nf
.ends pmos_vth
.subckt nmos_thkox d g s b w=0.1u l=0.05u nf=1
M0 d g s b NMOS_THKOX w=w l=l nf=nf
.ends nmos_thkox
.subckt pmos_thkox d g s b w=0.1u l=0.05u nf=1
M0 d g s b PMOS_THKOX w=w l=l nf=nf
.ends pmos_thkox
