#!/bin/sh
# Fetch the ST driver sources into the build image.
#
# These files are deliberately NOT committed to this repository: they are
# ~10 MB of third-party C that we never edit, with their own licence and
# their own release cadence. Vendoring them into git would mean reviewing
# ST's diffs in our pull requests forever.
#
# They are downloaded once, at `docker build` time, on a machine that has
# internet. The running containers never do: the builder has no route out
# (see `build-net: internal` in docker-compose.yml) and the backend reads
# the same files through a shared read-only volume. Generation stays fully
# offline.
#
# The layout produced here mirrors STM32CubeMX output on purpose, so a
# generated project looks familiar to anyone who has used the official
# tool:
#
#   Drivers/STM32F4xx_HAL_Driver/{Inc,Src}
#   Drivers/CMSIS/Include
#   Drivers/CMSIS/Device/ST/STM32F4xx/{Include,Source/Templates}

set -eu

HAL_REF="${HAL_REF:-v1.8.3}"
CMSIS_DEVICE_REF="${CMSIS_DEVICE_REF:-v2.6.10}"
CMSIS_CORE_REF="${CMSIS_CORE_REF:-v5.9.0}"
DEST="${DEST:-/opt/stm32cube/f4}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST/Drivers"
: > "$DEST/VERSION"

# Download <repo> at <ref> and unpack it into <dir>.
#
# Tags get renamed and repositories get restructured; a hard failure here
# breaks `make builder-image` for everyone. So a pinned tag is tried first
# (reproducible builds) and the default branches are tried after it, with
# whatever was actually used recorded in VERSION.
fetch() {
	repo="$1"
	ref="$2"
	dir="$3"
	for candidate in "refs/tags/$ref" "refs/heads/$ref" "refs/heads/main" "refs/heads/master"; do
		url="https://github.com/STMicroelectronics/$repo/archive/$candidate.tar.gz"
		printf '  %s ... ' "$candidate"
		if curl -fsSL --retry 3 --retry-delay 2 "$url" -o "$WORK/src.tar.gz"; then
			mkdir -p "$dir"
			tar -xzf "$WORK/src.tar.gz" -C "$dir" --strip-components=1
			rm -f "$WORK/src.tar.gz"
			printf 'ok\n'
			printf '%s %s\n' "$repo" "$candidate" >> "$DEST/VERSION"
			return 0
		fi
		printf 'not found\n'
	done
	printf 'FATAL: cannot download %s (tried %s and the default branches).\n' \
		"$repo" "$ref" >&2
	printf 'Pass a different ref, e.g. --build-arg HAL_REF=v1.8.0\n' >&2
	exit 1
}

printf 'stm32f4xx_hal_driver (%s)\n' "$HAL_REF"
fetch stm32f4xx_hal_driver "$HAL_REF" "$WORK/hal"
mkdir -p "$DEST/Drivers/STM32F4xx_HAL_Driver"
cp -a "$WORK/hal/Inc" "$WORK/hal/Src" "$DEST/Drivers/STM32F4xx_HAL_Driver/"

printf 'cmsis_device_f4 (%s)\n' "$CMSIS_DEVICE_REF"
fetch cmsis_device_f4 "$CMSIS_DEVICE_REF" "$WORK/device"
mkdir -p "$DEST/Drivers/CMSIS/Device/ST/STM32F4xx"
cp -a "$WORK/device/Include" "$WORK/device/Source" \
	"$DEST/Drivers/CMSIS/Device/ST/STM32F4xx/"

printf 'cmsis_core (%s)\n' "$CMSIS_CORE_REF"
fetch cmsis_core "$CMSIS_CORE_REF" "$WORK/core"
# ST's mirror keeps the headers at Include/; ARM's CMSIS_5 layout puts them
# at CMSIS/Core/Include. Accept either so a ref change cannot break this.
if [ -d "$WORK/core/Include" ]; then
	core_include="$WORK/core/Include"
elif [ -d "$WORK/core/CMSIS/Core/Include" ]; then
	core_include="$WORK/core/CMSIS/Core/Include"
else
	printf 'FATAL: no CMSIS core headers in the downloaded archive.\n' >&2
	exit 1
fi
mkdir -p "$DEST/Drivers/CMSIS"
cp -a "$core_include" "$DEST/Drivers/CMSIS/Include"

# ST ships example implementations next to the drivers. They are not meant
# to be compiled (several define the same symbols) and every one of them
# would be copied into every generated project.
find "$DEST/Drivers/STM32F4xx_HAL_Driver/Src" -name '*_template.c' -delete

# A generated project must not depend on a header that only exists in this
# image, so fail the build now rather than at the user's first compile.
for required in \
	"$DEST/Drivers/STM32F4xx_HAL_Driver/Src/stm32f4xx_hal.c" \
	"$DEST/Drivers/STM32F4xx_HAL_Driver/Inc/stm32f4xx_hal_rcc.h" \
	"$DEST/Drivers/CMSIS/Device/ST/STM32F4xx/Include/stm32f4xx.h" \
	"$DEST/Drivers/CMSIS/Device/ST/STM32F4xx/Source/Templates/system_stm32f4xx.c" \
	"$DEST/Drivers/CMSIS/Device/ST/STM32F4xx/Source/Templates/gcc/startup_stm32f407xx.s" \
	"$DEST/Drivers/CMSIS/Include/core_cm4.h"
do
	if [ ! -f "$required" ]; then
		printf 'FATAL: missing %s after download.\n' "$required" >&2
		exit 1
	fi
done

chmod -R a+rX "$DEST"

printf '\nSDK ready in %s (%s)\n' "$DEST" "$(du -sh "$DEST" | cut -f1)"
cat "$DEST/VERSION"
