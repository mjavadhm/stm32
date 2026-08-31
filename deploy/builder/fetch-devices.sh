#!/bin/sh
# Fetch the per-part pin tables into the build image.
#
# Which pin can carry which signal, and at which alternate-function number,
# is the one fact this system needs that ST does not ship with the HAL. It
# lives in the datasheets and in STM32CubeMX's unpacked database. modm-devices
# publishes it machine-readably, extracted from the same vendor sources, which
# is why we take it from there instead of parsing PDFs or asking a model.
#
# Like the HAL, it is deliberately NOT committed to this repository: it is
# third-party data we never edit, with its own licence and release cadence.
# It is downloaded once, at `docker build` time, on a machine that has
# internet. Nothing at runtime does: the builder has no route out and the
# backend reads these files through a read-only volume.
#
# The layout produced here:
#
#   $DEST/VERSION        what was actually downloaded
#   $DEST/stm32/*.xml    one file per group of parts

set -eu

MODM_DEVICES_REF="${MODM_DEVICES_REF:-develop}"
DEST="${DEST:-/opt/stm32cube/modm-devices}"
# Only the families we generate for. The full set is ~30 MB of XML for 4500
# devices, and every one of them we keep is a file the importer has to open.
DEVICE_GLOB="${DEVICE_GLOB:-stm32f4*.xml}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST/stm32"
: > "$DEST/VERSION"

# Tags get renamed and default branches get moved; a hard failure here breaks
# `make builder-image` for everyone. Try the pinned ref first (reproducible
# builds), fall back to the default branches, and record what was used.
fetch() {
	for candidate in \
		"refs/tags/$MODM_DEVICES_REF" \
		"refs/heads/$MODM_DEVICES_REF" \
		"refs/heads/develop" \
		"refs/heads/main" \
		"refs/heads/master"
	do
		url="https://github.com/modm-io/modm-devices/archive/$candidate.tar.gz"
		printf '  %s ... ' "$candidate"
		if curl -fsSL --retry 3 --retry-delay 2 "$url" -o "$WORK/src.tar.gz"; then
			mkdir -p "$WORK/src"
			tar -xzf "$WORK/src.tar.gz" -C "$WORK/src" --strip-components=1
			printf 'ok\n'
			printf 'modm-devices %s\n' "$candidate" >> "$DEST/VERSION"
			return 0
		fi
		printf 'not found\n'
	done
	printf 'FATAL: cannot download modm-devices (tried %s and the default branches).\n' \
		"$MODM_DEVICES_REF" >&2
	exit 1
}

printf 'modm-devices (%s)\n' "$MODM_DEVICES_REF"
fetch

if [ ! -d "$WORK/src/devices/stm32" ]; then
	printf 'FATAL: no devices/stm32 directory in the archive; the layout changed.\n' >&2
	exit 1
fi

# shellcheck disable=SC2086
find "$WORK/src/devices/stm32" -name "$DEVICE_GLOB" -exec cp {} "$DEST/stm32/" \;

count="$(find "$DEST/stm32" -name '*.xml' | wc -l | tr -d ' ')"
if [ "$count" -eq 0 ]; then
	printf 'FATAL: %s matched no files in devices/stm32.\n' "$DEVICE_GLOB" >&2
	exit 1
fi

# The parts we actually generate for must be in there, or the failure would
# only surface at a user's first project.
for required in stm32f407 stm32f411; do
	if ! grep -qil "$required" "$DEST/stm32"/*.xml; then
		printf 'FATAL: no vendor file mentions %s.\n' "$required" >&2
		exit 1
	fi
done

chmod -R a+rX "$DEST"

printf '\nPin tables ready in %s (%s files, %s)\n' \
	"$DEST" "$count" "$(du -sh "$DEST" | cut -f1)"
cat "$DEST/VERSION"
