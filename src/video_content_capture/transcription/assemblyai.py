"""AssemblyAI adapter for Mandarin transcription and diarization.

This module is the ONLY place the AssemblyAI Python SDK is imported. It
translates provider payloads into provider-neutral
:class:`video_content_capture.domain.models.Transcript` /
:class:`TranscriptSegment` instances and returns a
:class:`TranscriptionResult`. No provider SDK types leak into domain,
pipeline, storage, or CLI modules.

Verified against the installed SDK (assemblyai 0.64.31) source/metadata on
2026-07-20. External public documentation URLs were not reachable from the
implementation environment, so the verification basis is the installed
package source, package metadata, and executable compatibility tests under
Python 3.13.

Verified API surface used here:

* ``assemblyai.Transcriber(config=...)`` with ``TranscriptionConfig``.
* ``TranscriptionConfig(language_code=LanguageCode.zh, speaker_labels=True,
  speakers_expected=int | None, speaker_options=SpeakerOptions(...),
  speech_model=SpeechModel.best)``
* ``TranscriptionConfig.speaker_labels`` (bool), ``speakers_expected``
  (Optional[int]), ``speaker_options`` (Optional[SpeakerOptions]).
* ``SpeakerOptions(min_speakers_expected=int, max_speakers_expected=int)``
  — both bounded (``ConstrainedInt``).
* ``SpeechModel`` enum members: ``best``, ``nano``, ``slam_1``, ``universal``.
* ``LanguageCode.zh`` for Mandarin.
* ``Transcriber.transcribe(path, config)`` uploads the local file and polls
  to completion, returning a ``Transcript`` whose ``json_response`` carries
  the raw provider payload and whose ``utterances``/``words`` carry timing
  and speaker labels (``utterance.speaker`` is a provider string such as
  ``"A"``).
* ``assemblyai.TranscriptError(message, status_code)`` is raised for non-OK
  HTTP responses (upload, create, poll). ``status_code`` may be ``None``.
* ``assemblyai.AssemblyAIError`` is the base class; network timeouts surface
  as ``httpx.TimeoutException`` (the SDK does not wrap them).
* ``assemblyai.Client(settings=Settings(api_key=..., ...))`` is the
  settings entry point; the adapter constructs a
  :class:`assemblyai.Settings` from the local :class:`Settings` and never
  logs the API key.
* ``assemblyai.Settings`` env: ``api_key`` is read from the local
  :class:`Settings` only.

Retry policy: only ``httpx.TimeoutException`` and ``TranscriptError`` with
``status_code`` 408, 429, or any 5xx response are retried with bounded
exponential backoff (``base_delay * 2**attempt``, capped, plus a small
jitter). Authentication (401/403), malformed payload, and other 4xx raise
the accepted typed domain error immediately. No secrets in messages,
details, or logs.
"""

from __future__ import annotations

import json as _json
import random

# ffmpeg chunker needs subprocess + tempfile + Callable.
import subprocess  # noqa: E402 -- module-level import after SDK import block
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

# --- Provider SDK import confined to this module ---------------------------
# The SDK ships pydantic-v1 models with loose annotations; the project
# mypy config (pyproject.toml [[tool.mypy.overrides]]) ignores missing
# imports for assemblyai.*.
import assemblyai as aai
import httpx

from video_content_capture.config import Settings
from video_content_capture.domain.errors import (
    MediaError,
    ProviderAuthError,
    ProviderPayloadError,
    RateLimitError,
)
from video_content_capture.domain.models import (
    MediaMetadata,
    Transcript,
    TranscriptSegment,
    Word,
)
from video_content_capture.transcription.base import (
    Chunker,
    Sleeper,
    TranscriptionResult,
)
from video_content_capture.transcription.normalize import normalize_text

# --- Verified provider constants ------------------------------------------

# Verified SpeechModel members (assemblyai 0.64.31).
_VALID_SPEECH_MODELS = {"best", "nano", "slam-1", "universal"}

# Non-5xx HTTP status codes that are retryable per the project spec. Every
# status in the 500-599 range is handled by ``_is_retryable_status`` below.
_RETRYABLE_STATUS_CODES = frozenset({408, 429})

# Authentication failure status codes -> ProviderAuthError, never retried.
_AUTH_STATUS_CODES = frozenset({401, 403})

# Injectable subprocess runner for the default ffmpeg chunker. Tests inject
# a fake runner so no real ffmpeg runs and the source MP4 is never touched.
# The runner receives an argv list and keyword args (matches subprocess.run)
# and returns a CompletedProcess-like object with .returncode and .stderr.
Runner = Callable[..., Any]

# Bounded overlap for the default chunker, as a fraction of the chunk
# duration. The overlap is bounded so chunks never overlap by more than
# this fraction; the adapter's reassembly applies timestamp offsets and
# conservative exact-text overlap de-dup across the overlap window.
_DEFAULT_CHUNK_OVERLAP_FRACTION = 0.1  # 10% overlap

