# Set the run directory.  Here we use PWD, but in a distributed 
# environment, it is recommended to directly specify the full path 
# instead of using "PWD"
set rundir $env(PWD)

# Create the directories Liberate will write to.
exec mkdir -p ${rundir}/LDB
exec mkdir -p ${rundir}/LIBRARY
exec mkdir -p ${rundir}/DATASHEET

### Define temperature and default voltage ###
# UCIe voltage swing range: 0.4V to 1.15V; set VDD_VALUE to test different swings
# FreePDK45 core device nominal supply is 1.1 V (drawn L = 50 nm).
# PDK typical for FreePDK45: 1.1V
set VDD_VALUE 1.1
set_operating_condition -voltage $VDD_VALUE -temp 25

## Load template information for each cell ##
source ${rundir}/template.tcl

set_var extsim_model_include ${rundir}/model.sp

source ${rundir}/define_leafcell.tcl

read_spice -format spectre ${rundir}/rxip.scs

## Characterize the library for NLDM (default), CCS and ECSM timing.

# ## user arc mode
# char_library -cells ${cells} -user_arcs_only
# Enables characterization of cells without using the Inside View algorithm. 
# This is useful for cells that contain analog or that are too large for digital 
# vector analysis using the Inside View algorithm. Using this option disables 
# the automatic arc-determination and vector-generation of Liberate. For I/O cells, 
# each of the arcs and associated logic conditions must be expressed explicitly using 
# the define_arc and define_leakage commands.
# In io mode, the -probe option is required for all constraint define_arc commands, including MPW.
char_library -ccs -ecsm -cells ${cells}

## auto detect mode
# char_library -ccs -ecsm -cells ${cells} 

## Save characterization database for post-processing ##
# write_ldb ${rundir}/LDB/io.ldb

## Generate a .lib with ccs, ecsm ###
# write_library -overwrite -ccs  ${rundir}/LIBRARY/io_ccs.lib
# write_library -overwrite -ecsm ${rundir}/LIBRARY/io_ecsm.lib
write_library -overwrite ${rundir}/LIBRARY/rxip_nldm.lib
write_verilog ${rundir}/LIBRARY/rxip.v

## Generate ascii datatsheet ###
write_datasheet -format text ${rundir}/DATASHEET/rxip
