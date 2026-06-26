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

set cells { rxip }

define_template -type delay \
        -index_1        {0.250 0.750 1.500} \
        -index_2        {0.005 0.01 0.05 0.1 0.5 1.0} \
        delay_template_3x6

define_template -type power \
        -index_1        {0.250 0.750 1.500} \
        -index_2        {0.005 0.01 0.05 0.1 0.5 1.0} \
        power_template_3x6

define_template -type constraint \
        -index_1  {0.250  0.750 1.500} \
        -index_2  {0.250  0.750 1.500} \
        constraint_template_3x3

define_cell \
        -input  {PAD_0  PAD_1  PAD_2  PAD_3  \
                 PAD_4  PAD_5  PAD_6  PAD_7  \
                 PAD_8  PAD_9  PAD_10 PAD_11 \
                 PAD_12 PAD_13 PAD_14 PAD_15} \
        -output {OUT_0  OUT_1  OUT_2  OUT_3  \
                 OUT_4  OUT_5  OUT_6  OUT_7  \
                 OUT_8  OUT_9  OUT_10 OUT_11 \
                 OUT_12 OUT_13 OUT_14 OUT_15} \
        -pad    {PAD_0  PAD_1  PAD_2  PAD_3  \
                 PAD_4  PAD_5  PAD_6  PAD_7  \
                 PAD_8  PAD_9  PAD_10 PAD_11 \
                 PAD_12 PAD_13 PAD_14 PAD_15} \
        -bidi {} -clock {} -async {} \
        -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  \
                  PAD_4  PAD_5  PAD_6  PAD_7  \
                  PAD_8  PAD_9  PAD_10 PAD_11 \
                  PAD_12 PAD_13 PAD_14 PAD_15 \
                  OUT_0  OUT_1  OUT_2  OUT_3  \
                  OUT_4  OUT_5  OUT_6  OUT_7  \
                  OUT_8  OUT_9  OUT_10 OUT_11 \
                  OUT_12 OUT_13 OUT_14 OUT_15 \
                  VDD VSS} \
        -constraint  constraint_template_3x3 \
        -delay       delay_template_3x6 \
        -power       power_template_3x6 \
        rxip

# pinlist order: PAD_0..15 OUT_0..15 VDD VSS (34 pins total)
# Vector format: 16 PAD bits + 16 OUT bits (VDD/VSS excluded from vector)
# Each diagonal arc: driving PAD_i R/F, all other PADs held 0, OUT_i R/F, all other OUTs X

define_arc -type combinational -related_pin PAD_0  -pin OUT_0  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {R 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 R X X X X X X X X X X X X X X X} rxip
define_arc -type combinational -related_pin PAD_0  -pin OUT_0  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {F 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 F X X X X X X X X X X X X X X X} rxip

define_arc -type combinational -related_pin PAD_1  -pin OUT_1  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 R 0 0 0 0 0 0 0 0 0 0 0 0 0 0 X R X X X X X X X X X X X X X X} rxip
define_arc -type combinational -related_pin PAD_1  -pin OUT_1  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 F 0 0 0 0 0 0 0 0 0 0 0 0 0 0 X F X X X X X X X X X X X X X X} rxip

define_arc -type combinational -related_pin PAD_2  -pin OUT_2  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 R 0 0 0 0 0 0 0 0 0 0 0 0 0 X X R X X X X X X X X X X X X X} rxip
define_arc -type combinational -related_pin PAD_2  -pin OUT_2  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 F 0 0 0 0 0 0 0 0 0 0 0 0 0 X X F X X X X X X X X X X X X X} rxip

define_arc -type combinational -related_pin PAD_3  -pin OUT_3  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 R 0 0 0 0 0 0 0 0 0 0 0 0 X X X R X X X X X X X X X X X X} rxip
define_arc -type combinational -related_pin PAD_3  -pin OUT_3  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 F 0 0 0 0 0 0 0 0 0 0 0 0 X X X F X X X X X X X X X X X X} rxip

define_arc -type combinational -related_pin PAD_4  -pin OUT_4  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 R 0 0 0 0 0 0 0 0 0 0 0 X X X X R X X X X X X X X X X X} rxip
define_arc -type combinational -related_pin PAD_4  -pin OUT_4  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 F 0 0 0 0 0 0 0 0 0 0 0 X X X X F X X X X X X X X X X X} rxip

define_arc -type combinational -related_pin PAD_5  -pin OUT_5  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 R 0 0 0 0 0 0 0 0 0 0 X X X X X R X X X X X X X X X X} rxip
define_arc -type combinational -related_pin PAD_5  -pin OUT_5  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 F 0 0 0 0 0 0 0 0 0 0 X X X X X F X X X X X X X X X X} rxip

define_arc -type combinational -related_pin PAD_6  -pin OUT_6  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 R 0 0 0 0 0 0 0 0 0 X X X X X X R X X X X X X X X X} rxip
define_arc -type combinational -related_pin PAD_6  -pin OUT_6  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 F 0 0 0 0 0 0 0 0 0 X X X X X X F X X X X X X X X X} rxip

