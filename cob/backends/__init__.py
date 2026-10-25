"""Bluetooth backends.

A backend owns the radio side: it registers COB as a Hands-Free device,
connects to the phone, hands the RFCOMM control channel to the app as an
asyncio stream pair, and moves SCO audio packets through the app's
:class:`~cob.audio.AudioBridge`.

* ``bluez``  - Linux (Fedora etc.) through the system BlueZ daemon.
* ``bumble`` - Windows 10/11 (or Linux) through Google's Bumble stack,
  driving a USB Bluetooth dongle directly.
* ``none``   - no Bluetooth; use only the Wi-Fi companion app.
"""

from __future__ import annotations

import asyncio
import sys
from abc import ABC, abstractmethod
from typing import Protocol

from ..audio import AudioBridge
from ..hfp import HfFeature


class BackendListener(Protocol):
    """What a backend calls back into (implemented by the app)."""

    def control_connected(
        self,
        address: str,
        name: str | None,
        reader: asyncio.StreamReader,
        writer: "StreamWriterLike",
    ) -> bool:
        """A phone opened the HFP control channel. Return False to refuse it."""

    def audio_connected(self, address: str) -> AudioBridge | None:
        """An audio link is up. Return a started bridge, or None to refuse it."""

    def audio_disconnected(self) -> None:
        """The audio link went away (audio is back on the phone)."""


class StreamWriterLike(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...


class Backend(ABC):
    def __init__(self, listener: BackendListener, features: HfFeature):
        self.listener = listener
        self.features = features

    @abstractmethod
    async def start(self) -> None:
        """Bring up the stack and make COB connectable as a Hands-Free device."""

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def connect(self, address: str) -> None:
        """Connect to an (already paired) phone."""

    @abstractmethod
    async def open_audio(self, address: str) -> None:
        """Open an audio link, moving the call audio to the PC."""

    @abstractmethod
    async def close_audio(self) -> None:
        """Close the audio link, moving the call audio back to the phone."""

    def describe(self) -> str:
        return type(self).__name__


class NoBackend(Backend):
    """Bluetooth turned off: only the Wi-Fi link to the companion app is used."""

    def describe(self) -> str:
        return "no Bluetooth"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def connect(self, address: str) -> None:
        raise RuntimeError("Bluetooth is turned off (--backend none)")

    async def open_audio(self, address: str) -> None:
        raise RuntimeError("Bluetooth is turned off (--backend none)")

    async def close_audio(self) -> None:
        pass


def default_backend() -> str:
    return "bluez" if sys.platform.startswith("linux") else "bumble"


def create_backend(name: str, listener: BackendListener, features: HfFeature, args) -> Backend:
    if name == "none":
        return NoBackend(listener, features)
    if name == "bluez":
        from .bluez import BlueZBackend

        return BlueZBackend(listener, features, adapter=args.adapter)
    if name == "bumble":
        from .bumble import BumbleBackend

        return BumbleBackend(
            listener,
            features,
            transport=args.transport,
            device_name=args.name,
            keystore=args.keystore,
        )
    raise ValueError(f"unknown backend: {name}")
