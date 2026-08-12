# Golden project: proves the toolchain, not the agent.
#
# Structured the way generated projects will be (same variables, same
# targets), so `make golden` exercises the real path: sources -> objects ->
# elf -> bin -> size.

TARGET   = golden
BUILD    = build
LDSCRIPT = STM32F407VGTx_FLASH.ld

CC      = arm-none-eabi-gcc
OBJCOPY = arm-none-eabi-objcopy
SIZE    = arm-none-eabi-size

CPU     = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard
CFLAGS  = $(CPU) -std=gnu11 -Og -g3 -Wall -Wextra \
          -ffunction-sections -fdata-sections
LDFLAGS = $(CPU) -T$(LDSCRIPT) -Wl,--gc-sections -Wl,-Map=$(BUILD)/$(TARGET).map \
          --specs=nano.specs --specs=nosys.specs

SRCS = main.c startup_stm32f407xx.c
OBJS = $(addprefix $(BUILD)/,$(SRCS:.c=.o))

all: $(BUILD)/$(TARGET).elf $(BUILD)/$(TARGET).bin $(BUILD)/$(TARGET).hex size

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%.o: %.c | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/$(TARGET).elf: $(OBJS) $(LDSCRIPT) | $(BUILD)
	$(CC) $(OBJS) $(LDFLAGS) -o $@

$(BUILD)/$(TARGET).bin: $(BUILD)/$(TARGET).elf
	$(OBJCOPY) -O binary $< $@

$(BUILD)/$(TARGET).hex: $(BUILD)/$(TARGET).elf
	$(OBJCOPY) -O ihex $< $@

size: $(BUILD)/$(TARGET).elf
	$(SIZE) $<

clean:
	rm -rf $(BUILD)

.PHONY: all clean size