define_arc -type combinational -related_pin PAD_7  -pin OUT_7  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 R 0 0 0 0 0 0 0 0 X X X X X X X R X X X X X X X X} rxip
define_arc -type combinational -related_pin PAD_7  -pin OUT_7  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 F 0 0 0 0 0 0 0 0 X X X X X X X F X X X X X X X X} rxip

define_arc -type combinational -related_pin PAD_8  -pin OUT_8  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 R 0 0 0 0 0 0 0 X X X X X X X X R X X X X X X X} rxip
define_arc -type combinational -related_pin PAD_8  -pin OUT_8  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 F 0 0 0 0 0 0 0 X X X X X X X X F X X X X X X X} rxip

define_arc -type combinational -related_pin PAD_9  -pin OUT_9  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 R 0 0 0 0 0 0 X X X X X X X X X R X X X X X X} rxip
define_arc -type combinational -related_pin PAD_9  -pin OUT_9  -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 F 0 0 0 0 0 0 X X X X X X X X X F X X X X X X} rxip

define_arc -type combinational -related_pin PAD_10 -pin OUT_10 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 R  0 0 0 0 0 X X X X X X X X X X R  X X X X X} rxip
define_arc -type combinational -related_pin PAD_10 -pin OUT_10 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 F  0 0 0 0 0 X X X X X X X X X X F  X X X X X} rxip

define_arc -type combinational -related_pin PAD_11 -pin OUT_11 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  R  0 0 0 0 X X X X X X X X X X X  R  X X X X} rxip
define_arc -type combinational -related_pin PAD_11 -pin OUT_11 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  F  0 0 0 0 X X X X X X X X X X X  F  X X X X} rxip

define_arc -type combinational -related_pin PAD_12 -pin OUT_12 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  R  0 0 0 X X X X X X X X X X X  X  R  X X X} rxip
define_arc -type combinational -related_pin PAD_12 -pin OUT_12 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  F  0 0 0 X X X X X X X X X X X  X  F  X X X} rxip

define_arc -type combinational -related_pin PAD_13 -pin OUT_13 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  R  0 0 X X X X X X X X X X X  X  X  R  X X} rxip
define_arc -type combinational -related_pin PAD_13 -pin OUT_13 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  F  0 0 X X X X X X X X X X X  X  X  F  X X} rxip

define_arc -type combinational -related_pin PAD_14 -pin OUT_14 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  0  R  0 X X X X X X X X X X X  X  X  X  R  X} rxip
define_arc -type combinational -related_pin PAD_14 -pin OUT_14 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  0  F  0 X X X X X X X X X X X  X  X  X  F  X} rxip

define_arc -type combinational -related_pin PAD_15 -pin OUT_15 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  0  0  R  X X X X X X X X X X X  X  X  X  X  R } rxip
define_arc -type combinational -related_pin PAD_15 -pin OUT_15 -pinlist {PAD_0  PAD_1  PAD_2  PAD_3  PAD_4  PAD_5  PAD_6  PAD_7  PAD_8  PAD_9  PAD_10 PAD_11 PAD_12 PAD_13 PAD_14 PAD_15 OUT_0  OUT_1  OUT_2  OUT_3  OUT_4  OUT_5  OUT_6  OUT_7  OUT_8  OUT_9  OUT_10 OUT_11 OUT_12 OUT_13 OUT_14 OUT_15} -vector {0 0 0 0 0 0 0 0 0 0 0  0  0  0  0  F  X X X X X X X X X X X  X  X  X  X  F } rxip

# Use VDD_VALUE set in char.tcl; fall back to PDK typical (1.1V) if running standalone
if {![info exists VDD_VALUE]} { set VDD_VALUE 1.1 }
set_vdd VDD $VDD_VALUE
set_gnd VSS 0.0

set_pin_vdd -supply_name VDD {PAD_0, PAD_1, PAD_2, PAD_3, PAD_4, PAD_5, PAD_6, PAD_7, PAD_8, PAD_9, PAD_10, PAD_11, PAD_12, PAD_13, PAD_14, PAD_15} rxip $VDD_VALUE

set_pin_gnd -supply_name VSS {PAD_0, PAD_1, PAD_2, PAD_3, PAD_4, PAD_5, PAD_6, PAD_7, PAD_8, PAD_9, PAD_10, PAD_11, PAD_12, PAD_13, PAD_14, PAD_15}  rxip 0

set_var init_pin_hidden_period 1e-6
set_var extsim_save_failed deck
set_var extsim_save_passed all

# Leakage states: quiescent all-low and all-high (required for -io mode)
# Must appear before read_spice and char_library
define_leakage -when "!PAD_0 * !PAD_1 * !PAD_2 * !PAD_3 * !PAD_4 * !PAD_5 * !PAD_6 * !PAD_7 * !PAD_8 * !PAD_9 * !PAD_10 * !PAD_11 * !PAD_12 * !PAD_13 * !PAD_14 * !PAD_15" rxip
define_leakage -when "PAD_0 * PAD_1 * PAD_2 * PAD_3 * PAD_4 * PAD_5 * PAD_6 * PAD_7 * PAD_8 * PAD_9 * PAD_10 * PAD_11 * PAD_12 * PAD_13 * PAD_14 * PAD_15" rxip
