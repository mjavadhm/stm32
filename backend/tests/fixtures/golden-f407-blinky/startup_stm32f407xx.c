/* Minimal startup for STM32F407: vector table, .data/.bss init, main().
 *
 * Written in C rather than assembly on purpose -- there is nothing here that
 * needs assembly, and one less language in the golden project is one less
 * thing that can break for a reason unrelated to the toolchain.
 */

#include <stdint.h>

extern uint32_t _sidata; /* .data initialisers, in flash */
extern uint32_t _sdata;
extern uint32_t _edata;
extern uint32_t _sbss;
extern uint32_t _ebss;
extern uint32_t _estack;

int main(void);

void Reset_Handler(void);
void Default_Handler(void);

void Reset_Handler(void)
{
    const uint32_t *source = &_sidata;
    for (uint32_t *destination = &_sdata; destination < &_edata; ++destination) {
        *destination = *source++;
    }
    for (uint32_t *destination = &_sbss; destination < &_ebss; ++destination) {
        *destination = 0U;
    }

    (void)main();

    for (;;) {
        /* main() must not return on a microcontroller. */
    }
}

void Default_Handler(void)
{
    for (;;) {
        /* Unhandled exception: stop here so a debugger can see it. */
    }
}

void NMI_Handler(void) __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void MemManage_Handler(void) __attribute__((weak, alias("Default_Handler")));
void BusFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void UsageFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void) __attribute__((weak, alias("Default_Handler")));
void DebugMon_Handler(void) __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void) __attribute__((weak, alias("Default_Handler")));

typedef void (*vector_t)(void);

/* Only the core exceptions. A real project adds the 82 device interrupts;
 * the linker script keeps this section first in flash either way.
 */
__attribute__((section(".isr_vector"), used))
const vector_t g_pfnVectors[] = {
    (vector_t)(&_estack),
    Reset_Handler,
    NMI_Handler,
    HardFault_Handler,
    MemManage_Handler,
    BusFault_Handler,
    UsageFault_Handler,
    0,
    0,
    0,
    0,
    SVC_Handler,
    DebugMon_Handler,
    0,
    PendSV_Handler,
    SysTick_Handler,
};
