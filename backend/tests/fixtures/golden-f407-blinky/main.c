/* Blink PD12 (the green LED on an STM32F4-Discovery) with direct register
 * access: no HAL, no CMSIS, no vendor headers.
 *
 * The golden project exists to prove the toolchain, so every dependency it
 * carries is one more way for that proof to fail for an unrelated reason.
 * Register offsets: RM0090 sections 6.3 (RCC) and 8.4 (GPIO).
 */

#include <stdint.h>

#define PERIPH_BASE 0x40000000UL
#define AHB1_BASE   (PERIPH_BASE + 0x00020000UL)
#define RCC_BASE    (AHB1_BASE + 0x3800UL)
#define GPIOD_BASE  (AHB1_BASE + 0x0C00UL)

#define RCC_AHB1ENR (*(volatile uint32_t *)(RCC_BASE + 0x30UL))
#define GPIOD_MODER (*(volatile uint32_t *)(GPIOD_BASE + 0x00UL))
#define GPIOD_ODR   (*(volatile uint32_t *)(GPIOD_BASE + 0x14UL))

#define RCC_AHB1ENR_GPIODEN (1UL << 3)
#define LED_PIN             12U

static void delay(uint32_t ticks)
{
    for (volatile uint32_t i = 0U; i < ticks; ++i) {
        /* Busy wait: SysTick is deliberately not configured here. */
    }
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIODEN;

    GPIOD_MODER &= ~(3UL << (LED_PIN * 2U));
    GPIOD_MODER |= (1UL << (LED_PIN * 2U)); /* general purpose output */

    for (;;) {
        GPIOD_ODR ^= (1UL << LED_PIN);
        delay(1000000U);
    }
}
