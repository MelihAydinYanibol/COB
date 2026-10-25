import numpy as np

from cob.audio import AudioBridge, ByteFifo, downsample, upsample, volume_to_gain


def test_upsample_is_continuous_across_chunks():
    first, prev = upsample(np.array([0, 600], dtype=np.int16), 6, 0.0)
    assert len(first) == 12
    assert first[5] == 0 and first[-1] == 600
    second, _ = upsample(np.array([1200], dtype=np.int16), 6, prev)
    assert np.allclose(second, [700, 800, 900, 1000, 1100, 1200])


def test_downsample_averages_groups():
    assert np.allclose(downsample(np.array([1, 3, 5, 7], dtype=np.int16), 2), [2, 6])


def test_volume_to_gain():
    assert volume_to_gain(15) == 1.0
    assert volume_to_gain(0) == 0.0
    assert 0 < volume_to_gain(5) < volume_to_gain(10) < 1


def test_fifo_pads_and_caps():
    fifo = ByteFifo(4)
    fifo.write(b"abcdef")
    assert len(fifo) == 4
    assert fifo.read(6) == b"cdef\x00\x00"


def test_exchange_returns_packet_of_same_size():
    bridge = AudioBridge(rate=48000)  # stream not started: no audio hardware needed
    packet = np.arange(24, dtype="<i2").tobytes()
    reply = bridge.exchange(packet)
    assert len(reply) == len(packet)
    assert reply == bytes(len(packet))  # silent microphone
    assert len(bridge._speaker) == 24 * 6 * 2
