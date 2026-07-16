"""Show which built-in provider configs opt into the session EventBus.

This is an inspection probe, not an application API. It reads the same
provider catalogs used by EasyCat's STT and TTS factories, then applies the
factory's structural rule: a config that declares an ``event_bus`` dataclass
field receives the session bus.

    uv run python \
        docs/teaching/13-swap-providers-and-transports/event_bus_probe.py
"""

from __future__ import annotations

from dataclasses import fields

from easycat.stt.factory import _PROVIDER_TO_CONFIG as STT_PROVIDERS
from easycat.tts.factory import _PROVIDER_TO_CONFIG as TTS_PROVIDERS


def catalog_rows() -> list[tuple[str, str, bool]]:
    """Return ``(surface, provider, declares_event_bus)`` catalog rows."""
    rows: list[tuple[str, str, bool]] = []
    for surface, providers in (("stt", STT_PROVIDERS), ("tts", TTS_PROVIDERS)):
        for provider, (_provider_cls, config_cls) in providers.items():
            declares_event_bus = any(field.name == "event_bus" for field in fields(config_cls))
            rows.append((surface, provider, declares_event_bus))
    return sorted(rows)


def main() -> None:
    print("surface  provider         receives session EventBus")
    for surface, provider, declares_event_bus in catalog_rows():
        answer = "yes" if declares_event_bus else "no"
        print(f"{surface:<8} {provider:<16} {answer}")


if __name__ == "__main__":
    main()
