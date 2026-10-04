"""Fixed-style burned-in subtitles: ASS rendering, line wrapping and encode planning.

Burned exports always re-encode (ADR 0004): H.264 through VideoToolbox plus AAC in MP4,
at the source resolution. The subtitle style is fixed: white text with a black outline,
bottom centre, size scaled to the video height and a CJK-capable macOS system font.
"""

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from video_content_capture.workspace.subtitles import Cue

# Share of the frame height used for the font size, bottom margin and side margins.
FONT_SCALE = 0.05
MARGIN_SCALE = 0.05
# Conservative advance estimates in em; libass smart wrapping stays as a backstop for
# space-separated text, but it never breaks CJK runs, so lines are wrapped here.
WIDE_ADVANCE = 1.0
NARROW_ADVANCE = 0.5
# Closing punctuation never starts a line (simple kinsoku rule).
_NO_LINE_START = set("，。、！？；：）」』】》〉,.!?;:)]}%”’…")
AUDIO_BITRATE = 192_000


def target_bitrate(height: int) -> int:
    """Video bitrate near source quality for the resolution (bits per second)."""
    for minimum, bitrate in (
        (2160, 45_000_000),
        (1440, 24_000_000),
        (1080, 12_000_000),
        (720, 7_500_000),
        (480, 4_000_000),
    ):
        if height >= minimum:
            return bitrate
    return 2_500_000


def subtitle_font(language: str) -> str:
    """A macOS system family covering the language; libass falls back per glyph."""
    tag = language.lower()
    if tag.startswith("ja"):
        return "Hiragino Sans"
    if tag.startswith("ko"):
        return "Apple SD Gothic Neo"
    if tag.startswith("zh") and any(region in tag for region in ("-tw", "-hk", "-mo", "-hant")):
        return "Heiti TC"
    if tag.startswith("zh"):
        return "Heiti SC"
    return "Hiragino Sans GB"


@dataclass(frozen=True)
class SubtitleLayout:
    width: int
    height: int

    @property
    def font_size(self) -> int:
        # Portrait video caps the size by width so a line still holds enough text.
        return max(12, round(min(self.height, self.width) * FONT_SCALE))

    @property
    def outline(self) -> int:
        return max(1, round(self.font_size * 0.06))

    @property
    def margin_vertical(self) -> int:
        return round(self.height * MARGIN_SCALE)

    @property
    def margin_horizontal(self) -> int:
        return round(self.width * MARGIN_SCALE)

    @property
    def line_width(self) -> float:
        """Usable line width in em."""
        return max(1.0, (self.width - 2 * self.margin_horizontal) / self.font_size)


def _advance(char: str) -> float:
    return WIDE_ADVANCE if unicodedata.east_asian_width(char) in {"W", "F"} else NARROW_ADVANCE


def _units(line: str) -> list[str]:
    """Break opportunities: each wide character, each run of narrow text, each space."""
    units: list[str] = []
    for char in line:
        wide = unicodedata.east_asian_width(char) in {"W", "F"}
        if (
            units
            and not wide
            and not char.isspace()
            and not units[-1].isspace()
            and unicodedata.east_asian_width(units[-1][-1]) not in {"W", "F"}
        ):
            units[-1] += char
        else:
            units.append(char)
    return units


def wrap_line(line: str, width: float) -> list[str]:
    """Greedy wrap by estimated advance; long narrow words are split by character."""
    lines: list[str] = []
    current, used = "", 0.0
    for unit in _units(line):
        size = sum(_advance(char) for char in unit)
        if unit.isspace():
            if current:
                current, used = current + unit, used + size
            continue
        if current and used + size > width and unit[0] not in _NO_LINE_START:
            lines.append(current.rstrip())
            current, used = "", 0.0
        if size > width:
            for char in unit:
                if current and used + _advance(char) > width:
                    lines.append(current.rstrip())
                    current, used = "", 0.0
                current, used = current + char, used + _advance(char)
            continue
        current, used = current + unit, used + size
    if current.strip():
        lines.append(current.rstrip())
    return lines


def _ass_text(text: str) -> str:
    # libass has no escape for a backslash; a full-width one keeps \N, \h and tags inert.
    return text.replace("\\", "＼").replace("{", "\\{").replace("}", "\\}")


def _ass_time(centiseconds: int) -> str:
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def render_ass(cues: Sequence[Cue], language: str, layout: SubtitleLayout) -> str:
    """ASS document in source pixels; cue times keep the subtitle version's timing."""
    colour = "&H00FFFFFF"
    black = "&H00000000"
    style = ",".join(
        str(value)
        for value in (
            "Default",
            subtitle_font(language),
            layout.font_size,
            colour,
            colour,
            black,
            black,
            -1,  # bold
            0,
            0,
            0,
            100,
            100,
            0,
            0,
            1,  # outline plus drop shadow border style
            layout.outline,
            0,
            2,  # bottom centre
            layout.margin_horizontal,
            layout.margin_horizontal,
            layout.margin_vertical,
            1,
        )
    )
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {layout.width}",
        f"PlayResY: {layout.height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: {style}",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        wrapped = [
            _ass_text(part)
            for source_line in cue.text.splitlines()
            for part in wrap_line(source_line, layout.line_width)
        ]
        if not wrapped:
            continue
        # ASS keeps centiseconds; rounding must never collapse a cue to zero length.
        start = max(0, round(cue.start * 100))
        end = max(start + 1, round(cue.end * 100))
        text = "\\N".join(wrapped)
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def filter_value(value: str) -> str:
    """Escape a filter option value for both option and filtergraph parsing levels."""
    option = value.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return "".join("\\" + char if char in "\\'[],;" else char for char in option)
