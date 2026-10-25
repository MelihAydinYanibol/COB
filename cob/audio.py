"""Bridges call audio to the PC's microphone and speakers via PortAudio.

Call audio uses the CVSD codec: 8 kHz, 16-bit little-endian mono PCM. The
Bluetooth link sets the pace. For every packet received from the phone the
backend calls :meth:`AudioBridge.exchange`, which queues it for the speakers
and returns a microphone packet of the same size to send back. This is how
Bluetooth controllers expect SCO traffic to flow.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

SCO_RATE = 8000
SAMPLE_BYTES = 2


def upsample(samples: np.ndarray, factor: int, previous: float) -> tuple[np.ndarray, float]:
    """Linear-interpolate ``samples`` by an integer ``factor``.

    ``previous`` is the last sample of the previous chunk, so consecutive
    chunks join up without clicks. Returns the result and the new ``previous``.
    """
    if len(samples) == 0:
        return samples.astype(np.float32), previous
    if factor == 1:
        return samples.astype(np.float32), float(samples[-1])
    points = np.concatenate(([previous], samples.astype(np.float32)))
    positions = np.arange(1, len(samples) * factor + 1, dtype=np.float32) / factor
    result = np.interp(positions, np.arange(len(points)), points)
    return result.astype(np.float32), float(samples[-1])


def downsample(samples: np.ndarray, factor: int) -> np.ndarray:
    """Average groups of ``factor`` samples (a crude but adequate low-pass)."""
    if factor == 1:
        return samples.astype(np.float32)
    usable = len(samples) - len(samples) % factor
    return samples[:usable].astype(np.float32).reshape(-1, factor).mean(axis=1)


def volume_to_gain(level: int) -> float:
    """Map an HFP volume level (0-15) to a linear gain, 2 dB per step."""
    if level <= 0:
        return 0.0
    return 10 ** ((min(level, 15) - 15) * 2 / 20)


def to_pcm(samples: np.ndarray) -> bytes:
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


class ByteFifo:
    """Thread-safe byte queue with a size cap that drops the oldest data."""

    def __init__(self, max_bytes: int):
        self._data = bytearray()
        self._max = max_bytes
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            self._data += data
            overflow = len(self._data) - self._max
            if overflow > 0:
                overflow += overflow % SAMPLE_BYTES
                del self._data[:overflow]

    def read(self, size: int) -> bytes:
        """Read exactly ``size`` bytes, padding with silence on underrun."""
        with self._lock:
            chunk = bytes(self._data[:size])
            del self._data[:size]
        return chunk + bytes(size - len(chunk))

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class AudioBridge:
    """Plays phone audio on the speakers and captures the microphone for the phone."""

    def __init__(self, input_device=None, output_device=None, rate: int = 48000):
        if rate % SCO_RATE:
            raise ValueError(f"audio rate must be a multiple of {SCO_RATE} Hz")
        self._devices = (input_device, output_device)
        self.rate = rate
        self._factor = rate // SCO_RATE
        bytes_per_ms = rate * SAMPLE_BYTES // 1000
        self._speaker = ByteFifo(300 * bytes_per_ms)
        self._mic = ByteFifo(300 * bytes_per_ms)
        self._prefill = 60 * bytes_per_ms
        self._previous = 0.0
        self._stream = None
        self.gain = 1.0
        self.muted = False

    def set_volume(self, level: int) -> None:
        self.gain = volume_to_gain(level)

    def start(self) -> None:
        import sounddevice as sd

        self._speaker.write(bytes(self._prefill))  # jitter cushion
        self._stream = sd.RawStream(
            samplerate=self.rate,
            channels=1,
            dtype="int16",
            device=self._devices,
            latency="low",
            callback=self._audio_callback,
        )
        self._stream.start()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("closing audio stream failed", exc_info=True)

    def _audio_callback(self, indata, outdata, frames, time, status) -> None:
        if status:
            log.debug("audio status: %s", status)
        self._mic.write(bytes(len(indata)) if self.muted else bytes(indata))
        outdata[:] = self._speaker.read(len(outdata))

    def exchange(self, packet: bytes) -> bytes:
        """Queue one 8 kHz packet from the phone; return one for the phone."""
        count = len(packet) // SAMPLE_BYTES
        incoming = np.frombuffer(packet[: count * SAMPLE_BYTES], dtype="<i2")
        played, self._previous = upsample(incoming, self._factor, self._previous)
        self._speaker.write(to_pcm(played * self.gain))

        captured = np.frombuffer(
            self._mic.read(count * self._factor * SAMPLE_BYTES), dtype="<i2"
        )
        return to_pcm(downsample(captured, self._factor))


def play_ringtone(device=None, rate: int = 48000) -> None:
    """Play a short two-tone ring on the speakers (non-blocking)."""
    import sounddevice as sd

    t = np.arange(int(rate * 1.0)) / rate
    tone = 0.5 * np.sin(2 * np.pi * 440 * t) + 0.5 * np.sin(2 * np.pi * 480 * t)
    fade = np.minimum(1.0, np.minimum(t, t[-1] - t) / 0.02)
    sd.play((0.3 * tone * fade).astype(np.float32), rate, device=device)


def list_devices() -> str:
    import sounddevice as sd

    return str(sd.query_devices())
