"""Resolution-only quality choice: resolve the actual source version from what the user picks."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from video_content_capture.workspace.youtube import (
    AudioTrack,
    SourceMetadata,
    VideoFormat,
    _codec,
)

SubtitleForm = Literal["burned", "tracks"]
SUBTITLE_FORMS: tuple[SubtitleForm, ...] = ("burned", "tracks")
DEFAULT_MAX_HEIGHT = 1080


class ResolvedSource(BaseModel):
    """Source format and audio track the system downloads for one resolution choice."""

    model_config = ConfigDict(frozen=True)
    height: int
    subtitle_form: SubtitleForm
    format_id: str
    audio_id: str
    fps: float
    video_codec: str
    audio_codec: str
    # Container of the downloaded source and of a selectable-track export (MP4 only for H.264+AAC).
    container: Literal["mp4", "mkv"]
    # Burned-in exports are always re-encoded to MP4 (ADR 0004).
    output_container: Literal["mp4", "mkv"]


def available_resolutions(source: SourceMetadata) -> list[int]:
    """Distinct actual source heights, highest first; never upscaled or invented."""
    return sorted({item.height for item in source.formats}, reverse=True)


def default_resolution(source: SourceMetadata) -> int:
    """Highest height not above 1080p; the lowest one when every source is above 1080p."""
    heights = available_resolutions(source)
    below = [height for height in heights if height <= DEFAULT_MAX_HEIGHT]
    return max(below) if below else min(heights)


def default_audio(source: SourceMetadata) -> AudioTrack:
    """The original-language default track chosen at query time; never user-selectable."""
    return next(track for track in source.audio_tracks if track.id == source.default_audio_id)


def _best(candidates: list[tuple[int, VideoFormat]]) -> VideoFormat:
    # Highest FPS first; the extractor lists formats worst to best, so a later entry wins ties.
    return max(candidates, key=lambda pair: (pair[1].fps, pair[0]))[1]


def resolve_source(
    source: SourceMetadata, height: int, subtitle_form: SubtitleForm
) -> ResolvedSource:
    """Pure (resolution, subtitle form) → source format/audio rule; raises ValueError if absent.

    - burned: highest FPS, then the downloader's best quality, any codec.
    - tracks: prefer H.264 video with an AAC copy of the default audio track (MP4);
      otherwise highest FPS, then best quality, with the default audio track (MKV unless
      the result happens to be H.264+AAC).
    """
    if subtitle_form not in SUBTITLE_FORMS:
        raise ValueError("Unknown subtitle form")
    candidates = [
        (index, item) for index, item in enumerate(source.formats) if item.height == height
    ]
    if not candidates:
        raise ValueError("Resolution is not available from the source")
    audio = default_audio(source)
    video = _best(candidates)
    if subtitle_form == "tracks":
        h264 = [pair for pair in candidates if _codec(pair[1].codec) == "h264"]
        # Same logical track (language and role), possibly offered in several codecs; muxed
        # video formats are not audio-track variants.
        muxed = {item.id for item in source.formats} - {audio.id}
        aac = [
            track
            for track in source.audio_tracks
            if _codec(track.codec) == "aac"
            and track.id not in muxed
            and (track.language, track.original, track.default)
            == (audio.language, audio.original, audio.default)
        ]
        if h264 and aac:
            video, audio = _best(h264), aac[-1]
    video_codec, audio_codec = _codec(video.codec), _codec(audio.codec)
    container: Literal["mp4", "mkv"] = (
        "mp4" if video_codec == "h264" and audio_codec == "aac" else "mkv"
    )
    return ResolvedSource(
        height=height,
        subtitle_form=subtitle_form,
        format_id=video.id,
        audio_id=audio.id,
        fps=video.fps,
        video_codec=video_codec,
        audio_codec=audio_codec,
        container=container,
        output_container="mp4" if subtitle_form == "burned" else container,
    )


def quality_options(source: SourceMetadata) -> dict[str, object]:
    """Public resolution menu: each height with its resolution per subtitle form."""
    audio = default_audio(source)
    return {
        "resolutions": [
            {
                "height": height,
                **{
                    form: resolve_source(source, height, form).model_dump(mode="json")
                    for form in SUBTITLE_FORMS
                },
            }
            for height in available_resolutions(source)
        ],
        "default_resolution": default_resolution(source),
        "above_1080p": all(height > DEFAULT_MAX_HEIGHT for height in available_resolutions(source)),
        "audio": audio.model_dump(mode="json"),
    }
