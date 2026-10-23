"""Gemini subtitle recognition against a fake provider and a fake ffmpeg."""

import json
from itertools import pairwise
from pathlib import Path
from threading import Event

import httpx
import pytest
from google.genai import errors, types
from pydantic import SecretStr

from video_content_capture.workspace.asr import CHUNK_SECONDS, GeminiSubtitleAdapter
from video_content_capture.workspace.config import WorkspaceSettings
from video_content_capture.workspace.source import SourceError


def reply(language: str, *segments: tuple[float, float, str]) -> types.GenerateContentResponse:
    payload = {
        "language": language,
        "segments": [{"start": s, "end": e, "text": t} for s, e, t in segments],
    }
    return response(json.dumps(payload, ensure_ascii=False))


def response(
    text: str,
    finish: types.FinishReason = types.FinishReason.STOP,
    blocked: bool = False,
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason=finish,
                content=types.Content(role="model", parts=[types.Part(text=text)]),
            )
        ],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(
            block_reason=types.BlockedReason.SAFETY
        )
        if blocked
        else None,
    )


class FakeGemini:
    """Stands in for genai.Client; each request receives the next scripted reply."""

    def __init__(self, replies: list[object], on_request=None) -> None:
        self.replies = list(replies)
        self.on_request = on_request
        self.audio: list[bytes] = []
        self.client_options: list[dict[str, object]] = []
        self.models = self

    def __call__(self, **options: object) -> "FakeGemini":
        self.client_options.append(options)
        return self

    def __enter__(self) -> "FakeGemini":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def generate_content(self, *, model: str, contents: list[object], config: object):
        part = contents[0]
        assert isinstance(part, types.Part) and part.inline_data is not None
        assert part.inline_data.mime_type == "audio/ogg"
        self.audio.append(part.inline_data.data or b"")
        if self.on_request:
            self.on_request()
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeFFmpeg:
    """Writes the requested chunk's start offset as its audio bytes."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], cancel: Event) -> str:
        self.commands.append(command)
        if self.fail:
            raise SourceError("source_unavailable", "failed")
        offset = command[command.index("-ss") + 1]
        Path(command[-1]).write_bytes(f"audio@{offset}".encode())
        return ""


def settings(key: str | None = "fake-asr-key") -> WorkspaceSettings:
    return WorkspaceSettings(
        Path("."),
        Path("library"),
        asr_model="fake-asr-model",
        gemini_api_key=SecretStr(key) if key else None,
    )


def transcribe(gemini: FakeGemini, duration: float, cancel: Event | None = None, ffmpeg=None):
    adapter = GeminiSubtitleAdapter(settings(), gemini, ffmpeg or FakeFFmpeg())
    return adapter.transcribe(Path("media.mp4"), "en", duration, cancel or Event())


def test_chunks_are_recognised_in_order_and_shifted_to_absolute_time() -> None:
    gemini = FakeGemini(
        [
            reply("ja", (0.5, 4.0, "一つ目"), (170.0, 179.5, "二つ目")),
            reply("ja", (1.0, 179.0, "三つ目")),
            reply("ja", (0.0, 29.5, "四つ目")),
        ]
    )
    language, cues = transcribe(gemini, 2 * CHUNK_SECONDS + 30)
    assert gemini.audio == [b"audio@0.000", b"audio@180.000", b"audio@360.000"]
    assert language == "ja"
    assert [(cue.id, cue.start, cue.end, cue.text) for cue in cues] == [
        ("c000001", 0.5, 4.0, "一つ目"),
        ("c000002", 170.0, 179.5, "二つ目"),
        ("c000003", 181.0, 359.0, "三つ目"),
        ("c000004", 360.0, 389.5, "四つ目"),
    ]


def test_a_chunk_that_stops_early_is_heard_again_from_where_it_stopped() -> None:
    gemini = FakeGemini(
        [
            reply("en", (0.0, 10.0, "one"), (10.0, 150.1234, "two")),
            # Speech resumes at the clip start; the rounded -ss lands just before 150.1234.
            reply("en", (0.0, 4.0, "three"), (4.0, 175.0, "four")),
            reply("en", (0.0, 74.5, "five")),
        ]
    )
    _, cues = transcribe(gemini, 400)
    assert gemini.audio == [b"audio@0.000", b"audio@150.123", b"audio@325.123"]
    times = [(cue.start, cue.end) for cue in cues]
    assert times == pytest.approx(
        [(0, 10), (10, 150.1234), (150.1234, 154.123), (154.123, 325.123), (325.123, 399.623)]
    )
    assert all(left[1] <= right[0] for left, right in pairwise(times))


def test_a_chunk_with_no_speech_advances_by_the_whole_chunk() -> None:
    gemini = FakeGemini([reply("en"), reply("en"), reply("en", (0.0, 1.0, "end"))])
    _, cues = transcribe(gemini, 2 * CHUNK_SECONDS + 10)
    assert gemini.audio == [b"audio@0.000", b"audio@180.000", b"audio@360.000"]
    assert [(cue.start, cue.end) for cue in cues] == [(360.0, 361.0)]


def test_a_chunk_that_returns_almost_nothing_still_advances_the_minimum() -> None:
    gemini = FakeGemini([reply("en", (0.0, 1.0, "hi")) for _ in range(4)])
    _, cues = transcribe(gemini, 120)
    assert gemini.audio == [b"audio@0.000", b"audio@30.000", b"audio@60.000", b"audio@90.000"]
    assert [cue.start for cue in cues] == [0.0, 30.0, 60.0, 90.0]


def test_client_uses_the_configured_key_model_and_no_sdk_retries() -> None:
    seen: list[str] = []

    class Recording(FakeGemini):
        def generate_content(self, *, model: str, contents: list[object], config: object):
            seen.append(model)
            return super().generate_content(model=model, contents=contents, config=config)

    gemini = Recording([reply("en", (0, 1, "hi"))])
    transcribe(gemini, 10)
    assert seen == ["fake-asr-model"]
    [options] = gemini.client_options
    assert options["api_key"] == "fake-asr-key"
    http = options["http_options"]
    assert isinstance(http, types.HttpOptions)
    assert http.retry_options is not None and http.retry_options.attempts == 1


def test_segments_outside_the_chunk_are_dropped_and_overruns_clamped() -> None:
    gemini = FakeGemini(
        [
            reply(
                "en",
                (-2.0, 0.0, "before the clip"),
                (5.0, 3.0, "inverted"),
                (8.0, 8.0, "zero length"),
                (10.0, 12.0, "   "),
                (-1.0, 2.0, "starts early"),
                (175.0, 190.0, "runs past the chunk"),
                (185.0, 186.0, "after the clip"),
            ),
            reply("en", (0.0, 50.0, "runs past the media")),
        ]
    )
    _, cues = transcribe(gemini, CHUNK_SECONDS + 10)
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [
        (0.0, 2.0, "starts early"),
        (175.0, 180.0, "runs past the chunk"),
        (180.0, 190.0, "runs past the media"),
    ]


def test_language_falls_back_to_the_request_when_nothing_was_spoken_first() -> None:
    gemini = FakeGemini([reply("xx-silence-guess"), reply("ko", (1, 2, "안녕"))])
    language, _ = transcribe(gemini, CHUNK_SECONDS + 10)
    assert language == "ko"
    gemini = FakeGemini([reply("", (1, 2, "hello"))])
    language, _ = transcribe(gemini, 10)
    assert language == "en"


@pytest.mark.parametrize(
    "bad",
    [
        response('{"language": "en", "segments": []}', blocked=True),
        response('{"language": "en", "segments": []}', types.FinishReason.MAX_TOKENS),
        response('{"language": "en", "segments": [{"start": 0'),
        response('{"language": "en", "segments": [{"start": NaN, "end": 1, "text": "x"}]}'),
        response('{"language": "not a tag!", "segments": [{"start": 0, "end": 1, "text": "x"}]}'),
    ],
)
def test_unusable_responses_are_reported_as_invalid(bad: types.GenerateContentResponse) -> None:
    with pytest.raises(SourceError) as error:
        transcribe(FakeGemini([bad]), 10)
    assert error.value.code == "asr_response_invalid"


@pytest.mark.parametrize(
    "status,retry_after,code",
    [
        (401, None, "asr_key_invalid"),
        (403, None, "asr_permission_denied"),
        (404, None, "asr_model_missing"),
        (429, "42", "asr_rate_limited;retry_after=42s"),
        (503, None, "asr_provider_unavailable"),
        (400, None, "asr_provider_request_failed"),
    ],
)
def test_provider_failures_become_named_codes_without_the_body(
    status: int, retry_after: str | None, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    headers = {"Retry-After": retry_after} if retry_after else {}
    failure = errors.APIError(
        status, {"error": {"message": "body fake-asr-key"}}, httpx.Response(status, headers=headers)
    )
    with pytest.raises(SourceError) as error:
        transcribe(FakeGemini([failure]), 10)
    assert error.value.code == code
    assert "stage=asr" in caplog.text
    assert "fake-asr-key" not in caplog.text


def test_invalid_key_reported_as_400_is_named() -> None:
    body = {"error": {"details": [{"reason": "API_KEY_INVALID"}]}}
    with pytest.raises(SourceError) as error:
        transcribe(FakeGemini([errors.APIError(400, body)]), 10)
    assert error.value.code == "asr_key_invalid"


def test_unreachable_provider_is_named() -> None:
    with pytest.raises(SourceError) as error:
        transcribe(FakeGemini([httpx.ReadTimeout("slow")]), 10)
    assert error.value.code == "asr_provider_unavailable"


def test_missing_key_fails_before_any_audio_is_extracted() -> None:
    ffmpeg, gemini = FakeFFmpeg(), FakeGemini([])
    adapter = GeminiSubtitleAdapter(settings(None), gemini, ffmpeg)
    with pytest.raises(SourceError) as error:
        adapter.transcribe(Path("media.mp4"), "en", 10, Event())
    assert error.value.code == "asr_key_missing"
    assert ffmpeg.commands == [] and gemini.client_options == []


def test_audio_extraction_failure_is_named() -> None:
    with pytest.raises(SourceError) as error:
        transcribe(FakeGemini([]), 10, ffmpeg=FakeFFmpeg(fail=True))
    assert error.value.code == "asr_media_failed"


def test_cancel_stops_before_the_next_chunk() -> None:
    cancel = Event()
    gemini = FakeGemini(
        [reply("en", (0, 1, "one")), reply("en", (0, 1, "two"))], on_request=cancel.set
    )
    ffmpeg = FakeFFmpeg()
    with pytest.raises(SourceError) as error:
        transcribe(gemini, 2 * CHUNK_SECONDS, cancel, ffmpeg)
    assert error.value.code == "cancelled"
    assert len(gemini.audio) == 1 and len(ffmpeg.commands) == 1


def test_cancel_during_extraction_is_reported_as_cancelled() -> None:
    cancel = Event()

    def interrupted(command: list[str], event: Event) -> str:
        cancel.set()
        raise SourceError("cancelled", "工作已取消")

    with pytest.raises(SourceError) as error:
        transcribe(FakeGemini([]), 10, cancel, interrupted)
    assert error.value.code == "cancelled"
