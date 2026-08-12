"""Deterministic project generation (M4).

The structural half of a firmware project -- the Makefile, the linker
script, the startup code, the driver sources, the HAL configuration header
-- is not something a language model should be writing. It is the same every
time for a given MCU and a given set of peripherals, it is unforgiving about
detail, and a single wrong line breaks the build for reasons the model then
cannot see. It is generated here, from the plan, by code.

The model only fills the marked USER CODE regions inside the files this
package produces.
"""
