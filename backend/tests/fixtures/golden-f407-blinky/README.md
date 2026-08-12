# Golden project — STM32F407 blinky

A hand-written project that is known to compile. It is not a template and no
agent ever reads it.

## Why it exists

When a generated project fails to build there are two possible causes: the
generated code, or our own toolchain, volume mounts and container wiring.
Without a reference point every failure looks like the first one, and time
gets spent debugging a model that did nothing wrong.

`make golden` copies this directory into a workspace, compiles it through the
build sandbox, and prints the size report. If it fails, the problem is ours.

## What is deliberately missing

No HAL, no CMSIS, no vendor headers — just direct register access, a minimal
vector table and a linker script. Every dependency would be one more reason
for the check to fail for an unrelated reason.

The symbol names in `STM32F407VGTx_FLASH.ld` (`_estack`, `_sidata`, `_sdata`,
`_sbss`, …) match the CubeMX-generated ones, so generated projects can reuse
the same startup contract.

## Expected output

```
toolchain : arm-none-eabi-gcc (…) 12.2.x
status    : ok (exit 0) in ~2000 ms
artifacts : bin=build/golden.bin, elf=build/golden.elf, hex=build/golden.hex
flash     : ~1 KB (0.1%)
```
