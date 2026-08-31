# Build sandbox (M4): an arm-none-eabi toolchain behind a small HTTP API.
#
# Kept out of the backend image on purpose:
#   * the cross toolchain is ~400 MB and has nothing to do with serving HTTP
#   * this container gets no network route (see docker-compose.yml), so a
#     generated Makefile cannot download anything or call back into the API
#   * `make test` therefore never needs a cross compiler installed
FROM python:3.12-slim

# Debian bookworm ships GCC 12.2 for arm-none-eabi. To pin a specific Arm
# release instead, replace this block with a tarball download: the exact
# version string ends up in every BuildResult.toolchain, so the swap stays
# traceable in stored results.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc-arm-none-eabi \
        binutils-arm-none-eabi \
        libnewlib-arm-none-eabi \
        make \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ST's HAL and CMSIS sources.
#
# Downloaded here, once, at image build time -- the only moment in this
# system that has internet. They are not committed to the repository: they
# are third-party C we never edit, with their own licence and release
# cadence. The refs are pinned so two builds of this image produce the same
# drivers; override them without touching the file:
#
#   docker compose build --build-arg HAL_REF=v1.8.0 builder
#
# The result is shared with the backend through the `cube_sdk` volume, which
# Docker populates from this image the first time the container starts.
ARG HAL_REF=v1.8.3
ARG CMSIS_DEVICE_REF=v2.6.10
ARG CMSIS_CORE_REF=v5.9.0
ENV CUBE_SDK_ROOT=/opt/stm32cube/f4
COPY fetch-sdk.sh /usr/local/bin/fetch-sdk
RUN chmod +x /usr/local/bin/fetch-sdk \
    && HAL_REF="$HAL_REF" \
       CMSIS_DEVICE_REF="$CMSIS_DEVICE_REF" \
       CMSIS_CORE_REF="$CMSIS_CORE_REF" \
       DEST="$CUBE_SDK_ROOT" \
       fetch-sdk

# The per-part pin tables.
#
# Same reasoning as the drivers above, for the one fact they do not contain:
# which pin can carry which signal, and at which alternate-function number.
# It is downloaded here rather than committed, and it lands inside the same
# `cube_sdk` volume, so the backend already sees it read-only -- no new mount
# for the data itself. `make devices` converts it once into the compact
# tables the planner validates against.
ARG MODM_DEVICES_REF=develop
ENV DEVICE_XML_ROOT=/opt/stm32cube/modm-devices
COPY fetch-devices.sh /usr/local/bin/fetch-devices
RUN chmod +x /usr/local/bin/fetch-devices \
    && MODM_DEVICES_REF="$MODM_DEVICES_REF" \
       DEST="$DEVICE_XML_ROOT" \
       fetch-devices

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Builds run unprivileged. The workspace directories are created world
# writable by the backend precisely so this user can write build output.
RUN useradd --uid 1000 --create-home builder
USER builder

EXPOSE 9000

# The backend waits for this before it starts, which also guarantees the
# driver volume has been populated from the image.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=6 \
    CMD python -c "import urllib.request as r; r.urlopen('http://127.0.0.1:9000/health', timeout=3)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9000"]