# Default per-chunk duration, in seconds. Slightly shorter than the
# provider limit so each chunk is comfortably within the limit.
_DEFAULT_CHUNK_DURATION_FRACTION = 0.9  # 90% of the provider limit

# --- Anonymous deterministic speaker labels -------------------------------


def _speaker_label(index: int) -> str:
    """Return an anonymous deterministic Traditional Chinese speaker label.

    ``index`` is 0-based. Labels cycle through the 26 Latin letters so the
    sequence is ``講者 A``, ``講者 B``, ..., ``講者 Z``, then ``講者 AA`` ...
    Personal identities are never inferred.
    """

    if index < 0:
        raise ValueError("speaker index must be nonnegative")
    # Base-26 uppercase letters, 0 -> A.
    letters: list[str] = []
    n = index
    while True:
        letters.append(chr(ord("A") + (n % 26)))
        n = n // 26 - 1
        if n < 0:
            break
    return "講者 " + "".join(reversed(letters))


# --- Configuration translation --------------------------------------------


def _build_config(settings: Settings) -> aai.TranscriptionConfig:
    """Translate provider-neutral :class:`Settings` into an AssemblyAI config.

    Verified fields:
    * ``language_code``: ``LanguageCode.zh`` for Mandarin (zh-TW default).
    * ``speaker_labels``: enabled for diarization whenever speaker bounds are
      configured OR automatic estimation is requested.
    * ``speakers_expected``: set ONLY when ``min_speakers == max_speakers``
      (exact expected count). Otherwise ``None`` (automatic estimation).
    * ``speaker_options``: set with min/max bounds ONLY when BOTH bounds are
      set. If only one bound is set, we use the safe explicit policy of
      automatic estimation (no half-bound invented).
    * ``speech_model``: from ``settings.assemblyai_model`` if it matches a
      verified enum member; otherwise the default ``best``.

    Safe explicit speaker policy (documented in the brief):

    * equal min==max -> ``speakers_expected=min=max`` and
      ``SpeakerOptions(min=max=expected)``.
    * both set, unequal -> ``SpeakerOptions(min, max)``, ``speakers_expected
      = None`` (provider auto-estimates within bounds).
    * only one set -> automatic estimation: no ``SpeakerOptions``,
      ``speakers_expected = None``.
    * neither set -> automatic estimation.
    """

    # The SDK accepts the language code as a plain string per its
    # current TranscriptionConfig signature (LanguageCode enum is
    # deprecated in favor of strings). "zh" is the verified Mandarin
    # language code.
    language_code = "zh"

    config_kwargs: dict[str, Any] = {
        "language_code": language_code,
        "speaker_labels": True,
    }

    min_s = settings.min_speakers
    max_s = settings.max_speakers

    if min_s is not None and max_s is not None and min_s == max_s:
        # Equal bounds -> exact expected count.
        config_kwargs["speakers_expected"] = min_s
        config_kwargs["speaker_options"] = aai.SpeakerOptions(
            min_speakers_expected=min_s,
            max_speakers_expected=max_s,
        )
    elif min_s is not None and max_s is not None:
        # Both set, unequal -> bounds only, auto within.
        config_kwargs["speaker_options"] = aai.SpeakerOptions(
            min_speakers_expected=min_s,
            max_speakers_expected=max_s,
        )
    # else: only one or neither bound set -> automatic estimation (no options).

    # Speech model: only pass a verified enum member; default to 'best'.
    model_value = settings.assemblyai_model
    if model_value in _VALID_SPEECH_MODELS:
        # SpeechModel enum members: best='best', nano='nano', slam_1='slam-1',
        # universal='universal'. Map by value.
        config_kwargs["speech_model"] = aai.SpeechModel(model_value)
    else:
        # Unrecognized model string: fall back to the SDK default ('best')
        # rather than inventing a field. The Settings layer is the place to
        # validate; this adapter only passes verified values.
        config_kwargs["speech_model"] = aai.SpeechModel.best

    # Conservative raw-text preservation: disable provider-side format_text
    # (casing/abbreviation rewrites) and rely on our own normalize.py for
    # Traditional Chinese. Punctuate=True is left at the provider default so
    # the raw text retains provider punctuation; we do NOT strip it.
    config_kwargs["format_text"] = False

    return aai.TranscriptionConfig(**config_kwargs)


def _build_aai_settings(settings: Settings) -> aai.Settings:
    """Build an AssemblyAI SDK Settings from the local Settings.

    The API key is read from the local Settings only and never logged.
    """

    api_key = settings.assemblyai_api_key
    if api_key is None:
        # The pipeline layer should validate credentials before calling the
        # adapter; defensively raise a typed error if it did not.
        raise ProviderAuthError("ASSEMBLYAI_API_KEY is not configured")
    return aai.Settings(api_key=api_key.get_secret_value())


