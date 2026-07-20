"""Regenerate the public-domain deterministic audio fixture."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 8_000
DURATION_SECONDS = 1
FREQUENCY_HZ = 440.0
AMPLITUDE = 8_000


def main() -> None:
    output = Path(__file__).parent / "media" / "synthetic-tone.wav"
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        frames = bytearray()
        for index in range(SAMPLE_RATE * DURATION_SECONDS):
            sample = int(AMPLITUDE * math.sin(2 * math.pi * FREQUENCY_HZ * index / SAMPLE_RATE))
            frames.extend(struct.pack("<h", sample))
        wav.writeframes(bytes(frames))


if __name__ == "__main__":
    main()
