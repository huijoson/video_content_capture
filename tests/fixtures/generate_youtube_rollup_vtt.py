"""Regenerate the synthetic YouTube roll-up WebVTT fixture.

The fixture reproduces the structural shapes of a YouTube automatic-caption VTT without
copying any of its text: a `Kind:`/`Language:` header metadata block, blank roll-up cue
bodies that hold nothing but a single space, inline `<c>` markup, inline karaoke
timestamps, and a final cue that runs slightly past the reported duration. Those shapes
are exactly what broke #13, and a generator keeps the invisible whitespace-only lines
from being lost to an editor trimming them.
"""

from __future__ import annotations

from pathlib import Path

DURATION = 12.0

LINES = (
    "WEBVTT",
    "Kind: captions",
    "Language: en",
    "",
    "00:00:00.640 --> 00:00:04.150 align:start position:0%",
    # A blank roll-up line opens the body: it is not a block separator.
    " ",
    "a<00:00:00.799><c> rolling</c><00:00:01.000><c> caption</c><00:00:01.560><c> keeps</c>",
    "",
    "00:00:04.150 --> 00:00:04.160 align:start position:0%",
    "a rolling caption keeps",
    # A blank roll-up line also closes the body.
    " ",
    "",
    "00:00:04.160 --> 00:00:08.669 align:start position:0%",
    "a rolling caption keeps",
    "f<00:00:04.400><c> every</c><00:00:04.800><c> word</c>",
    "",
    # A cue whose whole body is blank, between a timing line and truly empty lines.
    "00:00:08.669 --> 00:00:08.679 align:start position:0%",
    " ",
    " ",
    "",
    "00:00:08.679 --> 00:00:10.000 align:start position:0%",
    " ",
    "b<00:00:09.000><c> after</c><00:00:09.400><c> a</c><00:00:09.800><c> blank</c> cue",
    "",
    "00:00:10.000 --> 00:00:12.500 align:start position:0%",
    # Runs past DURATION, the way a real track drifts past rounded metadata.
    "closing cue",
    "",
)


def main() -> None:
    output = Path(__file__).parent / "vtt" / "youtube-rollup-automatic.vtt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(LINES), encoding="utf-8")


if __name__ == "__main__":
    main()