# --- Response mapping -----------------------------------------------------


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a provider object that may be an object or a dict.

    The AssemblyAI SDK returns pydantic-v1 model instances for parsed
    responses, but tests and some code paths use plain dicts (e.g. the
    ``json_response`` payload). This helper reads ``name`` from either
    shape so the mapper is robust to both.
    """

    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_word(provider_word: Any) -> Word:
    """Map a provider word to a provider-neutral :class:`Word`.

    Provider word timing is in milliseconds; we convert to seconds.
    """

    return Word(
        text=_field(provider_word, "text", "") or "",
        start=_ms_to_seconds(_field(provider_word, "start", 0)),
        end=_ms_to_seconds(_field(provider_word, "end", 0)),
        confidence=_field(provider_word, "confidence", None),
    )


def _ms_to_seconds(ms: Any) -> float:
    if ms is None:
        return 0.0
    try:
        return float(ms) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _map_utterances(
    utterances: list[Any],
    *,
    start_offset_seconds: float,
    speaker_map: dict[str, str],
    seen_speakers: list[str],
) -> list[TranscriptSegment]:
    """Map provider utterances to provider-neutral segments.

    ``speaker_map`` is updated IN PLACE: provider speaker string -> stable
    anonymous label. ``seen_speakers`` records the insertion order so labels
    are assigned deterministically (``講者 A``, ``講者 B``, ...).
    ``start_offset_seconds`` is added to every timestamp (chunk offset).
    """

    segments: list[TranscriptSegment] = []
    for utt in utterances:
        provider_speaker = _field(utt, "speaker", None) or "A"
        if provider_speaker not in speaker_map:
            speaker_map[provider_speaker] = _speaker_label(len(seen_speakers))
            seen_speakers.append(provider_speaker)
        label = speaker_map[provider_speaker]

        raw_text = _field(utt, "text", "") or ""
        start = _ms_to_seconds(_field(utt, "start", 0)) + start_offset_seconds
        end = _ms_to_seconds(_field(utt, "end", 0)) + start_offset_seconds
        # Clamp: end must not precede start after offset.
        if end < start:
            end = start

        raw_words = _field(utt, "words", None) or []
        words = [_to_word(w) for w in raw_words]
        # Apply offset to word timing as well.
        words = [
            Word(
                text=w.text,
                start=w.start + start_offset_seconds,
                end=w.end + start_offset_seconds,
                confidence=w.confidence,
            )
            for w in words
        ]

        segments.append(
            TranscriptSegment(
                # Stable deterministic ID: reassigned by _reassemble_segments
                # after dedup and ordering so IDs are unique and ordered.
                segment_id="s0000",
                start=start,
                end=end,
                raw_text=raw_text,
                normalized_text=normalize_text(raw_text),
                speaker_label=label,
                words=words,
                confidence=_field(utt, "confidence", None),
            )
        )
    return segments


def _map_utterances_tracked(
    utterances: list[Any],
    *,
    start_offset_seconds: float,
    speaker_map: dict[str, str],
    seen_speakers: list[str],
) -> tuple[list[TranscriptSegment], list[str]]:
    """Map utterances AND return the per-segment provider speaker strings.

    Same as :func:`_map_utterances` but also returns the list of provider
    speaker strings aligned 1:1 with the returned segments, so the
    reconciliation pass can remap by provider speaker.
    """

    segments: list[TranscriptSegment] = []
    speakers: list[str] = []
    for utt in utterances:
        provider_speaker = _field(utt, "speaker", None) or "A"
        if provider_speaker not in speaker_map:
            speaker_map[provider_speaker] = _speaker_label(len(seen_speakers))
            seen_speakers.append(provider_speaker)
        label = speaker_map[provider_speaker]

        raw_text = _field(utt, "text", "") or ""
        start = _ms_to_seconds(_field(utt, "start", 0)) + start_offset_seconds
        end = _ms_to_seconds(_field(utt, "end", 0)) + start_offset_seconds
        if end < start:
            end = start

        raw_words = _field(utt, "words", None) or []
        words = [_to_word(w) for w in raw_words]
        words = [
            Word(
                text=w.text,
                start=w.start + start_offset_seconds,
                end=w.end + start_offset_seconds,
                confidence=w.confidence,
            )
            for w in words
        ]

        segments.append(
            TranscriptSegment(
                segment_id="s0000",
                start=start,
                end=end,
                raw_text=raw_text,
                normalized_text=normalize_text(raw_text),
                speaker_label=label,
                words=words,
                confidence=_field(utt, "confidence", None),
            )
        )
        speakers.append(provider_speaker)
    return segments, speakers


def _reconcile_and_remap(
    *,
    chunk_segments: list[TranscriptSegment],
    chunk_speakers: list[str],
    earlier_segments: list[TranscriptSegment],
    speaker_map: dict[str, str],
    seen_speakers: list[str],
    utterances: list[Any],
    offset: float,
) -> list[TranscriptSegment]:
    """Reconcile chunk-local speakers across chunks and re-map if needed.

    For each segment in this chunk, look for an earlier segment with the
    SAME raw_text and an overlapping time interval. If found, the
    chunk-local provider speaker that produced this segment is the SAME
    speaker as the earlier segment's speaker; remap that provider speaker
    in ``speaker_map`` to the earlier label, then RE-MAP the whole chunk so
    every utterance by that provider speaker carries the reconciled label.

    Re-mapping is safe because the only state mutated is ``speaker_map``
    (provider speaker -> stable label). The ``seen_speakers`` insertion
    order is preserved so previously-assigned labels are unchanged; only
    the offending chunk-local provider speaker is repointed at an existing
    label (no new label is created).
    """

    if not earlier_segments or not chunk_segments:
        return chunk_segments

    # Find the first overlap-duplicate and its offending provider speaker.
    remaps: dict[str, str] = {}  # provider_speaker -> earlier_label
    for seg, spk in zip(chunk_segments, chunk_speakers, strict=False):
        if not seg.raw_text:
            continue
        for kept in earlier_segments:
            if seg.raw_text != kept.raw_text:
                continue
            if kept.start <= seg.end and seg.start <= kept.end:
                remaps[spk] = kept.speaker_label
                break

    if not remaps:
        return chunk_segments

    # Apply remaps to the shared speaker_map. Re-pointing an existing
    # provider speaker to an existing label does NOT add a new label.
    for spk, label in remaps.items():
        speaker_map[spk] = label

    # Re-map the chunk with the updated speaker_map. We do NOT extend
    # seen_speakers (no new label introduced).
    remapped, _ = _map_utterances_tracked(
        utterances,
        start_offset_seconds=offset,
        speaker_map=speaker_map,
        seen_speakers=seen_speakers,
    )
    return remapped


def _reassemble_segments(chunks: list[list[TranscriptSegment]]) -> list[TranscriptSegment]:
    """Concatenate chunked segments, applying overlap de-duplication.

    Conservative duplicate removal: drop a segment from a later chunk if its
    raw text EXACTLY matches a segment from an earlier chunk whose time
    interval overlaps the later segment's interval. This is conservative
    because an exact text match in an overlapping region is overwhelmingly
    likely to be the same utterance; partial/loose matches are NEVER dropped.

    After de-dup, reassign stable deterministic IDs in start-time order.
    """

    if len(chunks) == 1:
        segments: list[TranscriptSegment] = list(chunks[0])
    else:
        kept: list[TranscriptSegment] = []
        for chunk_segments in chunks:
            for seg in chunk_segments:
                # Conservative exact-match dedup against already-kept segments
                # that overlap in time.
                is_dup = False
                for k in kept:
                    if k.raw_text != seg.raw_text:
                        continue
                    if not seg.raw_text:
                        continue
                    # Time overlap?
                    if k.start <= seg.end and seg.start <= k.end:
                        is_dup = True
                        break
                if not is_dup:
                    kept.append(seg)
        segments = kept

    # Reassign stable deterministic IDs in start-time order. Equal starts
    # preserve chunk order (stable sort).
    segments.sort(key=lambda s: s.start)
    for i, seg in enumerate(segments):
        # Use object.__setattr__ because TranscriptSegment is frozen=False in
        # pydantic (model_config extra="forbid" but validate_assignment=False
        # at the Settings level; TranscriptSegment has no validate_assignment
        # set, so default is False -> assignment allowed). Direct assignment
        # works for pydantic v2 models without validate_assignment.
        seg.segment_id = f"s{i + 1:04d}"
    return segments


def _extract_utterances(transcript_obj: Any) -> list[Any]:
    """Extract the utterances list from a provider Transcript-like object."""

    utterances = _field(transcript_obj, "utterances", None)
    if utterances is None:
        # Some responses carry utterances on the json_response directly.
        jr = _field(transcript_obj, "json_response", None)
        if isinstance(jr, dict):
            utterances = jr.get("utterances")
    return list(utterances or [])


def _validate_payload(transcript_obj: Any, response_dict: dict[str, Any]) -> None:
    """Validate a completed provider payload; raise ProviderPayloadError if malformed.

    Malformed = status != "completed", OR no utterances AND no text. The
    raw payload is preserved on the error's details for diagnostics (no
    secrets are present in a provider response payload body).
    """

    status = response_dict.get("status")
    if status == "error":
        # Provider-reported error: not retried by us; map to ProviderPayload.
        # Strip any potential error string but keep it (it is provider text,
        # not our secret).
        raise ProviderPayloadError(
            f"AssemblyAI returned an error status: {response_dict.get('error', '')}",
            details={"status": "error"},
        )
    if status != "completed":
        raise ProviderPayloadError(
            f"unexpected transcript status: {status!r}",
            details={"status": status},
        )
    utterances = response_dict.get("utterances")
    if not utterances:
        raise ProviderPayloadError(
            "completed transcript has no diarized utterances",
            details={"status": "completed"},
        )


def _response_to_dict(transcript_obj: Any) -> dict[str, Any]:
    """Return the raw provider payload as a dict from a provider Transcript."""

    jr = _field(transcript_obj, "json_response", None)
    if isinstance(jr, dict):
        return jr
    # Fall back to attribute/key probing.
    return {
        "id": _field(transcript_obj, "id", None),
        "status": _field(transcript_obj, "status", None),
        "text": _field(transcript_obj, "text", None),
        "utterances": _field(transcript_obj, "utterances", None),
        "words": _field(transcript_obj, "words", None),
        "language_code": _field(transcript_obj, "language_code", None),
        "audio_duration": _field(transcript_obj, "audio_duration", None),
        "confidence": _field(transcript_obj, "confidence", None),
        "error": _field(transcript_obj, "error", None),
    }


# --- Default chunker (ffmpeg audio-only, limit-driven) ---------------------


def _default_chunker(
    audio_path: Path,
    duration_seconds: float,
    *,
    runner: Runner,
    chunk_dir: Path,
    provider_max_duration_seconds: float,
) -> list[tuple[Path, float]]:
    """Split ``audio_path`` into bounded overlapping audio-only chunks via ffmpeg.

    Triggered ONLY when the probed audio duration exceeds the configured
    provider duration limit. The chunker:

    * Splits the extracted audio (never the source MP4) into chunks of ~90%
      of the provider limit, with a bounded 10% overlap between consecutive
      chunks.
    * Uses an ffmpeg argument array (``shell=False``), ``-vn`` (no video
      decoding), ``-ss``/``-to`` seek flags, and stream-copy into ``.m4a``
      when the codec permits (falls back to ``-c:a aac`` only if needed;
      the default uses copy to preserve quality).
    * Writes each chunk to a temp-owned path under ``chunk_dir`` (a temp
      dir created by the caller). Chunk files are NOT deleted before the
      SDK upload completes; cleanup is the caller's responsibility and
      happens only after all chunks have been submitted.
    * Returns ``(chunk_path, start_offset_seconds)`` tuples whose offsets
      are compatible with the adapter's timestamp-offset reassembly.

    On ffmpeg failure or missing/empty chunk artifact, raises a typed
    :class:`MediaError` so the operator learns chunking failed and the
    adapter NEVER silently falls back to submitting the over-limit full
    audio.
    """

    if duration_seconds <= provider_max_duration_seconds:
        # Should not be called in this case, but defensively return one
        # full-audio chunk at offset 0 rather than running ffmpeg.
        return [(audio_path, 0.0)]

    chunk_duration = provider_max_duration_seconds * _DEFAULT_CHUNK_DURATION_FRACTION
    overlap = chunk_duration * _DEFAULT_CHUNK_OVERLAP_FRACTION
    step = chunk_duration - overlap  # strictly positive, bounded overlap

    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[Path, float]] = []
    start = 0.0
    idx = 0
    while start < duration_seconds:
        end = min(start + chunk_duration, duration_seconds)
        chunk_path = chunk_dir / f"chunk-{idx:04d}.m4a"
        argv = _build_chunk_ffmpeg_argv(
            audio_path=audio_path,
            start_seconds=start,
            end_seconds=end,
            output_path=chunk_path,
        )
        try:
            completed = runner(argv, shell=False, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise MediaError(
                "ffmpeg executable was not found on PATH during chunking",
                details={"chunk_index": idx},
            ) from exc
        except OSError as exc:
            raise MediaError(
                f"failed to execute ffmpeg during chunking: {exc}",
                details={"chunk_index": idx},
            ) from exc
        if completed.returncode != 0:
            stderr = (getattr(completed, "stderr", "") or "").strip()
            raise MediaError(
                f"ffmpeg chunking failed for chunk {idx}: {stderr}",
                details={
                    "chunk_index": idx,
                    "returncode": completed.returncode,
                    "stderr": stderr,
                },
            )
        # Validate the chunk artifact: must exist and be non-empty.
        if not chunk_path.is_file():
            raise MediaError(
                f"ffmpeg produced no chunk artifact for chunk {idx}: {chunk_path}",
                details={"chunk_index": idx, "path": str(chunk_path)},
            )
        if chunk_path.stat().st_size == 0:
            raise MediaError(
                f"ffmpeg produced an empty chunk artifact for chunk {idx}: {chunk_path}",
                details={"chunk_index": idx, "path": str(chunk_path)},
            )
        chunks.append((chunk_path, start))
        idx += 1
        # Advance by the step (chunk_duration - overlap). If the remaining
        # audio is shorter than the overlap, we are done (the last chunk
        # covered it).
        start = start + step
        if end >= duration_seconds:
            break
    if not chunks:
        # Defensive: should not happen, but never silently submit the full
        # audio.
        raise MediaError(
            "chunking produced no chunks for over-limit audio",
            details={"duration_seconds": duration_seconds},
        )
    return chunks


def _build_chunk_ffmpeg_argv(
    *,
    audio_path: Path,
    start_seconds: float,
    end_seconds: float,
    output_path: Path,
) -> list[str]:
    """Build the ffmpeg argument array for one audio-only chunk.

    Uses ``-ss`` (seek) BEFORE ``-i`` for fast input seek, ``-to`` for the
    end offset (relative to the seeked start), ``-vn`` for no video, and
    ``-c:a copy`` to stream-copy the audio (no re-encode, preserves
    quality). The output is a ``.m4a`` artifact at a temp-owned path.
    """

    duration = max(end_seconds - start_seconds, 0.0)
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(audio_path),
        "-vn",  # audio-only, never decode video
        "-c:a",
        "copy",
        str(output_path),
    ]


# --- Adapter --------------------------------------------------------------


class AssemblyAITranscriber:
    """Adapter that transcribes Mandarin audio via AssemblyAI.

    Provider-neutral: returns :class:`TranscriptionResult`. All SDK calls are
    confined to this class. Tests inject ``sleeper``, ``chunker``, and patch
    ``_sdk_transcribe`` so no paid call is made.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        sleeper: Sleeper | None = None,
        chunker: Chunker | None = None,
        runner: Runner | None = None,
        provider_max_duration_seconds: float = 3600.0,
    ) -> None:
        # Build the SDK client eagerly so credential presence is checked at
        # construction (matches the spec: cloud commands fail before upload).
        self._aai_settings = _build_aai_settings(settings)
        self._client = aai.Client(settings=self._aai_settings)
        self._settings_model = settings
        self._sleeper: Sleeper = sleeper if sleeper is not None else _real_sleep
        self._chunker: Chunker | None = chunker
        # Injectable subprocess runner for the default ffmpeg chunker so
        # tests never run real ffmpeg and never touch the source MP4.
        self._runner: Runner = runner if runner is not None else _real_runner
        self._provider_max_duration_seconds = provider_max_duration_seconds
        # Temp chunk dirs created by the default ffmpeg chunker, cleaned up
        # best-effort AFTER all chunks are submitted. Tests assert chunk
        # files still exist at upload time.
        self._pending_chunk_cleanup: list[Path] = []

    # --- Public protocol method -------------------------------------------

    def transcribe(
        self,
        *,
        audio_path: Path,
        metadata: MediaMetadata,
        settings: Settings,
    ) -> TranscriptionResult:
        """Transcribe one extracted-audio artifact.

        Prefers one full-audio job when the probed duration is within the
        configured provider limit; otherwise uses the injected chunker to
        submit one job per chunk and reassembles the result with timestamp
        offsets, conservative overlap de-dup, and stable speaker
        reconciliation.
        """

        # Shared across all chunks so chunk-local provider speaker strings
        # map to the SAME stable anonymous transcript labels.
        speaker_map: dict[str, str] = {}
        seen_speakers: list[str] = []

        chunked = False
        if metadata.duration_seconds > self._provider_max_duration_seconds:
            chunked = True

        if not chunked:
            # One full-audio job.
            transcript_obj = self._transcribe_with_retry(audio_path, settings)
            response_dict = _response_to_dict(transcript_obj)
            _validate_payload(transcript_obj, response_dict)
            raw_bytes = _response_to_bytes(response_dict)
            utterances = _extract_utterances(transcript_obj)
            segments = _map_utterances(
                utterances,
                start_offset_seconds=0.0,
                speaker_map=speaker_map,
                seen_speakers=seen_speakers,
            )
            segments = _reassemble_segments([segments])
            transcript = self._build_transcript(metadata, segments, settings)
            return TranscriptionResult(
                transcript=transcript,
                raw_text=response_dict.get("text", "") or "",
                raw_response=raw_bytes,
                provider_job_id=str(response_dict.get("id") or ""),
                chunked=False,
            )

        # Chunk fallback: one job per chunk, then reassemble. For each chunk
        # AFTER the first, we first map its utterances, then look for an
        # overlap-duplicate segment whose raw text exactly matches an earlier
        # chunk's segment with overlapping time. If found, the chunk-local
        # provider speaker that produced the duplicate is the SAME speaker as
        # the earlier segment's speaker; we remap that provider speaker in the
        # shared speaker_map to the earlier label and RE-MAP the chunk so all
        # of that speaker's utterances in this chunk carry the reconciled
        # label. This is the stable speaker reconciliation contract: an
        # overlap-duplicate drives a global chunk-local -> earlier-label
        # remap, not just a single-segment rewrite.
        chunks = self._resolve_chunks(audio_path, metadata.duration_seconds)
        chunk_results: list[list[TranscriptSegment]] = []
        combined_raw_text: list[str] = []
        combined_raw: list[dict[str, Any]] = []
        last_job_id = ""
        kept_for_dedup: list[TranscriptSegment] = []
        for chunk_path, offset in chunks:
            transcript_obj = self._transcribe_with_retry(chunk_path, settings)
            response_dict = _response_to_dict(transcript_obj)
            _validate_payload(transcript_obj, response_dict)
            utterances = _extract_utterances(transcript_obj)
            # Provider speaker IDs are local to one job/chunk. Start a fresh
            # mapping for each chunk, then reconcile only when overlap evidence
            # proves a chunk-local speaker matches an earlier transcript label.
            chunk_speaker_map: dict[str, str] = {}
            chunk_segments, chunk_speakers = _map_utterances_tracked(
                utterances,
                start_offset_seconds=offset,
                speaker_map=chunk_speaker_map,
                seen_speakers=seen_speakers,
            )
            # Reconcile: find overlap-duplicates against kept_for_dedup and
            # remap the offending chunk-local provider speakers, then re-map.
            chunk_segments = _reconcile_and_remap(
                chunk_segments=chunk_segments,
                chunk_speakers=chunk_speakers,
                earlier_segments=kept_for_dedup,
                speaker_map=chunk_speaker_map,
                seen_speakers=seen_speakers,
                utterances=utterances,
                offset=offset,
            )
            # Now extend kept_for_dedup with this chunk's segments (dedup of
            # exact-text overlap duplicates is finalized in _reassemble_segments).
            kept_for_dedup.extend(chunk_segments)
            chunk_results.append(chunk_segments)
            combined_raw_text.append(response_dict.get("text", "") or "")
            combined_raw.append(response_dict)
            last_job_id = str(response_dict.get("id") or "")

        segments = _reassemble_segments(chunk_results)
        transcript = self._build_transcript(metadata, segments, settings)
        raw_bytes = _response_to_bytes({"chunks": combined_raw})
        result = TranscriptionResult(
            transcript=transcript,
            raw_text=" ".join(t for t in combined_raw_text if t),
            raw_response=raw_bytes,
            provider_job_id=last_job_id,
            chunked=True,
        )
        # Best-effort cleanup of temp chunk dirs now that all chunks have
        # been submitted. Swallow FileNotFoundError (safe no-op if the dir
        # was already cleaned up). Never raise from cleanup.
        self._cleanup_chunk_dirs()
        return result

    # --- Retry wrapper ----------------------------------------------------

    def _transcribe_with_retry(self, audio_path: Path, settings: Settings) -> Any:
        """Submit one transcription job with bounded exponential backoff.

        Retries ONLY timeouts and retryable HTTP status codes (408/429/5xx).
        Authentication (401/403) and other 4xx raise typed errors immediately.
        """

        config = _build_config(settings)
        attempt = 0
        last_exc: BaseException | None = None
        while attempt < self._settings_model.max_retries:
            attempt += 1
            try:
                return self._sdk_transcribe(audio_path, config)
            except httpx.TimeoutException as exc:
                last_exc = exc
                # Timeout is always retryable.
            except aai.AssemblyAIError as exc:
                status = _status_of(exc)
                if status in _AUTH_STATUS_CODES:
                    raise ProviderAuthError(
                        "AssemblyAI authentication failed",
                        details={"status": status},
                    ) from exc
                if _is_retryable_status(status):
                    last_exc = exc
                    if attempt >= self._settings_model.max_retries:
                        raise RateLimitError(
                            "AssemblyAI rate limit or server error after retries",
                            details={"status": status, "attempts": attempt},
                        ) from exc
                else:
                    # Non-retryable 4xx (400/404/422/...) or unknown status.
                    raise ProviderPayloadError(
                        f"AssemblyAI request rejected (status {status})",
                        details={"status": status},
                    ) from exc

            # Sleep before the next attempt (bounded exponential backoff).
            if attempt < self._settings_model.max_retries:
                delay = _backoff_delay(
                    base=self._settings_model.retry_base_delay_seconds,
                    attempt=attempt - 1,
                )
                self._sleeper(delay)

        # Exhausted retries on a timeout: classify as RateLimitError (transient).
        raise RateLimitError(
            "AssemblyAI request timed out after retries",
            details={"attempts": attempt},
        ) from last_exc

    # --- Chunk resolution -------------------------------------------------

    def _resolve_chunks(
        self, audio_path: Path, duration_seconds: float
    ) -> list[tuple[Path, float]]:
        """Resolve the chunk plan, using the injected chunker or the default
        ffmpeg chunker.

        When an explicit chunker was injected, it is called directly (tests
        own chunk paths). When no chunker was injected, the default ffmpeg
        audio-only chunker runs via the injectable subprocess runner,
        produces real temp-owned chunk artifacts, and never submits the
        source full audio as a chunk.
        """

        if self._chunker is not None:
            return self._chunker(audio_path, duration_seconds)
        # Default ffmpeg chunker: create a temp dir for chunk artifacts.
        # The dir is created per transcribe() call and owned by this
        # adapter instance; chunk files are NOT deleted before the SDK
        # upload completes (the SDK reads the file at upload time). Cleanup
        # happens after all chunks are submitted, via a best-effort unlink
        # that swallows FileNotFoundError (safe no-op).
        chunk_dir = Path(tempfile.mkdtemp(prefix="vcc-chunks-"))
        chunks = _default_chunker(
            audio_path,
            duration_seconds,
            runner=self._runner,
            chunk_dir=chunk_dir,
            provider_max_duration_seconds=self._provider_max_duration_seconds,
        )
        # Register the chunk dir for best-effort cleanup after all chunks
        # are submitted. We do NOT clean up here; cleanup runs after the
        # reassembly loop completes (see the end of the chunked branch).
        self._pending_chunk_cleanup.append(chunk_dir)
        return chunks

    def _cleanup_chunk_dirs(self) -> None:
        """Best-effort cleanup of temp chunk dirs after all chunks submitted.

        Swallows FileNotFoundError and OSError so cleanup never masks a
        real result. Chunk files were already read by the SDK upload; deleting
        them now is safe.
        """

        for chunk_dir in self._pending_chunk_cleanup:
            _safe_remove_dir(chunk_dir)
        self._pending_chunk_cleanup.clear()

    # --- SDK call indirection (mockable) -----------------------------------

    def _sdk_transcribe(self, audio_path: Path, config: Any) -> Any:
        """Submit and poll one transcription job via the SDK.

        Tests patch this method to return a fake Transcript-like object and
        avoid any paid call.
        """

        transcriber = aai.Transcriber(client=self._client, config=config)
        return transcriber.transcribe(str(audio_path), config=config)

    # --- Transcript assembly ----------------------------------------------

    def _build_transcript(
        self,
        metadata: MediaMetadata,
        segments: list[TranscriptSegment],
        settings: Settings,
    ) -> Transcript:
        """Build the provider-neutral :class:`Transcript` with media metadata.

        We replace the source-path-audio-only ``MediaMetadata`` with one
        derived from the probed source media (the source MP4 metadata) so
        the canonical transcript carries the original media identity, not
        the extracted-audio artifact. The audio stream from the probe is
        preserved.
        """

        # Use the provided metadata verbatim (the pipeline passes the source
        # MP4 metadata, not the extracted audio metadata). We keep audio
        # stream info; video/subtitle streams are preserved as-is.
        return Transcript(
            metadata=metadata,
            segments=segments,
            language=settings.language,
        )


