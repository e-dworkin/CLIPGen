set_var slew_lower_rise 0.2
set_var slew_upper_rise 0.8
set_var slew_lower_fall 0.2
set_var slew_upper_fall 0.8
set_var measure_slew_lower_rise 0.2
set_var measure_slew_upper_rise 0.8
set_var measure_slew_lower_fall 0.2
set_var measure_slew_upper_fall 0.8

set_units -capacitance 1pF
set_units -leakage_power 1nW
set_units -timing 1ns

set cells { txip }

define_template -type delay \
        -index_1        {0.250 0.500 0.750 1.250 1.500} \
        -index_2        {0.1867} \
        delay_template_5x1

define_template -type power \
        -index_1        {0.250 0.500 0.750 1.250 1.500} \
        -index_2        {0.1867} \
        power_template_5x1

define_template -type constraint \
        -index_1  {0.250  0.750 1.500} \
        -index_2  {0.250  0.750 1.500} \
        constraint_template_3x3

define_cell \
        -input  {IN_0  IN_1  IN_2  IN_3  \
                 IN_4  IN_5  IN_6  IN_7  \
                 IN_8  IN_9  IN_10 IN_11 \
                 IN_12 IN_13 IN_14 IN_15} \
        -output {PAD_0  PAD_1  PAD_2  PAD_3  \
                 PAD_4  PAD_5  PAD_6  PAD_7  \
                 PAD_8  PAD_9  PAD_10 PAD_11 \
                 PAD_12 PAD_13 PAD_14 PAD_15} \
        -pad    {PAD_0  PAD_1  PAD_2  PAD_3  \
                 PAD_4  PAD_5  PAD_6  PAD_7  \
                 PAD_8  PAD_9  PAD_10 PAD_11 \
                 PAD_12 PAD_13 PAD_14 PAD_15} \
        -bidi {} -clock {} -async {} \
        -pinlist {IN_0  IN_1  IN_2  IN_3  \
                  IN_4  IN_5  IN_6  IN_7  \
                  IN_8  IN_9  IN_10 IN_11 \
                  IN_12 IN_13 IN_14 IN_15 \
                  PAD_0  PAD_1  PAD_2  PAD_3  \
                  PAD_4  PAD_5  PAD_6  PAD_7  \
                  PAD_8  PAD_9  PAD_10 PAD_11 \
                  PAD_12 PAD_13 PAD_14 PAD_15 \
                  VDD VSS} \
        -constraint  constraint_template_3x3 \
        -delay       delay_template_5x1 \
        -power       power_template_5x1 \
        txip

