"""The per-part hardware table: the facts a model must not be asked to recall.

An alternate-function number is not a design decision. PA2 carries USART2_TX
at AF7 or it does not, and no amount of reasoning changes it. That fact is in
the datasheet and in STM32CubeMX's database -- it is *not* in the HAL sources
we already ship -- so it has to be imported once and then stored, rather than
recalled at generation time by something that is confidently wrong twice a
day.

This module owns the stored form: one JSON file per part, in a writable
directory. Nothing here downloads anything. `deviceimport` fills these files
from vendor data that the build image downloaded, and the admin panel will
later drop the same shape into the same place -- which is why the format is
boring, sorted and documented:

    {
      "format": 2,
      "part": "stm32f407xx",
      "source": "modm-devices refs/heads/develop | stm32f4-05_07_15_17.xml",
      "pins": {"PA2": {"USART2_TX": 7, "ADC1_IN2": null}},
      "vectors": {"USART2": 38, "TIM8_BRK_TIM12": 43},
      "instances": ["I2C1", "SPI1", "TIM3", "USART2"],
      "dma_routes": {
        "SPI1_RX": [
          {"controller": 2, "stream": 0, "channel": 3}
        ]
      },
      "notes": ["..."]
    }

`null` means the pin carries that signal with no alternate-function number
(an analog input, say), which is a different answer from "unknown". Unknown
is the absence of the key, and the only correct response to it is to refuse.

Adding a part is not a new table written by hand: it is one row in
`devices.py` plus a re-run of `make devices`.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.codegen.errors import CodegenError
from app.core.config import settings

# Bumped when the on-disk shape changes in a way an old file cannot satisfy.
# A stale file is then reported, not silently misread.
FORMAT = 2

HINT = (
    "pin tables are downloaded into the build image and are deliberately not "
    "in git; run `make builder-image`, then `make sdk-refresh` (which has to "
    "be able to delete the shared volume), then `make devices`"
)

_PIN_RE = re.compile(r"^P([A-K])(\d{1,2})$")
# TIM12, I2C1, SPI1 -- a peripheral instance. EV, ER, BRK, UP are not.
_INSTANCE_RE = re.compile(r"^[A-Z]+\d+$")


def _pin(text: str) -> str:
    return str(text or "").strip().upper()


def _order(pin: str) -> tuple[str, int]:
    match = _PIN_RE.match(pin)
    return (match.group(1), int(match.group(2))) if match else (pin, 0)


@dataclass
class DmaRoute:
    """One legal STM32F4 DMA request route."""

    controller: int
    stream: int
    channel: int

    @property
    def stream_name(self) -> str:
        return f"DMA{self.controller}_Stream{self.stream}"

    def to_dict(self) -> dict[str, int]:
        return {
            "controller": self.controller,
            "stream": self.stream,
            "channel": self.channel,
        }


@dataclass
class DeviceData:
    """Everything known about one part, as read from disk."""

    part: str
    source: str = ""
    # "PA2" -> {"USART2_TX": 7, "ADC1_IN2": None}
    pins: dict[str, dict[str, int | None]] = field(default_factory=dict)
    # "TIM8_BRK_TIM12" -> 43, the CMSIS name without its _IRQn suffix
    vectors: dict[str, int] = field(default_factory=dict)
    instances: list[str] = field(default_factory=list)
    # "SPI1_RX" -> every controller/stream/channel combination the part
    # supports, ordered deterministically by the importer.
    dma_routes: dict[str, list[DmaRoute]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # -- lookups ----------------------------------------------------------

    def signals(self, pin: str) -> dict[str, int | None]:
        """Every signal this pin can carry, with its AF number or None."""
        return self.pins.get(_pin(pin), {})

    def carries(self, pin: str, signal: str) -> bool:
        return str(signal or "").strip().upper() in self.signals(pin)

    def alternate(self, pin: str, signal: str) -> int | None:
        """The AF number, or None when the signal has no alternate function."""
        name = str(signal or "").strip().upper()
        signals = self.signals(pin)
        if name not in signals:
            raise CodegenError(f"{_pin(pin)} does not carry {name} on {self.part}")
        return signals[name]

    def pins_for(self, signal: str) -> list[str]:
        """Which pins could carry this signal -- the answer to a refusal."""
        name = str(signal or "").strip().upper()
        return sorted((pin for pin, table in self.pins.items() if name in table), key=_order)

    def signals_of(self, pin: str, peripheral: str) -> list[str]:
        """The signals on this pin that belong to one peripheral instance."""
        name = str(peripheral or "").strip().upper()
        if not name:
            return []
        return sorted(s for s in self.signals(pin) if s.split("_", 1)[0] == name)

    def has(self, peripheral: str) -> bool:
        return str(peripheral or "").strip().upper() in self.instances

    def routes_for(self, request: str) -> list[DmaRoute]:
        """Every legal route for a request, in stable hardware order."""
        name = str(request or "").strip().upper()
        return list(self.dma_routes.get(name, ()))

    def has_dma_request(self, request: str) -> bool:
        return bool(self.routes_for(request))

    def dma_route(self, request: str, stream: str, channel: int) -> DmaRoute | None:
        wanted_stream = str(stream or "").strip().upper()
        for route in self.routes_for(request):
            if route.stream_name.upper() == wanted_stream and route.channel == channel:
                return route
        return None

    def vectors_for(self, peripheral: str) -> list[str]:
        """The interrupt vectors that reach this peripheral.

        TIM3 gets its own; TIM12 arrives on TIM8_BRK_TIM12 together with
        TIM8. Which of the two it is decides whether a generated handler can
        be written at all, so it is read from the part's own header rather
        than from a list somebody typed out for one family.
        """
        name = str(peripheral or "").strip().upper()
        if not name:
            return []
        return [vector for vector in sorted(self.vectors) if name in vector.split("_")]

    def shares_vector(self, vector: str, peripheral: str) -> list[str]:
        """The other peripherals sitting on the same vector."""
        name = str(peripheral or "").strip().upper()
        return [
            token
            for token in str(vector or "").split("_")
            if token != name and _INSTANCE_RE.match(token)
        ]

    # -- storage ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "part": self.part,
            "source": self.source,
            "pins": {pin: dict(sorted(table.items())) for pin, table in sorted(self.pins.items())},
            "vectors": dict(sorted(self.vectors.items())),
            "instances": sorted(self.instances),
            "dma_routes": {
                request: [route.to_dict() for route in routes]
                for request, routes in sorted(self.dma_routes.items())
            },
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "DeviceData":
        version = raw.get("format")
        if version != FORMAT:
            raise CodegenError(
                f"device table for {raw.get('part', 'an unknown part')!r} is format "
                f"{version!r}, this build reads format {FORMAT}; re-run `make devices`"
            )
        pins: dict[str, dict[str, int | None]] = {}
        for pin, table in (raw.get("pins") or {}).items():
            pins[_pin(pin)] = {
                str(signal).upper(): (None if value is None else int(value))
                for signal, value in (table or {}).items()
            }
        dma_routes: dict[str, list[DmaRoute]] = {}
        for request, routes in (raw.get("dma_routes") or {}).items():
            dma_routes[str(request).upper()] = sorted(
                [
                    DmaRoute(
                        controller=int(route["controller"]),
                        stream=int(route["stream"]),
                        channel=int(route["channel"]),
                    )
                    for route in routes or []
                ],
                key=lambda route: (route.controller, route.stream, route.channel),
            )
        return cls(
            part=str(raw.get("part") or "").strip().lower(),
            source=str(raw.get("source") or ""),
            pins=pins,
            vectors={str(k): int(v) for k, v in (raw.get("vectors") or {}).items()},
            instances=[str(name).upper() for name in (raw.get("instances") or [])],
            dma_routes=dma_routes,
            notes=[str(note) for note in (raw.get("notes") or [])],
        )


def data_root() -> Path:
    """Read the setting on every call so tests can point it at a temp dir."""
    return Path(settings.device_data_root)


def path_for(part: str) -> Path:
    return data_root() / f"{str(part).strip().lower()}.json"


def available() -> list[str]:
    """The parts that have a table on this machine."""
    root = data_root()
    if not root.is_dir():
        return []
    return sorted(path.stem for path in root.glob("*.json"))


def store(data: DeviceData) -> Path:
    path = path_for(data.part)
    path.parent.mkdir(parents=True, exist_ok=True)
    # indent=1 keeps the file diffable and readable in a browser: it is data
    # a human may well want to check against a datasheet by eye.
    path.write_text(json.dumps(data.to_dict(), indent=1, sort_keys=False), encoding="utf-8")
    return path


def read_cached(part: str) -> DeviceData | None:
    path = path_for(part)
    try:
        raw = json.loads(path.read_text("utf-8"))
    except OSError:
        return None
    except ValueError as error:
        raise CodegenError(f"{path} is not readable as JSON: {error}") from error
    return DeviceData.from_dict(raw)


def load(part: str) -> DeviceData:
    """The table for a part: from disk, or built once from the vendor data.

    Import happens here rather than at start-up so that a machine with no
    vendor data still boots, and still refuses to generate -- the refusal
    carries the command that fixes it.
    """
    name = str(part).strip().lower()
    cached = read_cached(name)
    if cached is not None:
        return cached
    # Local import: deviceimport reads this module's DeviceData.
    from app.codegen import deviceimport

    data = deviceimport.build(name)
    try:
        store(data)
    except OSError:
        # A read-only cache directory is not a reason to refuse a plan; the
        # next call simply pays for the parse again.
        pass
    return data
