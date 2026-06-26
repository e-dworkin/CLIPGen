# define leaf cell
# FreePDK45 4-terminal BSIM4 wrapper subckts (drain gate source bulk).
# The wrapper subckts are defined in model.sp; Liberate treats them as
# MOS primitives while Spectre simulates the underlying PTM device.
define_leafcell -type nmos -pin_position {0 1 2 3} { nmos_vtg nmos_vtl nmos_vth nmos_thkox }
define_leafcell -type pmos -pin_position {0 1 2 3} { pmos_vtg pmos_vtl pmos_vth pmos_thkox }