# pinlist order: IN_0..15 PAD_0..15 (VDD/VSS excluded from vector)
# One R and one F arc per lane, only diagonal IN_i -> PAD_i
define_arc -type combinational -related_pin IN_0  -pin PAD_0  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {R 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 R X X X X X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_0  -pin PAD_0  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {F 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 F X X X X X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_1  -pin PAD_1  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 R 0 0 0 0 0 0 0 0 0 0 0 0 0 0 X R X X X X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_1  -pin PAD_1  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 F 0 0 0 0 0 0 0 0 0 0 0 0 0 0 X F X X X X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_2  -pin PAD_2  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 R 0 0 0 0 0 0 0 0 0 0 0 0 0 X X R X X X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_2  -pin PAD_2  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 F 0 0 0 0 0 0 0 0 0 0 0 0 0 X X F X X X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_3  -pin PAD_3  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 R 0 0 0 0 0 0 0 0 0 0 0 0 X X X R X X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_3  -pin PAD_3  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 F 0 0 0 0 0 0 0 0 0 0 0 0 X X X F X X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_4  -pin PAD_4  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 R 0 0 0 0 0 0 0 0 0 0 0 X X X X R X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_4  -pin PAD_4  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 F 0 0 0 0 0 0 0 0 0 0 0 X X X X F X X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_5  -pin PAD_5  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 R 0 0 0 0 0 0 0 0 0 0 X X X X X R X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_5  -pin PAD_5  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 F 0 0 0 0 0 0 0 0 0 0 X X X X X F X X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_6  -pin PAD_6  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 R 0 0 0 0 0 0 0 0 0 X X X X X X R X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_6  -pin PAD_6  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 F 0 0 0 0 0 0 0 0 0 X X X X X X F X X X X X X X X X} txip
define_arc -type combinational -related_pin IN_7  -pin PAD_7  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 R 0 0 0 0 0 0 0 0 X X X X X X X R X X X X X X X X} txip
define_arc -type combinational -related_pin IN_7  -pin PAD_7  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 F 0 0 0 0 0 0 0 0 X X X X X X X F X X X X X X X X} txip
define_arc -type combinational -related_pin IN_8  -pin PAD_8  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 R 0 0 0 0 0 0 0 X X X X X X X X R X X X X X X X} txip
define_arc -type combinational -related_pin IN_8  -pin PAD_8  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 F 0 0 0 0 0 0 0 X X X X X X X X F X X X X X X X} txip
define_arc -type combinational -related_pin IN_9  -pin PAD_9  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 R 0 0 0 0 0 0 X X X X X X X X X R X X X X X X} txip
define_arc -type combinational -related_pin IN_9  -pin PAD_9  -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 F 0 0 0 0 0 0 X X X X X X X X X F X X X X X X} txip
define_arc -type combinational -related_pin IN_10 -pin PAD_10 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 R  0 0 0 0 0 X X X X X X X X X X R  X X X X X} txip
define_arc -type combinational -related_pin IN_10 -pin PAD_10 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 F  0 0 0 0 0 X X X X X X X X X X F  X X X X X} txip
define_arc -type combinational -related_pin IN_11 -pin PAD_11 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  R  0 0 0 0 X X X X X X X X X X X  R  X X X X} txip
define_arc -type combinational -related_pin IN_11 -pin PAD_11 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  F  0 0 0 0 X X X X X X X X X X X  F  X X X X} txip
define_arc -type combinational -related_pin IN_12 -pin PAD_12 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  R  0 0 0 X X X X X X X X X X X  X  R  X X X} txip
define_arc -type combinational -related_pin IN_12 -pin PAD_12 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  F  0 0 0 X X X X X X X X X X X  X  F  X X X} txip
define_arc -type combinational -related_pin IN_13 -pin PAD_13 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  R  0 0 X X X X X X X X X X X  X  X  R  X X} txip
define_arc -type combinational -related_pin IN_13 -pin PAD_13 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  F  0 0 X X X X X X X X X X X  X  X  F  X X} txip
define_arc -type combinational -related_pin IN_14 -pin PAD_14 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  0  R  0 X X X X X X X X X X X  X  X  X  R  X} txip
define_arc -type combinational -related_pin IN_14 -pin PAD_14 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  0  F  0 X X X X X X X X X X X  X  X  X  F  X} txip
define_arc -type combinational -related_pin IN_15 -pin PAD_15 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  0  0  R  X X X X X X X X X X X  X  X  X  X  R} txip
define_arc -type combinational -related_pin IN_15 -pin PAD_15 -pinlist {IN_0  IN_1  IN_2  IN_3  IN_4  IN_5  IN_6  IN_7  IN_8  IN_9  IN_10 IN_11 IN_12 IN_13 IN_14 IN_15 PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  0  0  F  X X X X X X X X X X X  X  X  X  X  F} txip

# Use VDD_VALUE set in char.tcl; fall back to PDK typical (1.1V) if running standalone
if {![info exists VDD_VALUE]} { set VDD_VALUE 1.1 }
set_vdd VDD $VDD_VALUE
set_gnd VSS 0.0

set_var extsim_save_failed deck
set_var extsim_save_passed all

# Leakage states: quiescent all-low and all-high (required for -io mode)
# Must appear before read_spice and char_library
define_leakage -when "!IN_0 * !IN_1 * !IN_2 * !IN_3 * !IN_4 * !IN_5 * !IN_6 * !IN_7 * !IN_8 * !IN_9 * !IN_10 * !IN_11 * !IN_12 * !IN_13 * !IN_14 * !IN_15" txip
define_leakage -when "IN_0 * IN_1 * IN_2 * IN_3 * IN_4 * IN_5 * IN_6 * IN_7 * IN_8 * IN_9 * IN_10 * IN_11 * IN_12 * IN_13 * IN_14 * IN_15" txip
