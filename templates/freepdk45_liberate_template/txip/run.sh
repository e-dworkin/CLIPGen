#!/bin/bash
# 'liberate' must be on your PATH (set up your Cadence environment before running).
liberate char.tcl 2>&1 | tee char.log > /dev/null