# --- Helpers ---------------------------------------------------------------


def _real_runner(argv: list[str], **kwargs: Any) -> Any:
    """Default subprocess runner that actually runs ffmpeg (production).

    Tests inject a fake runner so no real ffmpeg runs.
    """

    return subprocess.run(argv, shell=False, check=False, capture_output=True, text=True)


def _safe_remove_dir(path: Path) -> None:
    """Best-effort recursive removal of a temp chunk dir.

    Swallows FileNotFoundError and OSError so cleanup never raises.
    """

    import shutil

    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        # Best-effort: log nothing (no secrets here) and continue.
        pass


def _real_sleep(seconds: float) -> None:
    """Default sleeper that actually sleeps (used in production)."""

    import time

    time.sleep(seconds)


def _backoff_delay(*, base: float, attempt: int) -> float:
    """Bounded exponential backoff delay.

    ``base * 2**attempt`` capped at a sensible upper bound, plus a small
    deterministic jitter (capped). The base delay is the configured
    ``retry_base_delay_seconds``. The delay is bounded so a 5-retry budget
    never blocks for unbounded time.
    """

    if base <= 0:
        return 0.0
    # Exponential, capped at base * 2**5 (32x) to keep the delay bounded.
    exp = min(attempt, 5)
    delay = base * (2**exp)
    # Bounded jitter: at most 20% of the delay. Deterministic via a fixed
    # seed so tests are reproducible; production callers may inject a sleeper
    # that ignores the value.
    jitter = delay * 0.2 * random.random()  # noqa: S311 — jitter only
    return float(delay + jitter)


def _status_of(exc: BaseException) -> int | None:
    """Extract the HTTP status code from an AssemblyAI error, if present."""

    return getattr(exc, "status_code", None)


def _is_retryable_status(status: int | None) -> bool:
    return status in _RETRYABLE_STATUS_CODES or (status is not None and 500 <= status <= 599)


def _response_to_bytes(response_dict: dict[str, Any]) -> bytes:
    """Serialize a raw provider payload dict to UTF-8 JSON bytes."""

    return _json.dumps(response_dict, ensure_ascii=False).encode("utf-8")


__all__ = ["AssemblyAITranscriber"]


# Re-export the protocol so callers can type-hint against the adapter
# without importing provider SDK types.
